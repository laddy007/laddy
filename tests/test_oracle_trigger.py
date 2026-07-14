"""Tests for the oracle trigger (orchestrator.oracle.trigger)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator import TARGET_DIR_NAME
from orchestrator.oracle.runlog import append_run
from orchestrator.oracle.trigger import (
    TRIGGER_MAX_DAYS,
    TRIGGER_TASK_COUNT,
    check,
    oracle_due,
)
from tests.fakes import git, init_repo, merge_agent_task

# --- pure decision ------------------------------------------------------------


def _status(**kw: Any):
    defaults: dict[str, Any] = {
        "watermark_sha": "abc",
        "merges_since": 0,
        "high_risk_since": False,
        "days_since_last": 1.0,
    }
    defaults.update(kw)
    return oracle_due(**defaults)


def test_quiet_period_is_not_due() -> None:
    status = _status()
    assert status.due is False and status.reasons == ()


def test_no_watermark_is_due() -> None:
    status = _status(watermark_sha=None)
    assert status.due is True
    assert "no oracle run recorded" in status.reasons[0]


def test_task_count_high_risk_and_age_each_trigger() -> None:
    assert _status(merges_since=TRIGGER_TASK_COUNT).due is True
    assert _status(high_risk_since=True).due is True
    assert _status(days_since_last=TRIGGER_MAX_DAYS).due is True
    both = _status(merges_since=TRIGGER_TASK_COUNT, high_risk_since=True)
    assert len(both.reasons) == 2  # every firing reason is named


# --- gatherer ------------------------------------------------------------------


def _record_first_run(repo: Path, ts: str = "2026-07-12T10:00:00Z") -> None:
    start = git(repo, "rev-parse", "HEAD~0")  # current main
    append_run(repo, from_sha="seed", to_sha=start,
               reviewed={}, skipped={}, findings=[], now=lambda: ts)


def test_check_counts_merges_and_high_risk(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    _record_first_run(repo)
    now = lambda: datetime(2026, 7, 13, tzinfo=timezone.utc)  # noqa: E731
    assert check(repo, now=now).due is False

    merge_agent_task(repo, "t-sensitive", {"myapp/models.py": "# change\n"})
    status = check(repo, now=now)
    assert status.due is True and status.high_risk_since is True
    assert status.merges_since == 1


def test_report_only_merge_does_not_fire_high_risk_trigger(tmp_path: Path) -> None:
    # An artifact-only merge (report task: everything under
    # <agent-dir>/tasks/ is pathspec-excluded) has an empty product diff;
    # the fail-closed L3 for () must not report "a high-risk merge landed"
    # after every report task.
    repo = init_repo(tmp_path / "repo")
    _record_first_run(repo)
    # commit the run log on main (as production does) so merge_agent_task's
    # `add -A` does not sweep it onto the agent branch as a product change
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "oracle: run log")
    merge_agent_task(repo, "t-report", {
        f"{TARGET_DIR_NAME}/tasks/t-report/report.md": "# findings\n",
    })
    now = lambda: datetime(2026, 7, 13, tzinfo=timezone.utc)  # noqa: E731
    status = check(repo, now=now)
    assert status.high_risk_since is False
    assert status.merges_since == 0
    assert status.due is False


def test_check_fires_on_age_alone(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    _record_first_run(repo, ts="2026-01-01T00:00:00Z")
    status = check(repo, now=lambda: datetime(2026, 7, 12, tzinfo=timezone.utc))
    assert status.due is True
    assert status.days_since_last is not None and status.days_since_last > 100


def test_check_without_any_run_is_due(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    assert check(repo).due is True


def test_check_with_unresolvable_watermark_is_due_with_actionable_reason(
    tmp_path: Path,
) -> None:
    # A recorded to_sha can stop resolving (reset + gc, a fresh clone, a
    # hand-edited log). check() must surface that loudly instead of dying
    # with a raw RuntimeError from git log - the automated trigger would
    # otherwise stay dead until someone edits the append-only run log.
    repo = init_repo(tmp_path / "repo")
    append_run(repo, from_sha="seed", to_sha="deadbeef" * 5,
               reviewed={}, skipped={}, findings=[],
               now=lambda: "2026-07-12T10:00:00Z")
    status = check(repo)
    assert status.due is True
    assert "not resolvable" in status.reasons[0]


# --- local_merge notice ---------------------------------------------------------


def test_local_merge_notice_prints_due_and_never_raises(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    from orchestrator import local_merge
    from orchestrator.oracle import trigger as trigger_mod
    from orchestrator.oracle.trigger import TriggerStatus

    due = TriggerStatus(True, ("5 agent merges since watermark",), "abc", 5, False, 1.0)
    monkeypatch.setattr(trigger_mod, "check", lambda repo: due)
    local_merge._oracle_notice(tmp_path)
    out = capsys.readouterr().out
    assert "[oracle] review DUE" in out and "orchestrator.oracle" in out

    def boom(repo: Path) -> TriggerStatus:
        raise RuntimeError("git exploded")

    monkeypatch.setattr(trigger_mod, "check", boom)
    local_merge._oracle_notice(tmp_path)  # must not raise
    assert "trigger check failed" in capsys.readouterr().out
