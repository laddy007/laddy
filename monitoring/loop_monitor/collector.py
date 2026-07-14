"""Long-running aggregation loop."""

from __future__ import annotations

import signal
import threading
import time
from datetime import datetime, timezone
from typing import Any

from loop_monitor.config import MonitorConfig
from loop_monitor.docker_api import DockerAPI, DockerCollector
from loop_monitor.events import ActiveSubagents, HookServer
from loop_monitor.procfs import HostCollector, ProcessCollector, ranked_processes
from loop_monitor.storage import JsonlStore, iter_records

SCHEMA_VERSION = 1


def _iso(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _timestamp(value: object, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _number(value: object, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def incident_reasons(
    host: dict[str, Any], docker_events: list[dict[str, object]], config: MonitorConfig
) -> list[str]:
    reasons: list[str] = []
    cpu = _number(host.get("cpu_pct"), 0)
    if cpu >= config.cpu_incident_pct:
        reasons.append("high_cpu")
    load = _mapping(host.get("load"))
    cpu_count = int(host.get("cpu_count") or 1)
    if _number(load.get("1m"), 0) >= cpu_count * config.load_incident_per_cpu:
        reasons.append("high_load")
    memory = _mapping(host.get("memory"))
    if (
        _number(memory.get("available_pct"), 100)
        <= config.memory_available_incident_pct
    ):
        reasons.append("low_memory")
    vm = _mapping(host.get("vm"))
    if int(vm.get("swap_in_pages_delta") or 0) > 0:
        reasons.append("swap_in")
    if int(vm.get("swap_out_pages_delta") or 0) > 0:
        reasons.append("swap_out")
    if int(vm.get("oom_kills_delta") or 0) > 0:
        reasons.append("host_oom_kill")
    pressure = _mapping(host.get("pressure"))
    io_pressure = _mapping(pressure.get("io"))
    if (
        _number(io_pressure.get("full_avg10"), 0)
        >= config.io_full_pressure_incident_pct
    ):
        reasons.append("io_pressure")
    for event in docker_events:
        action = str(event.get("action") or "").replace(" ", "_").replace(":", "")
        if action in {"oom", "die", "restart", "health_status_unhealthy"}:
            reasons.append(f"docker_{action}")
    return sorted(set(reasons))


class Monitor:
    def __init__(self, config: MonitorConfig) -> None:
        self.config = config
        self.host = HostCollector()
        self.processes = ProcessCollector()
        self.docker = DockerCollector(
            DockerAPI(), inspect_refresh_seconds=config.docker_inspect_refresh_seconds
        )
        self.hooks = HookServer(config.socket_path)
        self.subagents = ActiveSubagents(config.hook_state_ttl_seconds)
        self.store = JsonlStore(config.data_dir, config.retention_days)
        self.stop_event = threading.Event()
        self._last_detail_at = 0.0
        self._hooks_active = False
        self._restore_subagents()

    def _restore_subagents(self) -> None:
        now = time.time()
        start = now - self.config.hook_state_ttl_seconds
        for event in iter_records(self.config.data_dir, "events", start, now):
            if event.get("source") == "agent_hook":
                self.subagents.apply(event)

    def start(self) -> None:
        self._hooks_active = self.hooks.start()
        self.docker.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.hooks.stop()
        self.docker.stop()
        self.store.close()

    def run(self) -> None:
        self.start()
        try:
            deadline = time.monotonic()
            while not self.stop_event.is_set():
                started = time.monotonic()
                try:
                    self.collect_once()
                except Exception as exc:  # keep monitoring alive; error is sanitized
                    now = time.time()
                    self.store.append(
                        "events",
                        {
                            "schema": SCHEMA_VERSION,
                            "timestamp": now,
                            "time": _iso(now),
                            "source": "monitor",
                            "action": "collection_error",
                            "error": type(exc).__name__,
                        },
                        now,
                    )
                deadline += self.config.interval_seconds
                if deadline <= started:
                    deadline = started + self.config.interval_seconds
                self.stop_event.wait(max(0.0, deadline - time.monotonic()))
        finally:
            self.stop()

    def collect_once(self, timestamp: float | None = None) -> dict[str, Any]:
        now = time.time() if timestamp is None else timestamp
        monotonic = time.monotonic()
        hook_events = self.hooks.drain()
        for event in hook_events:
            event_timestamp = _timestamp(event.get("observed_at"), now)
            event["timestamp"] = event_timestamp
            event["time"] = _iso(event_timestamp)
            event["schema"] = SCHEMA_VERSION
            self.subagents.apply(event)
            self.store.append("events", event, event_timestamp)
        host = self.host.collect(monotonic)
        process_summary, process_samples = self.processes.collect(monotonic)
        docker, docker_events = self.docker.collect(monotonic)
        for event in docker_events:
            event.update(
                {"schema": SCHEMA_VERSION, "timestamp": now, "time": _iso(now)}
            )
            self.store.append("events", event, now)
        process_counts = process_summary["counts"]
        self.subagents.reconcile_sessions(
            process_counts["claude_agents"], process_counts["codex_agents"]
        )
        logical = self.subagents.counts(now)
        agents = {
            **logical,
            "claude_sessions": process_counts["claude_agents"],
            "codex_sessions": process_counts["codex_agents"],
            "helper_processes": process_counts["agent_helpers"],
            "parallel_total": process_counts["claude_agents"]
            + process_counts["codex_agents"]
            + logical["subagents_total"],
            "subagent_precision": "hooks" if self._hooks_active else "process_only",
        }
        sample: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "timestamp": now,
            "time": _iso(now),
            "host": host,
            "processes": process_summary,
            "agents": agents,
            "docker": docker,
        }
        self.store.append("samples", sample, now)
        reasons = incident_reasons(host, docker_events, self.config)
        critical = any(
            reason in {"host_oom_kill", "docker_oom", "docker_die", "docker_restart"}
            for reason in reasons
        )
        detail_due = (
            monotonic - self._last_detail_at
            >= self.config.incident_detail_interval_seconds
        )
        if reasons and (critical or detail_due):
            incident = {
                "schema": SCHEMA_VERSION,
                "timestamp": now,
                "time": _iso(now),
                "reasons": reasons,
                "host": host,
                "agents": agents,
                "processes": process_summary,
                "top_processes": ranked_processes(
                    process_samples, self.config.top_process_limit
                ),
                "docker": docker,
                "docker_events": docker_events,
            }
            self.store.append("incidents", incident, now)
            self._last_detail_at = monotonic
        return sample


def run_service(config: MonitorConfig) -> None:
    monitor = Monitor(config)

    def request_stop(_signum: int, _frame: object) -> None:
        monitor.stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    monitor.run()
