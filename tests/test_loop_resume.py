"""Tests for the pure log-replay functions: resume derivation and the
SHA-keyed gate states (both replay the append-only iteration log)."""

from __future__ import annotations

from typing import Any

from orchestrator.loop import ResumePoint, derive_resume_point, gate_states_from_log


def _e(action: str, outcome: str, **extra: Any) -> dict[str, Any]:
    return {"ts": "t", "action": action, "outcome": outcome, **extra}


def test_empty_log_starts_round_one_developer() -> None:
    rp = derive_resume_point([], max_loops=4)
    assert rp == ResumePoint(
        round=1, phase="developer", dev_session=None, rw1_session=None, rw2_session=None
    )


def test_after_developer_ok_next_is_fast_tests_same_round() -> None:
    rp = derive_resume_point([_e("developer", "ok", session_id="d1")], max_loops=4)
    assert rp.phase == "fast_tests"
    assert rp.round == 1
    assert rp.dev_session == "d1"


def test_after_fast_tests_fail_next_is_developer_next_round() -> None:
    entries = [_e("developer", "ok", session_id="d1"), _e("fast_tests", "fail")]
    rp = derive_resume_point(entries, max_loops=4)
    assert rp.phase == "developer"
    assert rp.round == 2
    assert rp.dev_session == "d1"


def test_after_fast_tests_pass_next_is_rw1() -> None:
    entries = [_e("developer", "ok", session_id="d1"), _e("fast_tests", "pass")]
    rp = derive_resume_point(entries, max_loops=4)
    assert rp.phase == "rw1"
    assert rp.round == 1


def test_after_rw1_changes_requested_next_is_developer() -> None:
    entries = [
        _e("developer", "ok", session_id="d1"),
        _e("fast_tests", "pass"),
        _e("rw1", "changes_requested", session_id="r1"),
    ]
    rp = derive_resume_point(entries, max_loops=4)
    assert rp.phase == "developer"
    assert rp.round == 2
    assert rp.rw1_session == "r1"


def test_after_rw1_approved_next_is_push() -> None:
    entries = [
        _e("developer", "ok", session_id="d1"),
        _e("fast_tests", "pass"),
        _e("rw1", "approved", session_id="r1"),
    ]
    assert derive_resume_point(entries, max_loops=4).phase == "push"


def test_after_push_done() -> None:
    entries = [
        _e("developer", "ok"),
        _e("fast_tests", "pass"),
        _e("rw1", "approved"),
        _e("push", "ok"),
    ]
    assert derive_resume_point(entries, max_loops=4).phase == "done"


def test_cap_reached_when_next_developer_round_exceeds_max() -> None:
    entries = []
    for _ in range(4):
        entries.append(_e("developer", "ok"))
        entries.append(_e("fast_tests", "fail"))
    rp = derive_resume_point(entries, max_loops=4)
    assert rp.phase == "cap_reached"


def test_cap_not_reached_when_last_round_still_in_review() -> None:
    entries = []
    for _ in range(3):
        entries.append(_e("developer", "ok"))
        entries.append(_e("fast_tests", "fail"))
    entries.append(_e("developer", "ok"))
    rp = derive_resume_point(entries, max_loops=4)
    assert rp.phase == "fast_tests"
    assert rp.round == 4


def test_clarify_entries_are_ignored_for_phase() -> None:
    entries = [_e("clarify", "no_questions")]
    rp = derive_resume_point(entries, max_loops=4)
    assert rp.phase == "developer"
    assert rp.round == 1


# --- rw2 / authoritative / senior phases (slice-2 composition) ---------------

_APPROVED_PREFIX = [
    _e("developer", "ok", session_id="d1"),
    _e("fast_tests", "pass"),
    _e("rw1", "approved", session_id="r1"),
]


def test_with_rw2_after_rw1_approved_next_is_rw2() -> None:
    rp = derive_resume_point(_APPROVED_PREFIX, max_loops=4, with_rw2=True)
    assert rp.phase == "rw2"
    assert rp.round == 1


def test_without_rw2_but_with_authoritative_rw1_approved_goes_to_authoritative() -> None:
    rp = derive_resume_point(_APPROVED_PREFIX, max_loops=4, with_authoritative=True)
    assert rp.phase == "authoritative"


def test_after_rw2_go_next_is_authoritative_when_gate_present() -> None:
    entries = [*_APPROVED_PREFIX, _e("rw2", "go", session_id="g1")]
    rp = derive_resume_point(entries, max_loops=4, with_rw2=True, with_authoritative=True)
    assert rp.phase == "authoritative"
    assert rp.rw2_session == "g1"


def test_after_rw2_go_without_gate_next_is_push() -> None:
    entries = [*_APPROVED_PREFIX, _e("rw2", "go")]
    assert derive_resume_point(entries, max_loops=4, with_rw2=True).phase == "push"


def test_after_rw2_nogo_next_is_developer_next_round() -> None:
    entries = [*_APPROVED_PREFIX, _e("rw2", "nogo", session_id="g1")]
    rp = derive_resume_point(entries, max_loops=4, with_rw2=True)
    assert rp.phase == "developer"
    assert rp.round == 2
    assert rp.rw2_session == "g1"


def test_after_rw2_malformed_next_is_developer() -> None:
    entries = [*_APPROVED_PREFIX, _e("rw2", "malformed")]
    rp = derive_resume_point(entries, max_loops=4, with_rw2=True)
    assert rp.phase == "developer"


def test_after_authoritative_pass_next_is_push() -> None:
    entries = [*_APPROVED_PREFIX, _e("rw2", "go"), _e("authoritative", "pass")]
    rp = derive_resume_point(entries, max_loops=4, with_rw2=True, with_authoritative=True)
    assert rp.phase == "push"


def test_after_authoritative_fail_next_is_developer_next_round() -> None:
    entries = [*_APPROVED_PREFIX, _e("rw2", "go"), _e("authoritative", "fail")]
    rp = derive_resume_point(entries, max_loops=4, with_rw2=True, with_authoritative=True)
    assert rp.phase == "developer"
    assert rp.round == 2


def test_after_senior_approved_next_is_authoritative_when_gate_present() -> None:
    entries = [*_APPROVED_PREFIX, _e("rw2", "nogo"), _e("senior", "approved")]
    rp = derive_resume_point(entries, max_loops=4, with_rw2=True, with_authoritative=True)
    assert rp.phase == "authoritative"


def test_after_senior_approved_without_gate_next_is_push() -> None:
    entries = [*_APPROVED_PREFIX, _e("rw2", "nogo"), _e("senior", "approved")]
    assert derive_resume_point(entries, max_loops=4, with_rw2=True).phase == "push"


def test_after_senior_changes_requested_next_is_developer() -> None:
    entries = [*_APPROVED_PREFIX, _e("rw2", "nogo"), _e("senior", "changes_requested")]
    rp = derive_resume_point(entries, max_loops=4, with_rw2=True)
    assert rp.phase == "developer"
    assert rp.round == 2


def test_after_senior_deadlock_next_is_deadlock() -> None:
    entries = [*_APPROVED_PREFIX, _e("rw2", "nogo"), _e("senior", "deadlock")]
    rp = derive_resume_point(entries, max_loops=4, with_rw2=True)
    assert rp.phase == "deadlock"


def test_sessions_survive_multiple_rounds() -> None:
    entries = [
        _e("clarify", "answered"),
        _e("developer", "ok", session_id="d1"),
        _e("fast_tests", "pass"),
        _e("rw1", "changes_requested", session_id="r1"),
        _e("developer", "ok", session_id="d1"),
        _e("fast_tests", "pass"),
    ]
    rp = derive_resume_point(entries, max_loops=4)
    assert rp.phase == "rw1"
    assert rp.round == 2
    assert rp.dev_session == "d1"
    assert rp.rw1_session == "r1"


# --- gate_states_from_log: SHA-keyed replay incl. the senior override ---------


def test_gate_states_replay_basic_shas_and_outcomes() -> None:
    entries = [
        _e("rw1", "approved", sha="s1"),
        _e("rw2", "go", sha="s1"),
        _e("authoritative", "pass", sha="s1", flaky=True),
    ]
    gates = gate_states_from_log(entries, "s1")
    assert gates.rw1_sha == "s1" and gates.rw1_approved
    assert gates.rw2_sha == "s1" and gates.rw2_go
    assert gates.authoritative_sha == "s1" and gates.authoritative_passed
    assert gates.authoritative_flaky


def test_senior_approval_overrides_rw2_nogo_with_senior_sha() -> None:
    entries = [
        _e("rw1", "approved", sha="s1"),
        _e("rw2", "nogo", sha="s1"),
        _e("senior", "approved", sha="s2"),
        _e("authoritative", "pass", sha="s2"),
    ]
    gates = gate_states_from_log(entries, "s2")
    assert gates.rw2_go
    assert gates.rw2_sha == "s2"  # the override is keyed to the senior's SHA
    assert gates.rw1_approved
    assert gates.rw1_sha == "s1"  # rw1 was already ok; its own approval stands


def test_senior_approval_overrides_rw1_changes_requested() -> None:
    entries = [
        _e("rw1", "changes_requested", sha="s1"),
        _e("senior", "approved", sha="s1"),
    ]
    gates = gate_states_from_log(entries, "s1", require_rw2=False, require_authoritative=False)
    assert gates.rw1_approved
    assert gates.rw1_sha == "s1"


def test_senior_changes_requested_overrides_nothing() -> None:
    entries = [
        _e("rw1", "approved", sha="s1"),
        _e("rw2", "nogo", sha="s1"),
        _e("senior", "changes_requested", sha="s1"),
    ]
    gates = gate_states_from_log(entries, "s1")
    assert not gates.rw2_go


def test_fresh_rw2_review_after_senior_override_supersedes_it() -> None:
    # the override stands in for the DISPUTED review; a later fresh rw2 nogo
    # (new round, new code) must win over the stale senior approval
    entries = [
        _e("rw2", "nogo", sha="s1"),
        _e("senior", "approved", sha="s1"),
        _e("rw2", "nogo", sha="s2"),
    ]
    gates = gate_states_from_log(entries, "s2", require_authoritative=False)
    assert not gates.rw2_go
    assert gates.rw2_sha == "s2"
