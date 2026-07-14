from __future__ import annotations

import json

from loop_monitor.events import ActiveSubagents, sanitize_hook_payload


def test_hook_payload_drops_messages_transcripts_and_unknown_fields() -> None:
    secret = "sk-do-not-store"
    event = sanitize_hook_payload(
        {
            "hook_event_name": "SubagentStart",
            "session_id": "session-1",
            "agent_id": "agent-2",
            "agent_type": "Explore",
            "cwd": "/root/myapp-trusted",
            "transcript_path": f"/tmp/{secret}.jsonl",
            "last_assistant_message": f"password={secret}",
            "prompt": secret,
        },
        "claude",
        now=100,
    )

    assert event is not None
    assert event == {
        "source": "agent_hook",
        "vendor": "claude",
        "action": "start",
        "agent_id": "agent-2",
        "session_id": "session-1",
        "agent_type": "Explore",
        "observed_at": 100,
        "repo": "myapp-trusted",
    }
    assert secret not in json.dumps(event)


def test_active_subagents_fold_start_stop_and_expiry() -> None:
    active = ActiveSubagents(ttl_seconds=60)
    start: dict[str, object] = {
        "vendor": "codex",
        "session_id": "s",
        "agent_id": "a",
        "action": "start",
        "observed_at": 100,
    }
    active.apply(start)
    assert active.counts(now=120) == {
        "claude_subagents": 0,
        "codex_subagents": 1,
        "subagents_total": 1,
    }
    active.apply({**start, "action": "stop", "observed_at": 130})
    assert active.counts(now=130)["subagents_total"] == 0
    active.apply(start)
    assert active.counts(now=161)["subagents_total"] == 0


def test_active_subagents_drop_orphans_when_parent_vendor_has_no_session() -> None:
    active = ActiveSubagents(ttl_seconds=3600)
    active.apply(
        {
            "vendor": "claude",
            "session_id": "s",
            "agent_id": "a",
            "action": "start",
            "observed_at": 100,
        }
    )

    active.reconcile_sessions(claude_sessions=0, codex_sessions=1)

    assert active.counts(now=101)["subagents_total"] == 0


def test_hook_rejects_non_lifecycle_event() -> None:
    assert (
        sanitize_hook_payload(
            {"hook_event_name": "PreToolUse", "session_id": "s", "agent_id": "a"},
            "codex",
        )
        is None
    )
