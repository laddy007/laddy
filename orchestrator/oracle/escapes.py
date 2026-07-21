"""oracle-escape flags: validated raising + the derived escape ledger.

An escape is a defect the oracle found in SHIPPED code - a bug every gate
passed. It is recorded as a ``flag`` event (kind ``oracle-escape``) on the
already-merged task, so the open/resolve lifecycle, the Director channel
and log-folding all reuse orchestrator.flags (converge, don't add).
``detail`` is the raise-time payload as JSON (class slug, grade, evidence,
optional attribution); ``note`` is written at resolve time and carries the
fix + distillation reference (commit/test), per the flags.py schema.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from orchestrator import TARGET_DIR_NAME
from orchestrator.artifacts import ArtifactPathError, LogCorruptionError, TaskArtifacts
from orchestrator.flags import ORACLE_ESCAPE, derive_flags, raise_flag
from orchestrator.oracle.classes import CLASSES_PATH, load_class_slugs
from orchestrator.oracle.runlog import append_escape, authentic_escape_ids

if TYPE_CHECKING:
    from collections.abc import Sequence

# confirmed = reproduced (failing test / concrete wrong output) -> enters the
# escape rate automatically; plausible = concrete evidence without mechanical
# reproduction -> the Director adjudicates (dismisses if not real). A finding
# with neither is dropped before it ever reaches this module.
GRADES = ("confirmed", "plausible")

# Phase-2 attribution targets: the EARLIEST structural owner per the upgrade
# ladder ("who" axis of the design), not blame for a single reviewer.
ATTRIBUTION_GATES = ("test", "rw1", "rw2", "merge-rw", "coverage-gap", "dev-scaffold")

# A class is a confirmed upgrade target only with recurrence (>= N escapes in
# the ledger); a single finding is a hypothesis (anti-overfitting meta-rule).
RECURRENCE_THRESHOLD = 2


def raise_oracle_escape(
    art: TaskArtifacts,
    *,
    class_slug: str,
    grade: str,
    summary: str,
    evidence: str,
    gate: str | None = None,
    attribution_note: str | None = None,
) -> str:
    """Validated wrapper over flags.raise_flag for oracle escapes.

    Validates the slug against the committed registry (never free text),
    the grade, non-empty evidence and the optional attribution gate, then
    composes ``detail`` as the JSON payload the ledger folds. Attribution
    is optional: detail is raise-time-only (append-only log), so callers
    raise AFTER phase 2 when they have it, or omit it when inconclusive.
    """
    slugs = load_class_slugs()
    if class_slug not in slugs:
        raise ValueError(
            f"unregistered class slug {class_slug!r}; register it in "
            f"{CLASSES_PATH} first (existing: {', '.join(slugs) or 'none'})"
        )
    if grade not in GRADES:
        raise ValueError(f"unknown grade {grade!r}; expected one of {GRADES}")
    if not evidence or not evidence.strip():
        raise ValueError("evidence must not be empty (no vibes: repro or concrete evidence)")
    if gate is not None and gate not in ATTRIBUTION_GATES:
        raise ValueError(f"unknown gate {gate!r}; expected one of {ATTRIBUTION_GATES}")
    payload: dict[str, object] = {
        "class": class_slug,
        "grade": grade,
        "evidence": evidence,
    }
    if gate is not None:
        payload["attribution"] = {"gate": gate, "note": attribution_note or ""}
    flag_id = raise_flag(
        art,
        ORACLE_ESCAPE,
        summary,
        detail=json.dumps(payload, ensure_ascii=False),
        needs_director=True,
        # this wrapper IS the validated Director/oracle channel; raise_flag
        # refuses an oracle-escape from any other caller (library boundary)
        allow_oracle_escape=True,
    )
    # Anchor the escape's authenticity in the oracle-only run log: iter_escapes
    # counts only task-log oracle-escape flags that have a matching record
    # here. The task log is branch-writable (forgeable); this run log is not
    # (the <agent-dir>/oracle/* L3 sensitive glob). Writing it HERE - the
    # validated raise - and not from iter_escapes is what breaks the
    # record-run/iter_escapes circularity: the anchor never derives from the
    # thing it authenticates.
    append_escape(
        art.repo_root,
        task=art.task_id,
        flag_id=flag_id,
        class_slug=class_slug,
        grade=grade,
    )
    return flag_id


UNCLASSIFIED = "unclassified"  # ledger bucket for unparseable/missing payloads


@dataclass(frozen=True)
class EscapeRecord:
    """One oracle-escape flag with its parsed payload (None = unparseable)."""

    task_id: str
    flag_id: str
    class_slug: str | None
    grade: str | None
    status: str  # open | resolved | dismissed
    summary: str


def iter_escapes(repo_root: Path) -> list[EscapeRecord]:
    """Every AUTHENTIC oracle-escape flag across all tasks' committed logs.

    Reads <agent-dir>/tasks/*/iteration-log.jsonl in the working tree -
    post-merge those are on main, which is exactly the oracle's substrate.
    An unparseable ``detail`` degrades to class/grade None (folded into the
    UNCLASSIFIED ledger bucket), never a crash: the ledger is a reporter.

    A task-log oracle-escape flag is counted only when its (task, flag_id) has
    a matching provenance record in the oracle-only run log (written by the
    validated raise, ``runlog.authentic_escape_ids``). A flag with no such
    record is branch-forged content - the task log is branch-writable, the run
    log is not - and is dropped, so a forged line can neither poison the ledger
    nor trip a false RECURRENT.
    """
    tasks_dir = repo_root / TARGET_DIR_NAME / "tasks"
    records: list[EscapeRecord] = []
    if not tasks_dir.is_dir():
        return records
    authentic = authentic_escape_ids(repo_root)
    for task_dir in sorted(p for p in tasks_dir.iterdir() if p.is_dir()):
        art = TaskArtifacts(repo_root, task_dir.name)
        try:
            flags = derive_flags(art.read_log())
        except (LogCorruptionError, ArtifactPathError):
            # The oracle is a NON-BLOCKING reporter. read_jsonl now raises on an
            # interior-corrupt task log (S5), and a branch-forged task dir could
            # be a refused symlink (ArtifactPathError): a single poisoned log
            # must not break the ledger for every OTHER task. Skip it - a skipped
            # task counts as zero escapes, the conservative fail-safe direction
            # (never a spurious RECURRENT), and it changes no merge decision.
            continue
        for flag in flags:
            if flag.kind != ORACLE_ESCAPE:
                continue
            if (task_dir.name, flag.id) not in authentic:
                continue  # forged: no oracle-authored provenance record
            slug: str | None = None
            grade: str | None = None
            try:
                payload = json.loads(flag.detail or "")
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                raw_slug = payload.get("class")
                raw_grade = payload.get("grade")
                slug = raw_slug if isinstance(raw_slug, str) else None
                grade = raw_grade if isinstance(raw_grade, str) else None
            records.append(
                EscapeRecord(task_dir.name, flag.id, slug, grade, flag.status, flag.summary)
            )
    return records


@dataclass(frozen=True)
class LedgerEntry:
    """Per-class fold of the escape ledger (derived, never stored)."""

    class_slug: str
    total: int  # non-dismissed escapes in this class
    open: int
    recurrent: bool  # total >= RECURRENCE_THRESHOLD -> confirmed upgrade target


def derive_ledger(records: Sequence[EscapeRecord]) -> list[LedgerEntry]:
    """Fold escapes into per-class counts (pure). Dismissed = not an escape."""
    totals: dict[str, int] = {}
    opens: dict[str, int] = {}
    for record in records:
        if record.status == "dismissed":
            continue
        slug = record.class_slug or UNCLASSIFIED
        totals[slug] = totals.get(slug, 0) + 1
        if record.status == "open":
            opens[slug] = opens.get(slug, 0) + 1
    return [
        LedgerEntry(slug, total, opens.get(slug, 0), total >= RECURRENCE_THRESHOLD)
        for slug, total in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
