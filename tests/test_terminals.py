"""Terminal-state taxonomy (M2): one shared home for what a terminal MEANS.

The loop (_recorded_terminal), the status reporter (run._derive_status) and
the ntfy sentences must agree on which states are sticky, which publish the
branch, and which write a handback - this file pins that single source.
"""

from __future__ import annotations

from orchestrator.handoff import STATE_SENTENCES
from orchestrator.terminals import TERMINALS, terminal_spec


def test_transient_environment_states_are_retryable() -> None:
    # a re-kickoff after the cause is addressed (quota reset, network back)
    # must RESUME the task, not return the stale terminal forever
    assert not terminal_spec("QUOTA_TIMEOUT").sticky
    assert not terminal_spec("INTERNAL_ERROR").sticky


def test_success_and_failure_states_are_sticky() -> None:
    for state in (
        "PUSHED",
        "CAP_REACHED",
        "ESCALATED_DEADLOCK",
        "PATH_GUARD_VIOLATION",
        "INVESTIGATOR_MALFORMED",
        "VERIFY_MALFORMED",
    ):
        assert terminal_spec(state).sticky, state


def test_merge_decided_states_resolve_to_success() -> None:
    for suffix in ("auto_merge", "auto_merge_notify", "stop_before_merge"):
        spec = terminal_spec(f"MERGE_DECIDED:{suffix}")
        assert spec.kind == "success"
        assert spec.sticky


def test_failures_write_a_handback_success_does_not() -> None:
    assert not terminal_spec("PUSHED").handback
    assert terminal_spec("CAP_REACHED").handback
    assert terminal_spec("QUOTA_TIMEOUT").handback


def test_path_guard_violation_never_publishes_the_violating_tree() -> None:
    # the one failure that must NOT push: the branch contains source edits a
    # report-only task was forbidden to make
    assert not terminal_spec("PATH_GUARD_VIOLATION").push
    for state in ("CAP_REACHED", "ESCALATED_DEADLOCK", "QUOTA_TIMEOUT",
                  "INVESTIGATOR_MALFORMED", "VERIFY_MALFORMED", "PUSHED"):
        assert terminal_spec(state).push, state


def test_unknown_state_fails_safe_sticky_no_push() -> None:
    spec = terminal_spec("SOME_FUTURE_STATE")
    assert spec.sticky
    assert not spec.push


def test_every_terminal_has_an_ntfy_sentence() -> None:
    # drift-pin: a new terminal state without a notification sentence would
    # push the generic "run finished" - name it in STATE_SENTENCES
    missing = set(TERMINALS) - set(STATE_SENTENCES)
    assert not missing, f"terminals without ntfy sentence: {missing}"