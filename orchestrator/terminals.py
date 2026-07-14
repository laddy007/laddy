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