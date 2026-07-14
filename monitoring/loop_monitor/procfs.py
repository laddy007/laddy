"""Low-overhead host and process metrics read directly from procfs."""

from __future__ import annotations

import os
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loop_monitor.classify import ProcessIdentity, classify_process, inherited_identity

# procfs collection only runs on Linux; on non-POSIX dev machines (Windows)
# os.sysconf is absent. The collector never executes there (no /proc), so the
# values are unused — fall back to the canonical Linux defaults to keep the
# module importable for the pure-logic unit tests.
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
_CLOCK_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return None


def _read_key_values(path: Path) -> dict[str, int]:
    text = _read_text(path)
    if text is None:
        return {}
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, tail = line.partition(":")
        raw = tail.strip().split(maxsplit=1)[0] if tail else ""
        try:
            values[key] = int(raw)
        except ValueError:
            continue
    return values


def _delta(current: int, previous: int | None) -> int:
    if previous is None or current < previous:
        return 0
    return current - previous


@dataclass(frozen=True)
class ProcessSample:
    pid: int
    ppid: int
    start_ticks: int
    comm: str
    identity: ProcessIdentity
    cpu_ticks: int
    rss_bytes: int
    read_bytes: int
    write_bytes: int
    cpu_pct: float = 0.0
    read_bps: float = 0.0
    write_bps: float = 0.0

    @property
    def key(self) -> tuple[int, int]:
        return self.pid, self.start_ticks

    def detail_json(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "comm": self.comm[:64],
            **self.identity.to_json(),
            "cpu_pct": round(self.cpu_pct, 2),
            "rss_bytes": self.rss_bytes,
            "read_bps": round(self.read_bps, 1),
            "write_bps": round(self.write_bps, 1),
        }


class HostCollector:
    def __init__(self, proc_root: Path = Path("/proc")) -> None:
        self.proc_root = proc_root
        self._previous_cpu: tuple[int, int] | None = None
        self._previous_vmstat: dict[str, int] = {}
        self._previous_disk: dict[str, int] = {}
        self._previous_at: float | None = None

    def collect(self, now: float | None = None) -> dict[str, Any]:
        measured_at = time.monotonic() if now is None else now
        elapsed = (
            measured_at - self._previous_at if self._previous_at is not None else 0.0
        )
        cpu = self._cpu()
        memory = self._memory()
        vmstat = self._vmstat()
        disk = self._disk(elapsed)
        pressures = {
            resource: self._pressure(resource) for resource in ("cpu", "memory", "io")
        }
        try:
            load1, load5, load15 = os.getloadavg()
        except OSError:
            load1 = load5 = load15 = 0.0
        result: dict[str, Any] = {
            "cpu_pct": round(cpu, 2),
            "load": {"1m": load1, "5m": load5, "15m": load15},
            "cpu_count": os.cpu_count() or 1,
            "memory": memory,
            "vm": vmstat,
            "disk": disk,
            "pressure": pressures,
        }
        self._previous_at = measured_at
        return result

    def _cpu(self) -> float:
        text = _read_text(self.proc_root / "stat") or ""
        line = next((item for item in text.splitlines() if item.startswith("cpu ")), "")
        try:
            parts = [int(value) for value in line.split()[1:]]
        except ValueError:
            parts = []
        if len(parts) < 5:
            return 0.0
        total = sum(parts)
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        previous = self._previous_cpu
        self._previous_cpu = (total, idle)
        if previous is None:
            return 0.0
        total_delta = total - previous[0]
        idle_delta = idle - previous[1]
        if total_delta <= 0:
            return 0.0
        return max(0.0, min(100.0, 100.0 * (total_delta - idle_delta) / total_delta))

    def _memory(self) -> dict[str, int | float]:
        values = _read_key_values(self.proc_root / "meminfo")
        total = values.get("MemTotal", 0) * 1024
        available = values.get("MemAvailable", 0) * 1024
        swap_total = values.get("SwapTotal", 0) * 1024
        swap_free = values.get("SwapFree", 0) * 1024
        return {
            "total_bytes": total,
            "available_bytes": available,
            "used_bytes": max(0, total - available),
            "available_pct": round(100.0 * available / total, 2) if total else 0.0,
            "swap_total_bytes": swap_total,
            "swap_used_bytes": max(0, swap_total - swap_free),
            "cached_bytes": values.get("Cached", 0) * 1024,
            "buffers_bytes": values.get("Buffers", 0) * 1024,
            "dirty_bytes": values.get("Dirty", 0) * 1024,
        }

    def _vmstat(self) -> dict[str, int]:
        text = _read_text(self.proc_root / "vmstat") or ""
        current: dict[str, int] = {}
        for line in text.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[0] in {
                "pswpin",
                "pswpout",
                "pgmajfault",
                "oom_kill",
            }:
                try:
                    current[fields[0]] = int(fields[1])
                except ValueError:
                    pass
        result = {
            "swap_in_pages": current.get("pswpin", 0),
            "swap_out_pages": current.get("pswpout", 0),
            "swap_in_pages_delta": _delta(
                current.get("pswpin", 0), self._previous_vmstat.get("pswpin")
            ),
            "swap_out_pages_delta": _delta(
                current.get("pswpout", 0), self._previous_vmstat.get("pswpout")
            ),
            "major_faults_delta": _delta(
                current.get("pgmajfault", 0), self._previous_vmstat.get("pgmajfault")
            ),
            "oom_kills_delta": _delta(
                current.get("oom_kill", 0), self._previous_vmstat.get("oom_kill")
            ),
        }
        self._previous_vmstat = current
        return result

    def _root_device_name(self) -> str | None:
        try:
            device = os.stat("/").st_dev
        except OSError:
            return None
        major, minor = os.major(device), os.minor(device)
        link = Path("/sys/dev/block") / f"{major}:{minor}"
        try:
            return link.resolve().name
        except OSError:
            return None

    def _disk(self, elapsed: float) -> dict[str, int | float | str | None]:
        target = self._root_device_name()
        text = _read_text(self.proc_root / "diskstats") or ""
        fields: list[str] | None = None
        for line in text.splitlines():
            candidate = line.split()
            if len(candidate) >= 14 and (target is None or candidate[2] == target):
                if target is not None or candidate[2].startswith(("sd", "vd", "nvme")):
                    fields = candidate
                    target = candidate[2]
                    break
        if fields is None:
            return {
                "device": target,
                "read_bps": 0.0,
                "write_bps": 0.0,
                "io_util_pct": 0.0,
            }
        try:
            current = {
                "read_sectors": int(fields[5]),
                "write_sectors": int(fields[9]),
                "io_ms": int(fields[12]),
            }
        except (ValueError, IndexError):
            current = {"read_sectors": 0, "write_sectors": 0, "io_ms": 0}
        read_delta = _delta(
            current["read_sectors"], self._previous_disk.get("read_sectors")
        )
        write_delta = _delta(
            current["write_sectors"], self._previous_disk.get("write_sectors")
        )
        io_ms_delta = _delta(current["io_ms"], self._previous_disk.get("io_ms"))
        self._previous_disk = current
        return {
            "device": target,
            "read_bps": round(read_delta * 512 / elapsed, 1) if elapsed > 0 else 0.0,
            "write_bps": round(write_delta * 512 / elapsed, 1) if elapsed > 0 else 0.0,
            "io_util_pct": round(min(100.0, io_ms_delta / (elapsed * 10)), 2)
            if elapsed > 0
            else 0.0,
        }

    def _pressure(self, resource: str) -> dict[str, float]:
        text = _read_text(self.proc_root / "pressure" / resource) or ""
        result: dict[str, float] = {}
        for line in text.splitlines():
            fields = line.split()
            if not fields:
                continue
            prefix = fields[0]
            for item in fields[1:]:
                key, _, raw = item.partition("=")
                if key not in {"avg10", "avg60", "avg300"}:
                    continue
                try:
                    result[f"{prefix}_{key}"] = float(raw)
                except ValueError:
                    continue
        return result


class ProcessCollector:
    def __init__(self, proc_root: Path = Path("/proc")) -> None:
        self.proc_root = proc_root
        self._previous: dict[tuple[int, int], ProcessSample] = {}
        self._previous_at: float | None = None

    def collect(
        self, now: float | None = None
    ) -> tuple[dict[str, Any], list[ProcessSample]]:
        measured_at = time.monotonic() if now is None else now
        elapsed = (
            measured_at - self._previous_at if self._previous_at is not None else 0.0
        )
        raw = self._scan()
        by_pid = {sample.pid: sample for sample in raw}
        resolved_identities: dict[int, ProcessIdentity] = {}

        def resolve_identity(
            pid: int, seen: frozenset[int] = frozenset()
        ) -> ProcessIdentity | None:
            if pid in resolved_identities:
                return resolved_identities[pid]
            sample = by_pid.get(pid)
            if sample is None or pid in seen:
                return None
            parent = resolve_identity(sample.ppid, seen | {pid})
            identity = inherited_identity(sample.identity, parent)
            resolved_identities[pid] = identity
            return identity

        resolved: list[ProcessSample] = []
        for sample in raw:
            identity = resolve_identity(sample.pid) or sample.identity
            previous = self._previous.get(sample.key)
            cpu_pct = (
                100.0
                * _delta(sample.cpu_ticks, previous.cpu_ticks if previous else None)
                / (_CLOCK_TICKS * elapsed)
                if elapsed > 0
                else 0.0
            )
            read_bps = (
                _delta(sample.read_bytes, previous.read_bytes if previous else None)
                / elapsed
                if elapsed > 0
                else 0.0
            )
            write_bps = (
                _delta(sample.write_bytes, previous.write_bytes if previous else None)
                / elapsed
                if elapsed > 0
                else 0.0
            )
            resolved.append(
                ProcessSample(
                    **{
                        **sample.__dict__,
                        "identity": identity,
                        "cpu_pct": cpu_pct,
                        "read_bps": read_bps,
                        "write_bps": write_bps,
                    }
                )
            )
        self._previous = {sample.key: sample for sample in resolved}
        self._previous_at = measured_at
        aggregates: dict[str, dict[str, float | int]] = defaultdict(
            lambda: {
                "processes": 0,
                "cpu_pct": 0.0,
                "rss_bytes": 0,
                "read_bps": 0.0,
                "write_bps": 0.0,
            }
        )
        for sample in resolved:
            bucket = aggregates[sample.identity.operation]
            bucket["processes"] = int(bucket["processes"]) + 1
            bucket["cpu_pct"] = float(bucket["cpu_pct"]) + sample.cpu_pct
            bucket["rss_bytes"] = int(bucket["rss_bytes"]) + sample.rss_bytes
            bucket["read_bps"] = float(bucket["read_bps"]) + sample.read_bps
            bucket["write_bps"] = float(bucket["write_bps"]) + sample.write_bps
        for bucket in aggregates.values():
            bucket["cpu_pct"] = round(float(bucket["cpu_pct"]), 2)
            bucket["read_bps"] = round(float(bucket["read_bps"]), 1)
            bucket["write_bps"] = round(float(bucket["write_bps"]), 1)
        counts = {
            "claude_agents": sum(
                1 for item in resolved if item.identity.operation == "claude_agent"
            ),
            "codex_agents": sum(
                1 for item in resolved if item.identity.operation == "codex_agent"
            ),
            "agent_helpers": sum(
                1 for item in resolved if item.identity.is_agent_helper
            ),
            "pytest_processes": sum(
                1 for item in resolved if item.identity.operation == "pytest"
            ),
            "pytest_workers": sum(
                1 for item in resolved if item.identity.operation == "pytest_worker"
            ),
            "build_processes": sum(
                1 for item in resolved if item.identity.category == "build"
            ),
            "test_processes_total": sum(
                1 for item in resolved if item.identity.category == "test"
            ),
        }
        tasks = sorted(
            {item.identity.task for item in resolved if item.identity.task is not None}
        )
        return {
            "counts": counts,
            "operations": dict(aggregates),
            "tasks": tasks,
        }, resolved

    def _scan(self) -> list[ProcessSample]:
        samples: list[ProcessSample] = []
        for entry in self.proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            sample = self._read_process(entry)
            if sample is not None:
                samples.append(sample)
        return samples

    def _read_process(self, directory: Path) -> ProcessSample | None:
        stat_text = _read_text(directory / "stat")
        if stat_text is None:
            return None
        close = stat_text.rfind(")")
        if close < 0:
            return None
        try:
            pid = int(directory.name)
            comm = stat_text[stat_text.find("(") + 1 : close]
            fields = stat_text[close + 2 :].split()
            ppid = int(fields[1])
            cpu_ticks = int(fields[11]) + int(fields[12])
            start_ticks = int(fields[19])
            rss_bytes = int(fields[21]) * _PAGE_SIZE
        except (ValueError, IndexError):
            return None
        cmdline = _read_text(directory / "cmdline")
        argv = tuple(part for part in (cmdline or "").split("\x00") if part)
        try:
            cwd = os.readlink(directory / "cwd")
        except OSError:
            cwd = None
        io_values = _read_key_values(directory / "io")
        identity = classify_process(comm=comm, argv=argv, cwd=cwd)
        return ProcessSample(
            pid=pid,
            ppid=ppid,
            start_ticks=start_ticks,
            comm=comm,
            identity=identity,
            cpu_ticks=cpu_ticks,
            rss_bytes=max(0, rss_bytes),
            read_bytes=io_values.get("read_bytes", 0),
            write_bytes=io_values.get("write_bytes", 0),
        )


def ranked_processes(
    samples: list[ProcessSample], limit: int
) -> list[dict[str, object]]:
    """Return one deduplicated top list for CPU, RSS and I/O attribution."""

    selected: dict[tuple[int, int], ProcessSample] = {}
    per_dimension = max(3, limit // 3)
    for key in ("cpu_pct", "rss_bytes", "read_bps", "write_bps"):
        for sample in sorted(
            samples, key=lambda item: getattr(item, key), reverse=True
        )[:per_dimension]:
            selected[sample.key] = sample
    ranked = sorted(
        selected.values(),
        key=lambda item: (item.cpu_pct, item.rss_bytes, item.read_bps + item.write_bps),
        reverse=True,
    )
    return [sample.detail_json() for sample in ranked[:limit]]
