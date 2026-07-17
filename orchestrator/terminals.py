"""Terminal-state taxonomy (M2): one home for what a terminal MEANS.

Three consumers must agree on the semantics of a recorded terminal, and
each used to hardcode its own subset:

- ``loop._recorded_terminal`` - which states are STICKY (replay-idempotent:
  a re-kickoff returns the recorded state) vs RETRYABLE (a transient
  environment condition; a re-kickoff resumes the task),
- ``loop._finalize`` - whether the terminal tail publishes the branch to
  the hub and whether it writes a handback,
- ``run._derive_status`` - whether a terminal marker reads as ``pushed``
  or ``failed:<state>``.

``MERGE_DECIDED:<decision>`` states share the PUSHED spec (a decided push
is a successful push). Unknown states fail safe: sticky, no push - a log
written by a newer engine must not make an older one loop or publish.
"""

from __future__ import annotations

from dataclasses import dataclass

_MERGE_DECIDED_PREFIX = "MERGE_DECIDED:"


@dataclass(frozen=True)
class TerminalSpec:
    kind: str  # "success" | "failure" | "retryable"
    push: bool
    handback: bool

    @property
    def sticky(self) -> bool:
        return self.kind != "retryable"


TERMINALS: dict[str, TerminalSpec] = {
    # deliverable on the hub; done
    "PUSHED": TerminalSpec("success", push=True, handback=False),
    # stopped for a human; the handback IS the deliverable, so it must
    # reach the hub too (a rebuilt disposable VPS holds nothing local)
    "CAP_REACHED": TerminalSpec("failure", push=True, handback=True),
    "ESCALATED_DEADLOCK": TerminalSpec("failure", push=True, handback=True),
    "INVESTIGATOR_MALFORMED": TerminalSpec("failure", push=True, handback=True),
    "VERIFY_MALFORMED": TerminalSpec("failure", push=True, handback=True),
    # the one failure that must NOT push: the branch carries source edits a
    # report-only task was forbidden to make - never publish the violating tree
    "PATH_GUARD_VIOLATION": TerminalSpec("failure", push=False, handback=True),
    # transient environment conditions: the record stays in the log, but a
    # re-kickoff resumes (QuotaBudget is per-run; a git blip may have passed)
    "QUOTA_TIMEOUT": TerminalSpec("retryable", push=True, handback=True),
    "INTERNAL_ERROR": TerminalSpec("retryable", push=False, handback=True),
}

_UNKNOWN = TerminalSpec("failure", push=False, handback=True)


def terminal_spec(state: str) -> TerminalSpec:
    if state.startswith(_MERGE_DECIDED_PREFIX):
        return TERMINALS["PUSHED"]
    return TERMINALS.get(state, _UNKNOWN)


# --- Resume table (director-resume): one home for "which log action un-sticks
# which terminal". A log event NEWER than a sticky terminal re-arms iteration
# when it appears here for that state; loop._recorded_terminal is the ONLY
# consumer and reads this table rather than hardcoding any event name (the next
# two consumers, cap_override and rw3, become rows here, not new `if`s).

# Sentinel standing for "clears any MERGE_DECIDED:<decision>". Reuses the same
# prefix constant terminal_spec keys on, so the prefix rule lives in one place.
MERGE_DECIDED_ANY = _MERGE_DECIDED_PREFIX

RESUMES: dict[str, frozenset[str]] = {
    # The Director's explicit resume channel un-sticks every finished terminal
    # EXCEPT PATH_GUARD_VIOLATION (a poisoned tree - discard, do not continue).
    "director_resume": frozenset(
        {"CAP_REACHED", "ESCALATED_DEADLOCK", "PUSHED", MERGE_DECIDED_ANY}
    ),
    # Rows added by their own specs as they land (director-resume is the seam):
    # "cap_override": frozenset({"CAP_REACHED"}),
    # "rw3":          frozenset({"PUSHED", MERGE_DECIDED_ANY}),
}


def clears_terminal(action: str, terminal: str) -> bool:
    """Does a log ``action`` un-stick a recorded ``terminal`` state? (pure)

    The single place that knows the ``MERGE_DECIDED:*`` prefix rule for the
    resume table: a table entry may name the ``MERGE_DECIDED_ANY`` sentinel to
    clear every decided-push suffix, or an exact terminal state for the rest.
    """
    states = RESUMES.get(action, frozenset())
    if terminal in states:
        return True
    return terminal.startswith(_MERGE_DECIDED_PREFIX) and MERGE_DECIDED_ANY in states