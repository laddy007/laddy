"""Unit tests for quota wait policy, budget, and the runner wrapper."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.agents import AgentResult
from orchestrator.quota import (
    QuotaAwareRunner,
    QuotaBudget,
    QuotaPolicy,
    QuotaTimeout,
    wait_plan,
)
from tests.fakes import FakeRunner

_NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
_POLICY = QuotaPolicy()


def _quota_result(reset_at: datetime | None = None) -> AgentResult:
    return AgentResult(
        text="usage limit reached",
        session_id=None,
        exit_reason="quota",
        returncode=1,
        quota_reset_at=reset_at,
    )


def test_wait_plan_known_reset_waits_until_reset_plus_buffer() -> None:
    reset = _NOW + timedelta(hours=3)
    assert wait_plan(reset, 0, _NOW, _POLICY) == timedelta(hours=3, seconds=120)


def test_wait_plan_past_reset_falls_back_to_backoff() -> None:
    assert wait_plan(_NOW - timedelta(minutes=5), 0, _NOW, _POLICY) == timedelta(minutes=15)


def test_wait_plan_unknown_reset_uses_backoff_schedule_then_repeats_last() -> None:
    assert wait_plan(None, 0, _NOW, _POLICY) == timedelta(minutes=15)
    assert wait_plan(None, 1, _NOW, _POLICY) == timedelta(minutes=30)
    assert wait_plan(None, 2, _NOW, _POLICY) == timedelta(minutes=60)
    assert wait_plan(None, 7, _NOW, _POLICY) == timedelta(minutes=60)


def test_budget_charges_until_limit_then_raises() -> None:
    budget = QuotaBudget(limit=timedelta(hours=1))
    budget.charge(timedelta(minutes=40))
    with pytest.raises(QuotaTimeout):
        budget.charge(timedelta(minutes=30))


def test_wrapper_waits_and_retries_then_returns_ok() -> None:
    inner = FakeRunner([_quota_result(), _quota_result(), "done"])
    sleeps: list[float] = []
    waits: list[tuple[str, timedelta, int]] = []
    resumes: list[int] = []
    runner = QuotaAwareRunner(
        inner, _POLICY, sleep_fn=sleeps.append, now_fn=lambda: _NOW
    )
    runner.bind(
        QuotaBudget(_POLICY.max_wait),
        on_wait=lambda role, reset, wait, attempt: waits.append((role, wait, attempt)),
        on_resume=lambda role, attempts: resumes.append(attempts),
    )
    result = runner.run("p", Path("."))
    assert result.exit_reason == "ok"
    assert result.text == "done"
    assert sleeps == [15 * 60.0, 30 * 60.0]
    assert [w[2] for w in waits] == [0, 1]
    assert resumes == [2]
    assert len(inner.calls) == 3


def test_wrapper_passthrough_without_quota() -> None:
    inner = FakeRunner(["fine"])
    sleeps: list[float] = []
    runner = QuotaAwareRunner(inner, _POLICY, sleep_fn=sleeps.append, now_fn=lambda: _NOW)
    result = runner.run("p", Path("."))
    assert result.exit_reason == "ok"
    assert sleeps == []


def test_wrapper_budget_exhaustion_raises_quota_timeout() -> None:
    inner = FakeRunner([_quota_result(), _quota_result(), _quota_result()])
    policy = QuotaPolicy(max_wait=timedelta(minutes=40))
    runner = QuotaAwareRunner(inner, policy, sleep_fn=lambda s: None, now_fn=lambda: _NOW)
    runner.bind(QuotaBudget(policy.max_wait), on_wait=lambda *a: None, on_resume=lambda *a: None)
    with pytest.raises(QuotaTimeout):
        runner.run("p", Path("."))  # 15 + 30 > 40 min
