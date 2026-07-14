from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from loop_monitor.config import MonitorConfig
from loop_monitor.hook_config import configure
from loop_monitor.storage import JsonlStore, iter_records


def _timestamp(day: str) -> float:
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def test_daily_store_and_retention(tmp_path) -> None:
    store = JsonlStore(tmp_path, retention_days=2)
    store.append(
        "samples",
        {"timestamp": _timestamp("2026-07-01"), "value": 1},
        _timestamp("2026-07-01"),
    )
    store.append(
        "samples",
        {"timestamp": _timestamp("2026-07-05"), "value": 2},
        _timestamp("2026-07-05"),
    )
    store.close()

    assert not (tmp_path / "samples" / "2026-07-01.jsonl").exists()
    records = list(
        iter_records(
            tmp_path, "samples", _timestamp("2026-07-04"), _timestamp("2026-07-06")
        )
    )
    assert records == [{"timestamp": _timestamp("2026-07-05"), "value": 2}]


def test_config_rejects_too_fast_interval() -> None:
    with pytest.raises(ValueError, match="at least 5"):
        MonitorConfig.from_env({"LOOP_MONITOR_INTERVAL": "1"})


def test_hook_config_merges_and_removes_only_monitor_handlers(tmp_path) -> None:
    claude = tmp_path / ".claude" / "settings.json"
    claude.parent.mkdir()
    claude.write_text(
        json.dumps(
            {
                "hooks": {
                    "SubagentStart": [
                        {
                            "matcher": "Explore",
                            "hooks": [{"type": "command", "command": "/existing"}],
                        }
                    ]
                },
                "permissions": {"allow": ["Read"]},
            }
        ),
        encoding="utf-8",
    )

    assert configure(tmp_path) == {"claude": True, "codex": True}
    assert configure(tmp_path) == {"claude": False, "codex": False}
    installed = json.loads(claude.read_text(encoding="utf-8"))
    assert installed["permissions"] == {"allow": ["Read"]}
    assert any(
        handler.get("command") == "/existing"
        for group in installed["hooks"]["SubagentStart"]
        for handler in group["hooks"]
    )

    assert configure(tmp_path, uninstall=True) == {"claude": True, "codex": True}
    removed = json.loads(claude.read_text(encoding="utf-8"))
    assert removed["permissions"] == {"allow": ["Read"]}
    assert removed["hooks"]["SubagentStart"] == [
        {
            "matcher": "Explore",
            "hooks": [{"type": "command", "command": "/existing"}],
        }
    ]
