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
    derive_resume_point,
    last_terminal_state,
)
from tests.fakes import FakeRunner, FakeShell, blocker, write_policy_toml


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
    src = inspect.getsource(_recorded_terminal)
    # the un-stick must consult terminals.clears_terminal, never a per-event if
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
