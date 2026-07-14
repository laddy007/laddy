from __future__ import annotations

from loop_monitor.collector import incident_reasons
from loop_monitor.config import MonitorConfig


def test_incident_reasons_cover_swap_oom_pressure_and_docker() -> None:
    host = {
        "cpu_pct": 90,
        "cpu_count": 6,
        "load": {"1m": 7},
        "memory": {"available_pct": 10},
        "vm": {
            "swap_in_pages_delta": 2,
            "swap_out_pages_delta": 3,
            "oom_kills_delta": 1,
        },
        "pressure": {"io": {"full_avg10": 2}},
    }

    assert incident_reasons(
        host,
        [{"action": "oom"}, {"action": "health_status: unhealthy"}],
        MonitorConfig(),
    ) == [
        "docker_health_status_unhealthy",
        "docker_oom",
        "high_cpu",
        "high_load",
        "host_oom_kill",
        "io_pressure",
        "low_memory",
        "swap_in",
        "swap_out",
    ]


def test_zero_available_memory_is_not_treated_as_missing_metric() -> None:
    host = {
        "cpu_count": 1,
        "memory": {"available_pct": 0},
        "load": {},
        "vm": {},
        "pressure": {},
    }

    assert "low_memory" in incident_reasons(host, [], MonitorConfig())
