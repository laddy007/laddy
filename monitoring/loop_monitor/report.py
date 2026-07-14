"""Human-readable analysis of a selected monitoring window."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loop_monitor.storage import iter_records


def parse_time(value: str) -> float:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def _nested(record: dict[str, Any], *keys: str, default: Any = 0) -> Any:
    value: Any = record
    for key in keys:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    return default if value is None else value


def _bytes(value: float | int) -> str:
    number = float(value)
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(number) < 1024 or suffix == "TiB":
            return f"{number:.1f} {suffix}"
        number /= 1024
    return f"{number:.1f} TiB"


def _nearest(records: list[dict[str, Any]], timestamp: float) -> dict[str, Any] | None:
    return (
        min(records, key=lambda item: abs(float(item["timestamp"]) - timestamp))
        if records
        else None
    )


def build_report(data_dir: Path, at: float, window_seconds: float) -> str:
    start, end = at - window_seconds, at + window_seconds
    samples = list(iter_records(data_dir, "samples", start, end))
    incidents = list(iter_records(data_dir, "incidents", start, end))
    events = list(iter_records(data_dir, "events", start, end))
    if not samples:
        return f"No samples in {datetime.fromtimestamp(start).isoformat()} .. {datetime.fromtimestamp(end).isoformat()}"
    peak_cpu = max(samples, key=lambda row: float(_nested(row, "host", "cpu_pct")))
    min_memory = min(
        samples,
        key=lambda row: float(_nested(row, "host", "memory", "available_bytes")),
    )
    max_swap = max(
        samples, key=lambda row: int(_nested(row, "host", "memory", "swap_used_bytes"))
    )
    max_io = max(
        samples,
        key=lambda row: (
            float(_nested(row, "host", "disk", "read_bps"))
            + float(_nested(row, "host", "disk", "write_bps"))
        ),
    )
    focus = _nearest(samples, at)
    assert focus is not None
    lines = [
        f"Window: {samples[0]['time']} .. {samples[-1]['time']} ({len(samples)} samples)",
        f"Peak CPU: {_nested(peak_cpu, 'host', 'cpu_pct'):.1f}% at {peak_cpu['time']}",
        f"Minimum available RAM: {_bytes(_nested(min_memory, 'host', 'memory', 'available_bytes'))} "
        f"at {min_memory['time']}",
        f"Maximum swap used: {_bytes(_nested(max_swap, 'host', 'memory', 'swap_used_bytes'))} "
        f"at {max_swap['time']}",
        f"Peak root-disk throughput: "
        f"{_bytes(float(_nested(max_io, 'host', 'disk', 'read_bps')) + float(_nested(max_io, 'host', 'disk', 'write_bps')))}/s "
        f"at {max_io['time']}",
        "",
        f"Nearest sample: {focus['time']}",
        f"Agents: Claude sessions={_nested(focus, 'agents', 'claude_sessions')}, "
        f"Codex sessions={_nested(focus, 'agents', 'codex_sessions')}, "
        f"logical subagents={_nested(focus, 'agents', 'subagents_total')}, "
        f"parallel total={_nested(focus, 'agents', 'parallel_total')}",
        f"Tests: pytest roots={_nested(focus, 'processes', 'counts', 'pytest_processes')}, "
        f"xdist/workers={_nested(focus, 'processes', 'counts', 'pytest_workers')}",
        f"Build processes: {_nested(focus, 'processes', 'counts', 'build_processes')}",
    ]
    active_operations = _nested(focus, "processes", "operations", default={})
    if isinstance(active_operations, dict):
        relevant = [
            (name, metrics)
            for name, metrics in active_operations.items()
            if isinstance(metrics, dict)
            and name != "other"
            and int(metrics.get("processes") or 0) > 0
        ]
        if relevant:
            lines.append("Active operations:")
            for name, metrics in sorted(relevant):
                lines.append(
                    f"  {name}: n={metrics['processes']} cpu={float(metrics['cpu_pct']):.1f}% "
                    f"rss={_bytes(int(metrics['rss_bytes']))}"
                )
    if incidents:
        lines.extend(["", f"Incident details ({len(incidents)}):"])
        for incident in incidents:
            lines.append(
                f"  {incident['time']}: {', '.join(incident.get('reasons', []))}"
            )
            top = incident.get("top_processes")
            if isinstance(top, list):
                for process in top[:5]:
                    if isinstance(process, dict):
                        lines.append(
                            f"    pid={process.get('pid')} {process.get('comm')} "
                            f"op={process.get('operation')} cpu={process.get('cpu_pct')}% "
                            f"rss={_bytes(int(process.get('rss_bytes') or 0))}"
                        )
    lifecycle = [event for event in events if event.get("source") == "docker"]
    if lifecycle:
        lines.extend(["", "Docker lifecycle events:"])
        for event in lifecycle:
            lines.append(
                f"  {event['time']}: {event.get('name') or event.get('container_id')} "
                f"{event.get('action')} exit={event.get('exit_code', '-')}"
            )
    return "\n".join(lines)


def overhead_report(data_dir: Path, hours: float = 24.0) -> str:
    end = time.time()
    samples = list(iter_records(data_dir, "samples", end - hours * 3600, end))
    cpu: list[float] = []
    rss: list[int] = []
    for sample in samples:
        operations = _nested(sample, "processes", "operations", default={})
        monitoring = (
            operations.get("loop_monitor", {}) if isinstance(operations, dict) else {}
        )
        if isinstance(monitoring, dict):
            cpu.append(float(monitoring.get("cpu_pct") or 0))
            rss.append(int(monitoring.get("rss_bytes") or 0))
    today = datetime.now(tz=timezone.utc).date().isoformat()
    written = sum(path.stat().st_size for path in data_dir.glob(f"*/{today}.jsonl"))
    first_timestamp = min(
        (float(sample["timestamp"]) for sample in samples),
        default=end - hours * 3600,
    )
    measured_seconds = max(15.0, end - first_timestamp)
    projected = written * 86_400 / measured_seconds
    return "\n".join(
        [
            f"Samples inspected: {len(samples)}",
            f"Monitor average CPU: {sum(cpu) / len(cpu):.3f}%"
            if cpu
            else "Monitor average CPU: unavailable",
            f"Monitor maximum RSS: {_bytes(max(rss))}"
            if rss
            else "Monitor maximum RSS: unavailable",
            f"Data written today: {_bytes(written)}",
            f"Projected daily write: {_bytes(projected)}",
        ]
    )


def json_report(data_dir: Path, at: float, window_seconds: float) -> str:
    start, end = at - window_seconds, at + window_seconds
    return json.dumps(
        {
            "samples": list(iter_records(data_dir, "samples", start, end)),
            "incidents": list(iter_records(data_dir, "incidents", start, end)),
            "events": list(iter_records(data_dir, "events", start, end)),
        },
        indent=2,
        sort_keys=True,
    )
