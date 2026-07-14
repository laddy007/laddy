"""When is an oracle run due - the AUTO half of the design's split:
the TRIGGER is automated (a forgettable notice would be the same bug that
caused the 7-bug blind spot), the RUN stays manual until output quality is
calibrated.

Due when, since the watermark: >= TRIGGER_TASK_COUNT agent merges landed,
OR any high-risk (L3) merge landed, OR TRIGGER_MAX_DAYS elapsed since the
last run - whichever comes first. Both thresholds are hypotheses tuned by
the oracle's own output (finds a lot -> tighten; long silence -> loosen).
No watermark at all = due: the oracle has never run; start it with an
explicit --since.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from orchestrator.oracle import commit_exists
from orchestrator.oracle.runlog import read_runs
from orchestrator.oracle.scope import merged_tasks_in_range
from orchestrator.policy import L3

if TYPE_CHECKING:
    from collections.abc import Callable

TRIGGER_TASK_COUNT = 5
TRIGGER_MAX_DAYS = 14.0


@dataclass(frozen=True)
class TriggerStatus:
    due: bool
    reasons: tuple[str, ...]
    watermark: str | None
    merges_since: int
    high_risk_since: bool
    days_since_last: float | None


def oracle_due(
    *,
    watermark_sha: str | None,
    merges_since: int,
    high_risk_since: bool,
    days_since_last: float | None,
) -> TriggerStatus:
    """Pure decision; callers gather the inputs."""
    if watermark_sha is None:
        return TriggerStatus(
            True,
            ("no oracle run recorded yet - start with an explicit --since <sha>",),
            None, merges_since, high_risk_since, days_since_last,
        )
    reasons: list[str] = []
    if merges_since >= TRIGGER_TASK_COUNT:
        reasons.append(
            f"{merges_since} agent merges since watermark (>= {TRIGGER_TASK_COUNT})"
        )
    if high_risk_since:
        reasons.append("a high-risk (L3) merge landed since the watermark")
    if days_since_last is not None and days_since_last >= TRIGGER_MAX_DAYS:
        reasons.append(
            f"{days_since_last:.0f} days since the last run (>= {TRIGGER_MAX_DAYS:.0f})"
        )
    return TriggerStatus(
        bool(reasons), tuple(reasons), watermark_sha,
        merges_since, high_risk_since, days_since_last,
    )


def check(repo: Path, now: Callable[[], datetime] | None = None) -> TriggerStatus:
    """Gather trigger inputs from the run log + git history, then decide."""
    # One parse of the run log: both the watermark (last run's to_sha) and
    # the last-run timestamp come from the same final event.
    runs = read_runs(repo)
    raw_wm = runs[-1].get("to_sha") if runs else None
    wm = str(raw_wm) if raw_wm else None
    if wm is None:
        return oracle_due(
            watermark_sha=None, merges_since=0,
            high_risk_since=False, days_since_last=None,
        )
    if not commit_exists(repo, wm):
        # git history and the run log disagree (reset + gc, fresh clone,
        # hand-edited log): surface it loudly - a raw git error here would
        # leave the automated trigger dead until someone notices.
        return TriggerStatus(
            True,
            (
                f"recorded watermark {wm[:12]} is not resolvable in this "
                "clone - re-baseline with record-run --since <sha> --to <sha>",
            ),
            wm, 0, False, None,
        )
    tasks = merged_tasks_in_range(repo, wm)
    days: float | None = None
    last_ts = runs[-1].get("ts")
    if isinstance(last_ts, str):
        try:
            last = datetime.strptime(last_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            last = None
        if last is not None:
            current = now() if now is not None else datetime.now(timezone.utc)
            days = (current - last).total_seconds() / 86400.0
    return oracle_due(
        watermark_sha=wm,
        merges_since=len(tasks),
        high_risk_since=any(t.bucket == L3 for t in tasks),
        days_since_last=days,
    )
