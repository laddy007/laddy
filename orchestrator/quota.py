"""Quota-window handling (spec: quota-resume-queue).

Subscription rate-limits are recoverable, not fatal: a quota-classified
agent result makes the loop wait for the window reset and retry the SAME
step. All decisions are pure functions over injected time; the wrapper
below adds no policy of its own beyond wait_plan + QuotaBudget.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.agents import AgentResult, AgentRunner


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class QuotaTimeout(Exception):
    """Cumulative quota waiting exceeded the per-run budget."""


@dataclass(frozen=True)
class QuotaPolicy:
    """Waiting rules; defaults mirror the env knobs in config.py."""

    reset_buffer: timedelta = timedelta(seconds=120)
    backoff: tuple[timedelta, ...] = (
        timedelta(minutes=15),
        timedelta(minutes=30),
        timedelta(minutes=60),
    )
    max_wait: timedelta = timedelta(hours=30)


def wait_plan(
    reset_at: datetime | None, attempt: int, now: datetime, policy: QuotaPolicy
) -> timedelta:
    """How long to wait before the next attempt (pure)."""
    if reset_at is not None and reset_at > now:
        return (reset_at - now) + policy.reset_buffer
    return policy.backoff[min(attempt, len(policy.backoff) - 1)]


@dataclass
class QuotaBudget:
    """Cumulative wait budget shared by all runners of one task run."""

    limit: timedelta
    spent: timedelta = field(default_factory=timedelta)

    def charge(self, wait: timedelta) -> None:
        if self.spent + wait > self.limit:
            raise QuotaTimeout(
                f"quota wait budget exhausted ({self.spent + wait} > {self.limit})"
            )
        self.spent += wait


OnWait = Callable[[str, datetime | None, timedelta, int], None]
OnResume = Callable[[str, int], None]


class QuotaAwareRunner:
    """AgentRunner decorator: sleep through quota exhaustion, retry the step.

    bind() attaches the per-run budget and event callbacks; an unbound
    wrapper still waits (with its own unlimited budget semantics skipped --
    charge() is only called when a budget is bound).
    """

    def __init__(
        self,
        inner: AgentRunner,
        policy: QuotaPolicy,
        sleep_fn: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._inner = inner
        self.name = inner.name
        self._policy = policy
        self._sleep = sleep_fn
        self._now = now_fn
        self._budget: QuotaBudget | None = None
        self._on_wait: OnWait | None = None
        self._on_resume: OnResume | None = None

    def bind(self, budget: QuotaBudget, on_wait: OnWait, on_resume: OnResume) -> None:
        self._budget = budget
        self._on_wait = on_wait
        self._on_resume = on_resume

    def run(self, prompt: str, cwd: Path, resume: str | None = None) -> AgentResult:
        attempt = 0
        while True:
            result = self._inner.run(prompt, cwd, resume)
            if result.exit_reason != "quota":
                if attempt and self._on_resume is not None:
                    self._on_resume(self.name, attempt)
                return result
            wait = wait_plan(result.quota_reset_at, attempt, self._now(), self._policy)
            if self._budget is not None:
                self._budget.charge(wait)  # raises QuotaTimeout at the cap
            if self._on_wait is not None:
                self._on_wait(self.name, result.quota_reset_at, wait, attempt)
            self._sleep(wait.total_seconds())
            attempt += 1
