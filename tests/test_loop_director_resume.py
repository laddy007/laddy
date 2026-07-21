"""Director resume mechanism (director-resume): the shared un-stick primitive
(_recorded_terminal + terminals.RESUMES) and the Director note that reaches the
first developer round after a resume. Pure log-replay over fake entries, plus
one integration round for the note delivery."""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path
from typing import Any

import pytest

from orchestrator import TARGET_DIR_NAME
from orchestrator.artifacts import RW1_VERDICT, TaskArtifacts
from orchestrator.gitops import GitOps
from orchestrator.loop import (
    Orchestrator,
    _director_note,
    _recorded_terminal,
    _resume_developer_point,
    derive_resume_point,
    last_terminal_state,
)
from tests.fakes import FakeRunner, FakeShell, blocker, verdict_json, write_policy_toml


def _e(action: str, outcome: str, **extra: Any) -> dict[str, Any]:
    return {"ts": "t", "action": action, "outcome": outcome, **extra}


def _resume(reason: str = "spec was wrong; fixed it") -> dict[str, Any]:
    return {"ts": "t", "action": "director_resume", "outcome": "ok", "reason": reason}


# --- AC1: a newer director_resume un-sticks every clearable terminal ----------

RESUMABLE_TERMINALS = [
    "CAP_REACHED",
    "ESCALATED_DEADLOCK",
    "PUSHED",
    "MERGE_DECIDED:stop_before_merge",
]


@pytest.mark.parametrize("state", RESUMABLE_TERMINALS)
def test_newer_director_resume_unsticks_terminal(state: str) -> None:
    entries = [_e("developer", "ok"), _e("terminal", state), _resume()]
    assert _recorded_terminal(entries) is None
    # without the resume event the terminal is sticky (today's behaviour)
    assert _recorded_terminal(entries[:-1]) == state


def test_newer_director_resume_unsticks_bare_push_ok() -> None:
    # AC1: the un-stick must wrap the push:ok branch too, not just `terminal`
    entries = [_e("developer", "ok"), _e("push", "ok"), _resume()]
    assert _recorded_terminal(entries) is None
    assert _recorded_terminal(entries[:-1]) == "PUSHED"


# --- AC2: order is load-bearing; one event buys one run -----------------------


def test_director_resume_older_than_terminal_does_not_unstick() -> None:
    # event BEFORE the terminal -> the terminal is newer -> still sticky
    entries = [_resume(), _e("developer", "ok"), _e("terminal", "CAP_REACHED")]
    assert _recorded_terminal(entries) == "CAP_REACHED"


def test_resumed_run_that_hits_a_new_terminal_is_sticky_again() -> None:
    # [terminal, resume, ...work..., terminal] -> the SECOND terminal has no
    # newer resume, so it sticks: one resume == exactly one run.
    entries = [
        _e("terminal", "CAP_REACHED"),
        _resume(),
        _e("developer", "ok"),
        _e("terminal", "CAP_REACHED"),
    ]
    assert _recorded_terminal(entries) == "CAP_REACHED"
    # a SECOND resume after that second terminal un-sticks again (unbounded)
    assert _recorded_terminal([*entries, _resume()]) is None


# --- AC3: PATH_GUARD_VIOLATION is never resumable -----------------------------


def test_path_guard_violation_stays_sticky_even_with_newer_director_resume() -> None:
    entries = [_e("terminal", "PATH_GUARD_VIOLATION"), _resume()]
    assert _recorded_terminal(entries) == "PATH_GUARD_VIOLATION"


def test_unknown_future_terminal_stays_sticky_with_newer_director_resume() -> None:
    # a state absent from the resume table fails safe: still sticky
    entries = [_e("terminal", "SOME_FUTURE_STATE"), _resume()]
    assert _recorded_terminal(entries) == "SOME_FUTURE_STATE"


# --- AC4: the un-stick rule hardcodes no event name (it reads the table) ------


def test_recorded_terminal_hardcodes_no_resume_event_name() -> None:
    # AC4 targets the UN-STICK RULE specifically: _recorded_terminal must consult
    # terminals.clears_terminal, never a per-event `if action == "director_resume"`
    # (else the next consumers add more bespoke ifs). Note-rendering (_director_note)
    # legitimately names the event - that is not the un-stick rule and is out of
    # this grep's scope; the un-stick's freedom from event names is what AC4 locks.
    src = inspect.getsource(_recorded_terminal)
    assert "director_resume" not in src, "un-stick rule must be table-driven"
    assert "cap_override" not in src
    assert "clears_terminal" in src


# --- last_terminal_state: raw last-terminal for the CLI validation gate -------


def test_last_terminal_state_ignores_resume_and_stickiness() -> None:
    assert last_terminal_state([_e("terminal", "CAP_REACHED"), _resume()]) == "CAP_REACHED"
    assert last_terminal_state([_e("push", "ok")]) == "PUSHED"
    assert last_terminal_state([_e("terminal", "QUOTA_TIMEOUT")]) == "QUOTA_TIMEOUT"
    assert last_terminal_state([_e("developer", "ok")]) is None
    assert last_terminal_state([]) is None


# --- AC6: transition derivation is untouched by director_resume ---------------


def test_director_resume_does_not_change_next_phase_or_round() -> None:
    base = [
        _e("developer", "ok", session_id="d1"),
        _e("fast_tests", "pass"),
        _e("rw1", "changes_requested", session_id="r1"),
    ]
    without = derive_resume_point(base, max_loops=4)
    with_resume = derive_resume_point([*base, _resume()], max_loops=4)
    assert without.phase == with_resume.phase == "developer"
    assert without.round == with_resume.round == 2
    # director_resume is not a phase action: it never becomes `last`
    assert with_resume.dev_session == "d1" and with_resume.rw1_session == "r1"


# --- AC12: crash-safe resume - no second event needed to keep going -----------


def test_crash_safe_resume_continues_without_a_second_event() -> None:
    # a director_resume with NO subsequent terminal: _recorded_terminal returns
    # None (un-stuck) and derive_resume_point plainly continues the loop.
    entries = [
        _e("developer", "ok"),
        _e("terminal", "CAP_REACHED"),
        _resume(),
        _e("developer", "ok"),  # a resumed round ran, then the process crashed
    ]
    assert _recorded_terminal(entries) is None
    assert derive_resume_point(entries, max_loops=4).phase == "fast_tests"


# --- resume override: a fresh resume forces a productive developer round ------
# Regression guard for the two rw1 blockers: the un-stick primitive alone lets
# the pure derivation route the resumed run straight back to a terminal (done /
# cap_reached / deadlock) with NO developer round. _resume_developer_point
# overrides that, on top of the untouched derivation.


def _rp(phase: str, round_: int = 1) -> Any:
    from orchestrator.loop import ResumePoint

    return ResumePoint(round=round_, phase=phase, dev_session="d1",
                       rw1_session="r1", rw2_session=None)


def test_fresh_resume_forces_developer_over_done_phase() -> None:
    # PUSHED / MERGE_DECIDED tail derives 'done' - the resume must still develop
    entries = [
        _e("developer", "ok"), _e("push", "ok"),
        _e("terminal", "MERGE_DECIDED:stop_before_merge"), _resume(),
    ]
    forced = _resume_developer_point(entries, _rp("done", round_=1))
    assert forced is not None
    assert forced.phase == "developer"
    assert forced.round == 2  # rounds_used(1) + 1
    assert forced.dev_session == "d1"  # session continuity preserved


def test_fresh_resume_forces_developer_over_cap_reached_phase() -> None:
    entries = [
        _e("developer", "ok"), _e("developer", "ok"),
        _e("terminal", "CAP_REACHED"), _resume(),
    ]
    forced = _resume_developer_point(entries, _rp("cap_reached", round_=3))
    assert forced is not None and forced.phase == "developer"
    assert forced.round == 3  # rounds_used(2) + 1


def test_fresh_resume_forces_developer_over_deadlock_phase() -> None:
    entries = [_e("developer", "ok"), _e("senior", "deadlock"),
               _e("terminal", "ESCALATED_DEADLOCK"), _resume()]
    forced = _resume_developer_point(entries, _rp("deadlock"))
    assert forced is not None and forced.phase == "developer"


def test_no_resume_override_without_a_resume_event() -> None:
    entries = [_e("developer", "ok"), _e("terminal", "CAP_REACHED")]
    assert _resume_developer_point(entries, _rp("cap_reached")) is None


def test_resume_does_not_clobber_a_midflight_phase() -> None:
    # a developer round ran after the resume and the loop is mid-flight
    # (fast_tests): the grant only spends on a TERMINAL phase, so a mid-flight
    # phase flows through the pure derivation untouched - forcing never skips a
    # gate.
    entries = [_e("terminal", "CAP_REACHED"), _resume(), _e("developer", "ok")]
    assert _resume_developer_point(entries, _rp("fast_tests")) is None


def test_resume_grants_three_developer_rounds_past_the_cap() -> None:
    # One director_resume iterates up to _RESUME_ROUND_GRANT (3) developer rounds
    # before the loop is allowed to settle back onto a terminal: each time the
    # run re-caps within the grant, another developer round is forced.
    base = [_e("terminal", "CAP_REACHED"), _resume()]
    # round 1 (0 dev rounds since resume): forced over cap_reached
    forced = _resume_developer_point(base, _rp("cap_reached"))
    assert forced is not None and forced.phase == "developer"
    # round 2 (1 dev round since resume, re-capped): still forced
    base2 = [*base, _e("developer", "ok"), _e("terminal", "CAP_REACHED")]
    assert _resume_developer_point(base2, _rp("cap_reached")) is not None
    # round 3 (2 dev rounds since resume, re-capped): still forced
    base3 = [*base2, _e("developer", "ok"), _e("terminal", "CAP_REACHED")]
    assert _resume_developer_point(base3, _rp("cap_reached")) is not None
    # after 3 dev rounds since the resume the grant is spent: the cap stands
    base4 = [*base3, _e("developer", "ok"), _e("terminal", "CAP_REACHED")]
    assert _resume_developer_point(base4, _rp("cap_reached")) is None


# --- AC7 (pure): the note self-clears once a phase action runs after it -------


def test_director_note_returns_latest_reason_when_newest() -> None:
    entries = [_e("rw1", "changes_requested"), _resume("added throttling")]
    assert _director_note(entries) == "added throttling"


def test_director_note_self_clears_after_a_developer_round() -> None:
    entries = [_e("rw1", "changes_requested"), _resume("x"), _e("developer", "ok")]
    assert _director_note(entries) is None


def test_director_note_absent_without_any_resume() -> None:
    assert _director_note([_e("rw1", "changes_requested")]) is None


# --- AC7 (integration): the note reaches the developer prompt, additively ------


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


_IDENTITY = ("-c", "user.name=test", "-c", "user.email=test@example.com")


@pytest.fixture()
def remote(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    _git("init", "--bare", "--initial-branch=main", str(bare))
    seed = tmp_path / "seed"
    _git("clone", str(bare), str(seed))
    specs = seed / TARGET_DIR_NAME / "specs"
    specs.mkdir(parents=True)
    (specs / "t1.md").write_text("# Task t1\n\nDo X.\n", encoding="utf-8")
    write_policy_toml(seed)
    _git("-C", str(seed), "add", "-A")
    _git("-C", str(seed), *_IDENTITY, "commit", "-m", "init")
    _git("-C", str(seed), "push", "origin", "HEAD:main")
    return bare


@pytest.fixture()
def roles_dir(tmp_path: Path) -> Path:
    roles = tmp_path / "roles"
    roles.mkdir()
    (roles / "developer.md").write_text("DEVELOPER ROLE RULES\n", encoding="utf-8")
    (roles / "rw1.md").write_text("RW1 ROLE RULES\n", encoding="utf-8")
    return roles


def _orch(remote: Path, tmp_path: Path, roles_dir: Path, dev: FakeRunner) -> Orchestrator:
    dev.name = "dev"
    return Orchestrator(
        gitops=GitOps(repo_url=remote.as_uri(), work_root=tmp_path / "work"),
        dev_runner=dev,
        rw1_runner=FakeRunner([]),
        fast_commands="fake-tests",
        shell=FakeShell(),
        roles_dir=roles_dir,
        max_loops=4,
        now=lambda: "2026-07-17T00:00:00Z",
    )


def test_note_and_verdict_both_reach_developer_then_self_clear(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    dev = FakeRunner(["fix one", "fix two"])
    orch = _orch(remote, tmp_path, roles_dir, dev)
    wt = orch.gitops.task_worktree("t1")
    artifacts = TaskArtifacts(wt, "t1")
    # a run that stopped at CAP_REACHED after an rw1 change request, then the
    # Director resumed it with a corrected ask.
    artifacts.append_log(action="developer", outcome="ok", round=1, session_id="d1")
    artifacts.append_log(action="fast_tests", outcome="pass", round=1)
    artifacts.append_log(action="rw1", outcome="changes_requested", round=1, session_id="r1")
    artifacts.write_json(RW1_VERDICT, {"verdict": "CHANGES_REQUESTED",
                                       "findings": [blocker(summary="races on save")]})
    artifacts.append_log(action="director_resume", outcome="ok",
                         reason="spec omitted throttling; added it")

    rp = derive_resume_point(artifacts.read_log(), max_loops=4)
    assert rp.phase == "developer"
    orch._run_developer("t1", wt, artifacts, "spec.md", rp)

    prompt = dev.calls[-1].prompt
    # additive (AC7): the note AND the rw1 verdict section both present
    assert "spec omitted throttling; added it" in prompt
    assert "Director note" in prompt
    assert "races on save" in prompt  # the rw1 verdict JSON survives
    assert "Reviewer verdict to address (rw1" in prompt

    # a subsequent developer round no longer carries the note (self-cleared: a
    # developer entry now sits after the director_resume)
    assert _director_note(artifacts.read_log()) is None
    rp2 = derive_resume_point(
        [*artifacts.read_log(), _e("rw1", "changes_requested")], max_loops=4
    )
    orch._run_developer("t1", wt, artifacts, "spec.md", rp2)
    assert "spec omitted throttling" not in dev.calls[-1].prompt


# --- end-to-end: a resumed run drives a real developer round through _run_phases
# (the rw1-blocker regression: AC1 only proved _recorded_terminal returns None;
# these prove the loop then does PRODUCTIVE work for each terminal class) -------


def _full_orch(
    remote: Path,
    tmp_path: Path,
    roles_dir: Path,
    *,
    dev: FakeRunner,
    rw1: FakeRunner,
    shell: FakeShell,
    max_loops: int = 4,
    senior: FakeRunner | None = None,
) -> Orchestrator:
    dev.name, rw1.name = "dev", "rw1"
    return Orchestrator(
        gitops=GitOps(repo_url=remote.as_uri(), work_root=tmp_path / "work"),
        dev_runner=dev,
        rw1_runner=rw1,
        senior_runner=senior,
        fast_commands="fake-tests",
        shell=shell,
        roles_dir=roles_dir,
        max_loops=max_loops,
        now=lambda: "2026-07-17T00:00:00Z",
    )


def _dev_entries_after_resume(artifacts: TaskArtifacts) -> list[dict[str, Any]]:
    log = artifacts.read_log()
    idx = max(i for i, e in enumerate(log) if e.get("action") == "director_resume")
    return [e for e in log[idx + 1 :] if e.get("action") == "developer"]


def test_resumed_cap_reached_runs_developer_round_and_converges(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    dev = FakeRunner(["applied the corrected spec"])
    rw1 = FakeRunner([verdict_json("APPROVED")])
    orch = _full_orch(remote, tmp_path, roles_dir, dev=dev, rw1=rw1,
                      shell=FakeShell(results=[(0, "green")]), max_loops=2)
    wt = orch.gitops.task_worktree("t1")
    art = TaskArtifacts(wt, "t1")
    for r in (1, 2):  # a run that hit the round cap
        art.append_log(action="developer", outcome="ok", round=r, session_id="d1")
        art.append_log(action="fast_tests", outcome="pass", round=r)
        art.append_log(action="rw1", outcome="changes_requested", round=r, session_id="r1")
    art.write_json(RW1_VERDICT, {"verdict": "CHANGES_REQUESTED",
                                 "findings": [blocker(summary="missing throttling")]})
    art.append_log(action="terminal", outcome="CAP_REACHED")
    art.append_log(action="director_resume", outcome="ok",
                   reason="added the throttling the spec omitted")

    terminal = orch.run("t1")

    assert terminal == "PUSHED"  # the corrected round converged
    dev_rounds = _dev_entries_after_resume(art)
    assert len(dev_rounds) == 1  # exactly one developer round ran (one event, one run)
    assert dev_rounds[0]["round"] == 3  # rounds_used(2) + 1
    assert "added the throttling the spec omitted" in dev.calls[-1].prompt


def test_resumed_pushed_runs_developer_round_not_redone(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # trailing push:ok derives 'done'; the resume must still develop, not re-push
    dev = FakeRunner(["reworked per the corrected spec"])
    rw1 = FakeRunner([verdict_json("APPROVED")])
    orch = _full_orch(remote, tmp_path, roles_dir, dev=dev, rw1=rw1,
                      shell=FakeShell(results=[(0, "green")]))
    wt = orch.gitops.task_worktree("t1")
    art = TaskArtifacts(wt, "t1")
    art.append_log(action="developer", outcome="ok", round=1, session_id="d1")
    art.append_log(action="fast_tests", outcome="pass", round=1)
    art.append_log(action="rw1", outcome="approved", round=1, session_id="r1")
    art.append_log(action="push", outcome="ok", round=1)
    art.append_log(action="terminal", outcome="PUSHED")
    art.append_log(action="director_resume", outcome="ok", reason="ask was incomplete")

    terminal = orch.run("t1")

    assert terminal == "PUSHED"
    dev_rounds = _dev_entries_after_resume(art)
    assert len(dev_rounds) == 1 and dev_rounds[0]["round"] == 2
    assert "ask was incomplete" in dev.calls[-1].prompt


def test_resumed_deadlock_develops_instead_of_re_deadlocking(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # A log that WOULD make _override_phase route developer -> deadlock (2 rw2
    # nogos since the last senior, senior_ran true). A fresh resume must bypass
    # that backstop for one developer round, not immediately re-terminate.
    dev = FakeRunner(["reworked"])
    rw1 = FakeRunner([verdict_json("APPROVED")])
    orch = _full_orch(remote, tmp_path, roles_dir, dev=dev, rw1=rw1,
                      shell=FakeShell(results=[(0, "green")]),
                      senior=FakeRunner([]))  # senior present -> _override_phase active
    wt = orch.gitops.task_worktree("t1")
    art = TaskArtifacts(wt, "t1")
    art.append_log(action="senior", outcome="approved", round=1, sha="s1")
    art.append_log(action="rw2", outcome="nogo", round=1, sha="s1")
    art.append_log(action="rw2", outcome="nogo", round=1, sha="s1")
    art.append_log(action="terminal", outcome="ESCALATED_DEADLOCK")
    # sanity: without the resume this log re-terminates as a deadlock
    assert orch._override_phase("developer", art, wt) == "deadlock"
    art.append_log(action="director_resume", outcome="ok", reason="broke the tie in the spec")

    terminal = orch.run("t1")

    assert terminal == "PUSHED"  # developed + converged, did NOT re-deadlock
    assert len(_dev_entries_after_resume(art)) == 1
