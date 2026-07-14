"""Runtime configuration for the VPS monitor.

The service intentionally has a small, explicit configuration surface. Every
value can be overridden from ``/etc/default/loop-monitor`` without adding a
second config-file parser or a dependency.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def _number(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric, got {raw!r}") from exc


def _integer(env: Mapping[str, str], name: str, default: int) -> int:
    value = _number(env, name, float(default))
    if not value.is_integer():
        raise ValueError(f"{name} must be an integer, got {value}")
    return int(value)


@dataclass(frozen=True)
class MonitorConfig:
    data_dir: Path = Path("/var/lib/loop-monitor")
    socket_path: Path = Path("/run/loop-monitor/events.sock")
    interval_seconds: float = 15.0
    retention_days: int = 21
    cpu_incident_pct: float = 70.0
    memory_available_incident_pct: float = 15.0
    load_incident_per_cpu: float = 1.0
    io_full_pressure_incident_pct: float = 1.0
    incident_detail_interval_seconds: float = 30.0
    docker_inspect_refresh_seconds: float = 300.0
    top_process_limit: int = 20
    hook_state_ttl_seconds: float = 86_400.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MonitorConfig:
        source = os.environ if env is None else env
        config = cls(
            data_dir=Path(source.get("LOOP_MONITOR_DATA_DIR", str(cls.data_dir))),
            socket_path=Path(source.get("LOOP_MONITOR_SOCKET", str(cls.socket_path))),
            interval_seconds=_number(
                source, "LOOP_MONITOR_INTERVAL", cls.interval_seconds
            ),
            retention_days=_integer(
                source, "LOOP_MONITOR_RETENTION_DAYS", cls.retention_days
            ),
            cpu_incident_pct=_number(
                source, "LOOP_MONITOR_CPU_INCIDENT_PCT", cls.cpu_incident_pct
            ),
            memory_available_incident_pct=_number(
                source,
                "LOOP_MONITOR_MEMORY_AVAILABLE_INCIDENT_PCT",
                cls.memory_available_incident_pct,
            ),
            load_incident_per_cpu=_number(
                source, "LOOP_MONITOR_LOAD_INCIDENT_PER_CPU", cls.load_incident_per_cpu
            ),
            io_full_pressure_incident_pct=_number(
                source,
                "LOOP_MONITOR_IO_FULL_PRESSURE_INCIDENT_PCT",
                cls.io_full_pressure_incident_pct,
            ),
            incident_detail_interval_seconds=_number(
                source,
                "LOOP_MONITOR_INCIDENT_DETAIL_INTERVAL",
                cls.incident_detail_interval_seconds,
            ),
            docker_inspect_refresh_seconds=_number(
                source,
                "LOOP_MONITOR_DOCKER_INSPECT_REFRESH",
                cls.docker_inspect_refresh_seconds,
            ),
            top_process_limit=_integer(
                source, "LOOP_MONITOR_TOP_PROCESS_LIMIT", cls.top_process_limit
            ),
            hook_state_ttl_seconds=_number(
                source, "LOOP_MONITOR_HOOK_STATE_TTL", cls.hook_state_ttl_seconds
            ),
        )
        if config.interval_seconds < 5:
            raise ValueError("LOOP_MONITOR_INTERVAL must be at least 5 seconds")
        if not 1 <= config.retention_days <= 365:
            raise ValueError("LOOP_MONITOR_RETENTION_DAYS must be between 1 and 365")
        if config.top_process_limit < 1:
            raise ValueError("LOOP_MONITOR_TOP_PROCESS_LIMIT must be positive")
        return config
