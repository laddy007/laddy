"""Typed, event-sourced flags on a task (spec task-flags).

A flag is a channel for things that must reach the Director: a deviation
from plan to approve, a consciously deferred debt, a blocker, a question,
a minor note. It is NOT a mutable record - it is a pair of append-only
events in the existing durable ``iteration-log.jsonl``:

  * ``action="flag"``          - the flag is raised (opens it)
  * ``action="flag-resolved"`` - the flag is resolved or dismissed

The current state of a flag is never stored; it is *derived* from the log
by :func:`derive_flags`, exactly as the task status is derived from the
log. One fact, one home. Durability (surviving an OOM / SSH drop) comes
from the immediate on-disk append of ``TaskArtifacts.append_log`` - a flag
written before a crash is safely on disk.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from orchestrator.artifacts import TaskArtifacts

# Closed sets - a flag's kind and a resolution's outcome are enumerations,
# not free text (convergence R2: no structured data in a discriminator).
# "oracle-escape" is raised post-merge by orchestrator.oracle.escapes (a
# defect the oracle found in SHIPPED code); its detail is a JSON payload
# and its resolve-time note carries the fix + distillation reference.
ORACLE_ESCAPE = "oracle-escape"

FLAG_KINDS = ("deviation", "debt", "blocker", "question", "note", ORACLE_ESCAPE)

# The kinds the LOOP may raise (run.py --kind choices). An oracle-escape
# enters solely through the validated Director channel
# (oracle.escapes.raise_oracle_escape: registered class slug, grade,
# evidence, JSON detail) - the system under measurement must never write
# to the measuring instrument's data series.
LOOP_FLAG_KINDS = tuple(k for k in FLAG_KINDS if k != ORACLE_ESCAPE)

FLAG_RESOLUTIONS = ("resolved", "dismissed")

# The two flag event actions. Consumed by run.py's ``_derive_status`` (as part
# of its non-progress set: a flag must not flip a still-ready task to
# in-progress) and here to fold the log.
FLAG_ACTIONS = ("flag", "flag-resolved")


@dataclass(frozen=True)
class Flag:
    """Derived view of one flag: its raising event plus any resolution.

    ``resolution``/``note``/``resolved_ts`` are ``None`` until the flag is
    resolved; ``status`` is derived - ``open`` while unresolved, otherwise the
    resolution (``resolved`` or ``dismissed``).
    """

    id: str
    kind: str
    summary: str
    detail: str | None
    needs_director: bool
    round: int | None
    raised_ts: str | None
    resolution: str | None
    note: str | None
    resolved_ts: str | None

    @property
    def status(self) -> str:
        return self.resolution or "open"


def derive_flags(entries: Sequence[Mapping[str, Any]]) -> list[Flag]:
    """Fold the log into the current flags (pure, no I/O).

    A ``flag`` event opens a flag (status ``open``); the matching
    ``flag-resolved`` event sets the resolution, plus ``note`` and
    ``resolved_ts``. Order = order of raising. A ``flag-resolved`` for an
    unknown or already-resolved ``id`` is ignored defensively (you cannot
    resolve what was never raised).
    """
    by_id: dict[str, Flag] = {}
    for entry in entries:
        action = entry.get("action")
        if action == "flag":
            flag_id = entry.get("id")
            if not isinstance(flag_id, str) or flag_id in by_id:
                continue
            by_id[flag_id] = Flag(
                id=flag_id,
                kind=str(entry.get("kind", "")),
                summary=str(entry.get("summary", "")),
                detail=entry.get("detail"),
                needs_director=bool(entry.get("needs_director", False)),
                round=entry.get("round"),
                raised_ts=entry.get("ts"),
                resolution=None,
                note=None,
                resolved_ts=None,
            )
        elif action == "flag-resolved":
            flag_id = entry.get("id")
            if not isinstance(flag_id, str):
                continue
            current = by_id.get(flag_id)
            if current is None or current.status != "open":
                continue  # unknown or already-resolved id -> ignore
            by_id[flag_id] = replace(
                current,
                resolution=str(entry.get("resolution", "resolved")),
                note=entry.get("note"),
                resolved_ts=entry.get("ts"),
            )
    # dict preserves insertion order and each id is inserted exactly once, so
    # this is raise order.
    return list(by_id.values())


def open_flags(entries: Sequence[Mapping[str, Any]]) -> list[Flag]:
    """Open flags only, ``needs_director`` first, else raise order (stable)."""
    flags = [f for f in derive_flags(entries) if f.status == "open"]
    return sorted(flags, key=lambda f: not f.needs_director)


def raise_flag(
    art: TaskArtifacts,
    kind: str,
    summary: str,
    *,
    detail: str | None = None,
    round: int | None = None,
    needs_director: bool = False,
    allow_oracle_escape: bool = False,
) -> str:
    """Append a ``flag`` event and return its assigned ``id``.

    ``id`` is ``"<task>#N"`` where N = 1 + the number of ``flag`` events
    already in the log - deterministic (no random/uuid) and resume-safe.
    The count-then-append runs under ``TaskArtifacts.log_lock`` so concurrent
    raises on the same task cannot collide on ``id``.

    An oracle-escape is refused unless ``allow_oracle_escape`` (the validated
    Director/oracle channel, ``oracle.escapes.raise_oracle_escape``): the
    system under measurement must never write to the measuring instrument's
    data series, and the CLI's narrower ``--kind`` choices only guard the
    argparse layer - the library boundary enforces it itself.
    """
    if kind == ORACLE_ESCAPE and not allow_oracle_escape:
        raise ValueError(
            f"{ORACLE_ESCAPE} is raised only through the validated oracle "
            "channel (oracle.escapes.raise_oracle_escape), never directly"
        )
    if kind not in FLAG_KINDS:
        raise ValueError(f"unknown flag kind {kind!r}; expected one of {FLAG_KINDS}")
    if not summary or not summary.strip():
        raise ValueError("flag summary must not be empty")
    with art.log_lock():
        raised = sum(1 for e in art.read_log() if e.get("action") == "flag")
        flag_id = f"{art.task_id}#{raised + 1}"
        fields: dict[str, Any] = {
            "action": "flag",
            "id": flag_id,
            "kind": kind,
            "summary": summary,
            "needs_director": needs_director,
        }
        if detail:
            fields["detail"] = detail
        if round is not None:
            fields["round"] = round
        art.append_log(**fields)
    return flag_id


def resolve_flag(
    art: TaskArtifacts,
    flag_id: str,
    *,
    resolution: str = "resolved",
    note: str | None = None,
    allow_oracle_escape: bool = False,
) -> bool:
    """Resolve an open flag. Returns ``True`` on success.

    Verifies ``flag_id`` is currently ``open`` (via :func:`derive_flags`);
    on an unknown or already-resolved id returns ``False`` and writes
    nothing. On success appends one ``flag-resolved`` event.

    An oracle-escape is refused unless ``allow_oracle_escape`` (the
    Director channel, ``python -m orchestrator.oracle resolve``): a
    dismissal drops the escape from the ledger, so it must never be
    reachable from inside the loop.
    """
    if resolution not in FLAG_RESOLUTIONS:
        raise ValueError(
            f"unknown resolution {resolution!r}; expected one of {FLAG_RESOLUTIONS}"
        )
    # No log => no flags to resolve. Short-circuit BEFORE taking the lock so we
    # never create (via the lock's O_CREAT) an empty iteration-log.jsonl that a
    # later commit_all would sweep into the branch.
    if not art.log_path.is_file():
        return False
    with art.log_lock():
        current = next(
            (f for f in derive_flags(art.read_log()) if f.id == flag_id), None
        )
        if current is None or current.status != "open":
            return False
        if current.kind == ORACLE_ESCAPE and not allow_oracle_escape:
            raise ValueError(
                f"{flag_id} is an oracle-escape: only the Director resolves "
                "it, via `python -m orchestrator.oracle resolve`"
            )
        fields: dict[str, Any] = {
            "action": "flag-resolved",
            "id": flag_id,
            "resolution": resolution,
        }
        if note:
            fields["note"] = note
        art.append_log(**fields)
    return True
