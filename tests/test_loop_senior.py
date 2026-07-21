"""Tests for the authoritative gate in the loop + senior escalation (S6, S7)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator import TARGET_DIR_NAME
from orchestrator.artifacts import AUTHORITATIVE, SENIOR_VERDICT, TaskArtifacts
from orchestrator.gitops import GitOps
from orchestrator.loop import Orchestrator, nonconvergence_detected, senior_ran
from orchestrator.testgate import DockerGate
from tests.fakes import FakeRunner, FakeShell, blocker, verdict_json, write_policy_toml


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


IDENTITY = ("-c", "user.name=test", "-c", "user.email=test@example.com")


@pytest.fixture()
def remote(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    _git("init", "--bare", "--initial-branch=main", str(bare))
    seed = tmp_path / "seed"
    _git("clone", str(bare), str(seed))
    (seed / TARGET_DIR_NAME / "specs").mkdir(parents=True)
    (seed / TARGET_DIR_NAME / "specs" / "t1.md").write_text("# t1\n", encoding="utf-8")
    write_policy_toml(seed)
    _git("-C", str(seed), "add", "-A")
    _git("-C", str(seed), *IDENTITY, "commit", "-m", "init")
    _git("-C", str(seed), "push", "origin", "HEAD:main")
    return bare


@pytest.fixture()
def roles_dir(tmp_path: Path) -> Path:
    roles = tmp_path / "roles"
    roles.mkdir()
    for name in ("developer", "rw1", "rw2", "senior-reviewer"):
        (roles / f"{name}.md").write_text(f"{name.upper()} ROLE\n", encoding="utf-8")
    return roles


class GateShell(FakeShell):
    """Separate shell for the docker gate so fast tests and gate don't clash."""


def _orch(
    remote: Path,
    tmp_path: Path,
    roles_dir: Path,
    *,
    dev: FakeRunner,
    rw1: FakeRunner,
    rw2: FakeRunner | None = None,
    senior: FakeRunner | None = None,
    fast_shell: FakeShell,
    gate_shell: FakeShell | None = None,
) -> Orchestrator:
    dev.name, rw1.name = "dev", "rw1"
    if rw2 is not None:
        rw2.name = "rw2"
    if senior is not None:
        senior.name = "senior"
    docker_gate = (
        DockerGate(frontend_gate="FE", compose_rel="c.yml", shell=gate_shell)
        if gate_shell is not None
        else None
    )
    return Orchestrator(
        gitops=GitOps(repo_url=remote.as_uri(), work_root=tmp_path / "work"),
        dev_runner=dev,
        rw1_runner=rw1,
        rw2_runner=rw2,
        senior_runner=senior,
        docker_gate=docker_gate,
        fast_commands="fake-tests",
        shell=fast_shell,
        roles_dir=roles_dir,
        max_loops=4,
    )


def test_authoritative_green_after_rw2_go_then_push(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    orch = _orch(
        remote, tmp_path, roles_dir,
        dev=FakeRunner(["done"]),
        rw1=FakeRunner([verdict_json("APPROVED")]),
        rw2=FakeRunner([verdict_json("APPROVED")]),
        fast_shell=FakeShell(results=[(0, "g")]),
        gate_shell=GateShell(results=[(0, "full suite green")]),
    )
    assert orch.run("t1") == "PUSHED"
    wt = orch.gitops.task_worktree("t1")
    art = TaskArtifacts(wt, "t1")
    log = art.read_log()
    assert [e["action"] for e in log] == [
        "developer", "fast_tests", "rw1", "rw2", "authoritative", "push", "terminal",
    ]
    stored = art.read_json(AUTHORITATIVE)
    assert stored is not None
    assert stored["passed"] is True
    assert stored["sha"] == log[4]["sha"]


def test_authoritative_red_goes_back_to_developer_and_invalidates_approvals(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    orch = _orch(
        remote, tmp_path, roles_dir,
        dev=FakeRunner(["done", "fixed"]),
        rw1=FakeRunner([verdict_json("APPROVED"), verdict_json("APPROVED")]),
        rw2=FakeRunner([verdict_json("APPROVED"), verdict_json("APPROVED")]),
        fast_shell=FakeShell(results=[(0, "g"), (0, "g")]),
        gate_shell=GateShell(results=[(1, "FAILED test_full"), (0, "green")]),
    )
    assert orch.run("t1") == "PUSHED"
    wt = orch.gitops.task_worktree("t1")
    log = TaskArtifacts(wt, "t1").read_log()
    # any new commit invalidates prior approvals: rw1 AND rw2 re-ran
    assert [e["action"] for e in log] == [
        "developer", "fast_tests", "rw1", "rw2", "authoritative",
        "developer", "fast_tests", "rw1", "rw2", "authoritative", "push", "terminal",
    ]


def test_authoritative_failure_tail_feeds_developer_prompt(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    dev = FakeRunner(["done", "fixed"])
    orch = _orch(
        remote, tmp_path, roles_dir,
        dev=dev,
        rw1=FakeRunner([verdict_json("APPROVED"), verdict_json("APPROVED")]),
        rw2=FakeRunner([verdict_json("APPROVED"), verdict_json("APPROVED")]),
        fast_shell=FakeShell(results=[(0, "g"), (0, "g")]),
        gate_shell=GateShell(results=[(1, "FAILED test_full - integrity"), (0, "green")]),
    )
    orch.run("t1")
    assert "FAILED test_full - integrity" in dev.calls[1].prompt
    assert "Authoritative gate failure" in dev.calls[1].prompt


def test_oscillation_same_rw2_finding_escalates_to_senior(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    same = blocker(summary="loses audit rows", file="myapp/x.py")
    senior = FakeRunner([verdict_json("APPROVED")])
    orch = _orch(
        remote, tmp_path, roles_dir,
        dev=FakeRunner(["done", "attempt 2"]),
        rw1=FakeRunner([verdict_json("APPROVED")] * 3),
        rw2=FakeRunner(
            [
                verdict_json("CHANGES_REQUESTED", [same]),
                verdict_json("CHANGES_REQUESTED", [same]),
            ]
        ),
        senior=senior,
        fast_shell=FakeShell(results=[(0, "g")] * 3),
    )
    assert orch.run("t1") == "PUSHED"
    wt = orch.gitops.task_worktree("t1")
    log = TaskArtifacts(wt, "t1").read_log()
    actions = [e["action"] for e in log]
    # after the second identical nogo the loop went senior, not another dev round
    assert actions == [
        "developer", "fast_tests", "rw1", "rw2",
        "developer", "fast_tests", "rw1", "rw2",
        "senior", "push", "terminal",
    ]
    # senior got both verdicts as data
    assert "loses audit rows" in senior.calls[0].prompt
    # no gate failure in this dispute (fast all green, no authoritative run), so
    # the gate-failure section is omitted (Change 3)
    assert "Last gate failure" not in senior.calls[0].prompt
    art = TaskArtifacts(wt, "t1")
    stored = art.read_json(SENIOR_VERDICT)
    assert stored is not None and stored["verdict"] == "APPROVED"


def test_senior_changes_requested_sends_developer_round(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    same = blocker(summary="same issue")
    orch = _orch(
        remote, tmp_path, roles_dir,
        dev=FakeRunner(["a", "b", "c"]),
        rw1=FakeRunner([verdict_json("APPROVED")] * 3),
        rw2=FakeRunner(
            [
                verdict_json("CHANGES_REQUESTED", [same]),
                verdict_json("CHANGES_REQUESTED", [same]),
                verdict_json("APPROVED"),
            ]
        ),
        senior=FakeRunner(
            [verdict_json("CHANGES_REQUESTED", [blocker(summary="do it this way")])]
        ),
        fast_shell=FakeShell(results=[(0, "g")] * 3),
    )
    assert orch.run("t1") == "PUSHED"
    wt = orch.gitops.task_worktree("t1")
    actions = [e["action"] for e in TaskArtifacts(wt, "t1").read_log()]
    assert "senior" in actions
    # senior CHANGES_REQUESTED produced one more developer round, then converged
    assert actions.index("senior") < len(actions) - 2


def test_senior_malformed_is_deadlock_terminal(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    same = blocker(summary="same issue")
    orch = _orch(
        remote, tmp_path, roles_dir,
        dev=FakeRunner(["a", "b"]),
        rw1=FakeRunner([verdict_json("APPROVED")] * 2),
        rw2=FakeRunner(
            [
                verdict_json("CHANGES_REQUESTED", [same]),
                verdict_json("CHANGES_REQUESTED", [same]),
            ]
        ),
        senior=FakeRunner(["junk", "junk", "junk"]),
        fast_shell=FakeShell(results=[(0, "g")] * 2),
    )
    assert orch.run("t1") == "ESCALATED_DEADLOCK"
    wt = orch.gitops.task_worktree("t1")
    art = TaskArtifacts(wt, "t1")
    log = art.read_log()
    assert log[-2]["outcome"] == "deadlock"  # senior malformed -> deadlock
    # terminal marker (Task 7) is now the log's last entry
    assert log[-1]["action"] == "terminal"
    assert log[-1]["outcome"] == "ESCALATED_DEADLOCK"
    summary = (art.dir / "human-summary.md").read_text(encoding="utf-8")
    assert "ESCALATED_DEADLOCK" in summary


def test_high_risk_verdict_triggers_senior_gate(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    senior = FakeRunner([verdict_json("APPROVED")])
    orch = _orch(
        remote, tmp_path, roles_dir,
        dev=FakeRunner(["done"]),
        rw1=FakeRunner([verdict_json("APPROVED", risk="high")]),
        rw2=FakeRunner([verdict_json("APPROVED", risk="high")]),
        senior=senior,
        fast_shell=FakeShell(results=[(0, "g")]),
    )
    assert orch.run("t1") == "PUSHED"
    wt = orch.gitops.task_worktree("t1")
    actions = [e["action"] for e in TaskArtifacts(wt, "t1").read_log()]
    assert actions == [
        "developer", "fast_tests", "rw1", "rw2", "senior", "push", "terminal",
    ]


def test_nonconvergence_helpers() -> None:
    assert nonconvergence_detected(
        [
            {"action": "rw2", "outcome": "nogo", "fingerprint": "f1"},
            {"action": "rw2", "outcome": "nogo", "fingerprint": "f1"},
        ]
    )
    assert not nonconvergence_detected(
        [{"action": "rw2", "outcome": "nogo", "fingerprint": "f1"}]
    )
    assert nonconvergence_detected(
        [
            {"action": "authoritative", "outcome": "fail", "fingerprint": "x"},
            {"action": "authoritative", "outcome": "fail", "fingerprint": "x"},
        ]
    )
    # Change 2: two identical fast-test failures escalate too - a developer fix
    # that keeps failing the same fast test never reaches the reviewers again.
    assert nonconvergence_detected(
        [
            {"action": "fast_tests", "outcome": "fail", "fingerprint": "ft"},
            {"action": "fast_tests", "outcome": "fail", "fingerprint": "ft"},
        ]
    )
    # ...but only SINCE the last senior verdict: a senior intervention re-arms
    # the backstop, so one post-senior fast failure is not yet nonconvergence.
    assert not nonconvergence_detected(
        [
            {"action": "fast_tests", "outcome": "fail", "fingerprint": "ft"},
            {"action": "senior", "outcome": "changes_requested"},
            {"action": "fast_tests", "outcome": "fail", "fingerprint": "ft"},
        ]
    )
    # two DIFFERENT fast failures are progress, not a stall
    assert not nonconvergence_detected(
        [
            {"action": "fast_tests", "outcome": "fail", "fingerprint": "ft1"},
            {"action": "fast_tests", "outcome": "fail", "fingerprint": "ft2"},
        ]
    )
    assert senior_ran([{"action": "senior", "outcome": "approved"}])
    assert not senior_ran([{"action": "rw1", "outcome": "approved"}])


def test_repeated_fast_failure_escalates_to_senior_with_failure_tail(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # Change 2+3: a developer fix that keeps failing the SAME fast test would run
    # to CAP_REACHED (the fast gate is before rw1, so the reviewers never see it
    # again). Two identical fast failures must escalate to the senior instead,
    # and the senior must be handed the failure tail so it knows WHY it is stuck.
    senior = FakeRunner([verdict_json("APPROVED")])
    orch = _orch(
        remote, tmp_path, roles_dir,
        dev=FakeRunner(["done", "still broken"]),
        rw1=FakeRunner([verdict_json("APPROVED")]),
        senior=senior,
        fast_shell=FakeShell(
            results=[(1, "FAILED tests/test_x.py::test_y - boom")] * 2
        ),
    )
    assert orch.run("t1") == "PUSHED"
    wt = orch.gitops.task_worktree("t1")
    actions = [e["action"] for e in TaskArtifacts(wt, "t1").read_log()]
    # escalated after the second identical fast failure, not another dev round
    assert actions == [
        "developer", "fast_tests",
        "developer", "fast_tests",
        "senior", "push", "terminal",
    ]
    # the senior prompt carries the fast-failure tail (Change 3)
    assert "Last gate failure" in senior.calls[0].prompt
    assert "FAILED tests/test_x.py::test_y - boom" in senior.calls[0].prompt
