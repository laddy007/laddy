"""Append-only oracle run log + derived watermark and escape-rate series.

<agent-dir>/oracle/run-log.jsonl is the ONE new substrate of the oracle
design: iteration-log.jsonl is per-task, while an ``oracle-run`` event
spans a merge RANGE, so it gets its own file with the SAME pattern - one
JSON line per event, appended and never rewritten; state (watermark, the
escape-rate time series) is always derived by folding the log.

The oracle appends here post-merge and the file is committed directly to
local main (append-only history addition, no code change - within agent
commit authority; push stays with the Director).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from orchestrator import TARGET_DIR_NAME
from orchestrator.artifacts import append_jsonl, read_jsonl, utc_now

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


class _EscapeStatus(Protocol):
    """Structural view escape_rate_series needs of an escape record.

    runlog is the foundational log substrate: it must NOT import from
    escapes (that would cycle now that escapes anchors provenance here). It
    reads only these two fields off ``EscapeRecord``, so a Protocol suffices.
    """

    @property
    def flag_id(self) -> str: ...
    @property
    def status(self) -> str: ...


RUN_LOG_PATH = f"{TARGET_DIR_NAME}/oracle/run-log.jsonl"

# The two event tags of this log (convergence R2: one constant shared by
# writer and reader - a drifted literal makes the scanner silently return
# zero events).
ORACLE_RUN = "oracle-run"

# Blast-radius buckets (policy.L1/L2/L3) the per-bucket denominator uses.
BUCKETS = ("L1", "L2", "L3")


def run_log_path(repo_root: Path) -> Path:
    return repo_root / RUN_LOG_PATH


def append_run(
    repo_root: Path,
    *,
    from_sha: str,
    to_sha: str,
    reviewed: Mapping[str, Sequence[str]],
    skipped: Mapping[str, Sequence[str]],
    findings: Sequence[Mapping[str, Any]],
    mode: str = "calibration",
    now: Callable[[], str] | None = None,
) -> None:
    """Append exactly one ``oracle-run`` event (never rewrites).

    ``reviewed``/``skipped`` map bucket -> task ids: the honest denominator.
    Escape rate is reported over REVIEWED tasks per bucket, and the tasks a
    sample skipped are recorded in the same event, so the time series can
    never silently claim coverage it did not have.
    ``findings``: {"task", "flag_id", "grade"} per escape raised this run.
    """
    if not from_sha or not to_sha:
        raise ValueError("from_sha and to_sha are required (watermark integrity)")
    event = {
        "ts": (now or utc_now)(),
        "action": ORACLE_RUN,
        "from_sha": from_sha,
        "to_sha": to_sha,
        "mode": mode,
        "reviewed": {b: list(reviewed.get(b, [])) for b in BUCKETS},
        "skipped": {b: list(skipped.get(b, [])) for b in BUCKETS},
        "findings": [dict(f) for f in findings],
    }
    append_jsonl(run_log_path(repo_root), event)


def _read_events(repo_root: Path, action: str) -> list[dict[str, Any]]:
    """All events of one action; torn-final-line tolerant (artifacts.read_jsonl)."""
    return [
        entry
        for entry in read_jsonl(run_log_path(repo_root))
        if isinstance(entry, dict) and entry.get("action") == action
    ]


def read_runs(repo_root: Path) -> list[dict[str, Any]]:
    """All oracle-run events; torn-final-line tolerant (as artifacts.read_log)."""
    return _read_events(repo_root, ORACLE_RUN)


# Seeded-eval events share the run log: same file, same append-only +
# derive pattern; every reader folds by ``action``, so the watermark
# (oracle-run events only) is structurally unaffected. One oracle event
# log, not a second state file (converge, don't add).
SEEDED_EVAL = "seeded-eval"

# Closed outcome vocabulary of a seeded eval (convergence R2 - the enum
# lives with the event schema): caught = a gate flagged the seeded defect;
# missed = every judgment gate waved it through; inconclusive = the chain
# did not complete cleanly or blockers landed outside the seeded surface.
EVAL_RESULTS = ("caught", "missed", "inconclusive")


def append_eval(
    repo_root: Path,
    *,
    eval_id: str,
    class_slug: str,
    result: str,
    caught_by: Sequence[str],
    terminal: str,
    decision: str | None,
    fix_ref: str | None = None,
    now: Callable[[], str] | None = None,
) -> None:
    """Append exactly one ``seeded-eval`` event (never rewrites).

    ``fix_ref`` is the commit of the prompt/role fix being validated - the
    honesty chain "fix X validated by eval Y" lives in this event, and the
    escape flag's resolve note points back at it.
    """
    if not eval_id or not class_slug:
        raise ValueError("eval_id and class_slug are required")
    if result not in EVAL_RESULTS:
        raise ValueError(f"unknown result {result!r}; expected one of {EVAL_RESULTS}")
    event: dict[str, Any] = {
        "ts": (now or utc_now)(),
        "action": SEEDED_EVAL,
        "eval": eval_id,
        "class": class_slug,
        "result": result,
        "caught_by": list(caught_by),
        "terminal": terminal,
        "decision": decision,
    }
    if fix_ref:
        event["fix_ref"] = fix_ref
    append_jsonl(run_log_path(repo_root), event)


def read_evals(repo_root: Path) -> list[dict[str, Any]]:
    """All seeded-eval events, oldest first."""
    return _read_events(repo_root, SEEDED_EVAL)


# The oracle-escape PROVENANCE event: written at raise time through the
# single validated channel (escapes.raise_oracle_escape / the CLI ``escape``
# action) to the oracle-only run log. It is the AUTHORITATIVE record that an
# escape exists. The per-task iteration-log oracle-escape FLAG is
# branch-writable content (a merged branch can forge a raw flag line); this
# run log is not - it lives under the <agent-dir>/oracle/* L3 sensitive glob,
# so a branch touching it rides the risk lane, never L2 auto-merge. A
# task-log oracle-escape with no matching event here is forged and must not be
# counted (iter_escapes drops it). This event is NOT sourced from
# iter_escapes (that would be circular) - it is written by the validated raise.
ESCAPE_RAISED = "escape-raised"


def append_escape(
    repo_root: Path,
    *,
    task: str,
    flag_id: str,
    class_slug: str,
    grade: str,
    now: Callable[[], str] | None = None,
) -> None:
    """Append exactly one ``escape-raised`` provenance event (never rewrites).

    Called only by the validated raise (escapes.raise_oracle_escape), so an
    entry here vouches that the oracle - not a branch - raised this escape.
    """
    if not task or not flag_id:
        raise ValueError("task and flag_id are required (escape provenance)")
    event = {
        "ts": (now or utc_now)(),
        "action": ESCAPE_RAISED,
        "task": task,
        "flag_id": flag_id,
        "class": class_slug,
        "grade": grade,
    }
    append_jsonl(run_log_path(repo_root), event)


def read_escapes(repo_root: Path) -> list[dict[str, Any]]:
    """All escape-raised provenance events, oldest first."""
    return _read_events(repo_root, ESCAPE_RAISED)


def authentic_escape_ids(repo_root: Path) -> set[tuple[str, str]]:
    """(task, flag_id) pairs the oracle authored through the validated raise.

    The authenticity anchor iter_escapes cross-checks each task-log
    oracle-escape flag against: a flag whose (task, flag_id) is absent here is
    branch-forged and not a real escape.
    """
    return {
        (str(e.get("task")), str(e.get("flag_id"))) for e in read_escapes(repo_root)
    }


def watermark(repo_root: Path) -> str | None:
    """Last reviewed merge commit = ``to_sha`` of the latest run (derived)."""
    runs = read_runs(repo_root)
    if not runs:
        return None
    value = runs[-1].get("to_sha")
    return str(value) if value else None


def escape_rate_series(
    runs: Sequence[Mapping[str, Any]], records: Sequence[_EscapeStatus]
) -> list[dict[str, Any]]:
    """Per-run, per-bucket time series with the honest denominator (pure).

    escapes = findings graded confirmed, plus plausible ones the Director
    upheld (their flag ended resolved); pending = plausible still open;
    dismissed findings drop out. Grade is the run-time snapshot; status is
    the CURRENT flag state from ``records`` - so the series self-corrects
    as the Director adjudicates plausible findings.
    """
    status_by_flag = {r.flag_id: r.status for r in records}
    series: list[dict[str, Any]] = []
    for run in runs:
        reviewed = run.get("reviewed", {})
        skipped = run.get("skipped", {})
        findings = run.get("findings", [])
        for bucket in BUCKETS:
            tasks = list(reviewed.get(bucket, []))
            skip = list(skipped.get(bucket, []))
            if not tasks and not skip:
                continue
            escapes = pending = 0
            for finding in findings:
                if finding.get("task") not in tasks:
                    continue
                status = status_by_flag.get(str(finding.get("flag_id")), "open")
                if status == "dismissed":
                    continue
                if finding.get("grade") == "confirmed" or status == "resolved":
                    escapes += 1
                else:
                    pending += 1
            series.append({
                "ts": run.get("ts"),
                "to_sha": run.get("to_sha"),
                "bucket": bucket,
                "reviewed": len(tasks),
                "skipped": len(skip),
                "escapes": escapes,
                "pending": pending,
            })
    return series
