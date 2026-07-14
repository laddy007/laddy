"""Tests for the rw2 cross-vendor guard in the loop (design S5 step 8)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator import TARGET_DIR_NAME
from orchestrator.artifacts import RW2_VERDICT, TaskArtifacts
from orchestrator.gitops import GitOps
from orchestrator.loop import Orchestrator
from tests.fakes import (
    FakeRunner,
    FakeShell,
    advisory,
    blocker,
    verdict_json,
    write_policy_toml,
)


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


def _orch(
    remote: Path,
    tmp_path: Path,
    roles_dir: Path,
    dev: FakeRunner,
    rw1: FakeRunner,
    rw2: FakeRunner,
    shell: FakeShell,
    senior: FakeRunner | None = None,
) -> Orchestrator:
    dev.name, rw1.name, rw2.name = "dev", "rw1", "rw2"
    if senior is not None:
        senior.name = "senior"
    return Orchestrator(
        gitops=GitOps(repo_url=remote.as_uri(), work_root=tmp_path / "work"),
        dev_runner=dev,
        rw1_runner=rw1,
        rw2_runner=rw2,
        senior_runner=senior,
        fast_commands="fake-tests",
        shell=shell,
        roles_dir=roles_dir,
        max_loops=4,
    )


def test_rw2_go_proceeds_to_push(remote: Path, tmp_path: Path, roles_dir: Path) -> None:
    dev = FakeRunner(["done"])
    rw1 = FakeRunner([verdict_json("APPROVED")])
    rw2 = FakeRunner([verdict_json("APPROVED")])
    orch = _orch(remote, tmp_path, roles_dir, dev, rw1, rw2, FakeShell(results=[(0, "g")]))

    assert orch.run("t1") == "PUSHED"
    wt = orch.gitops.task_worktree("t1")
    log = TaskArtifacts(wt, "t1").read_log()
    assert [e["action"] for e in log] == [
        "developer", "fast_tests", "rw1", "rw2", "push", "terminal",
    ]
    assert log[3]["outcome"] == "go"


def test_rw2_advisory_only_never_blocks(remote: Path, tmp_path: Path, roles_dir: Path) -> None:
    dev = FakeRunner(["done"])
    rw1 = FakeRunner([verdict_json("APPROVED")])
    rw2 = FakeRunner([verdict_json("APPROVED", [advisory(summary="could be simpler")])])
    orch = _orch(remote, tmp_path, roles_dir, dev, rw1, rw2, FakeShell(results=[(0, "g")]))

    assert orch.run("t1") == "PUSHED"
    assert len(dev.calls) == 1  # advisory forced no developer round
    wt = orch.gitops.task_worktree("t1")
    stored = TaskArtifacts(wt, "t1").read_json(RW2_VERDICT)
    assert stored is not None
    assert stored["findings"][0]["severity"] == "advisory"  # recorded either way


def test_rw2_blocker_forces_rework_then_gonogo(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    dev = FakeRunner(["done", "fixed"])
    rw1 = FakeRunner([verdict_json("APPROVED"), verdict_json("APPROVED")])
    rw2 = FakeRunner(
        [
            verdict_json("CHANGES_REQUESTED", [blocker(summary="drops rows on conflict")]),
            verdict_json("APPROVED"),
        ]
    )
    shell = FakeShell(results=[(0, "g"), (0, "g")])
    orch = _orch(remote, tmp_path, roles_dir, dev, rw1, rw2, shell)

    assert orch.run("t1") == "PUSHED"
    # rw2 finding travelled to the developer as data
    assert "drops rows on conflict" in dev.calls[1].prompt
    # rw1 re-approved before rw2 got the go/nogo pass
    assert len(rw1.calls) == 2
    # second rw2 pass used the go/nogo prompt, not a fresh full review
    assert "go/nogo ONLY" in rw2.calls[1].prompt
    assert "Adversarially review" not in rw2.calls[1].prompt
    wt = orch.gitops.task_worktree("t1")
    log = TaskArtifacts(wt, "t1").read_log()
    assert [e["action"] for e in log] == [
        "developer", "fast_tests", "rw1", "rw2",
        "developer", "fast_tests", "rw1", "rw2", "push", "terminal",
    ]


def test_rw2_after_go_then_authoritative_rework_gets_fresh_review(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # rw2 says go; authoritative fails; dev reworks; rw1 re-approves. The next
    # rw2 must be a FRESH adversarial review of the rewritten code, NOT a
    # go/nogo rubber-stamp of the stale approving verdict.
    from orchestrator.testgate import DockerGate

    dev = FakeRunner(["done", "reworked"])
    rw1 = FakeRunner([verdict_json("APPROVED"), verdict_json("APPROVED")])
    rw2 = FakeRunner([verdict_json("APPROVED"), verdict_json("APPROVED")])
    dev.name, rw1.name, rw2.name = "dev", "rw1", "rw2"
    orch = Orchestrator(
        gitops=GitOps(repo_url=remote.as_uri(), work_root=tmp_path / "work"),
        dev_runner=dev,
        rw1_runner=rw1,
        rw2_runner=rw2,
        docker_gate=DockerGate(
            frontend_gate="FE",
            compose_rel="c.yml",
            shell=FakeShell(results=[(1, "FAILED"), (0, "green")]),
        ),
        fast_commands="t",
        shell=FakeShell(results=[(0, "g"), (0, "g")]),
        roles_dir=roles_dir,
        max_loops=4,
    )
    assert orch.run("t1") == "PUSHED"
    assert len(rw2.calls) == 2
    # both rw2 passes were fresh adversarial reviews (prior was a 'go', not nogo)
    assert "Adversarially review" in rw2.calls[1].prompt
    assert "go/nogo ONLY" not in rw2.calls[1].prompt


def test_rw2_after_malformed_gets_fresh_review_not_null_gonogo(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # first rw2 malformed -> no verdict stored; the retry within the SAME rw2
    # phase must be a fresh review, and must never embed a null previous verdict
    dev = FakeRunner(["done"])
    rw1 = FakeRunner([verdict_json("APPROVED")])
    rw2 = FakeRunner(["not json", verdict_json("APPROVED")])
    orch = _orch(remote, tmp_path, roles_dir, dev, rw1, rw2, FakeShell(results=[(0, "g")]))

    assert orch.run("t1") == "PUSHED"
    # neither rw2 prompt is a go/nogo over a null previous finding
    assert all("go/nogo ONLY" not in c.prompt for c in rw2.calls)
    assert all('"previous_verdict": null' not in c.prompt for c in rw2.calls)


def test_rw2_quality_blocker_is_schema_rejected_and_retried(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    dev = FakeRunner(["done"])
    rw1 = FakeRunner([verdict_json("APPROVED")])
    quality_blocker = blocker(category="quality", summary="ugly code")
    rw2 = FakeRunner(
        [verdict_json("CHANGES_REQUESTED", [quality_blocker]), verdict_json("APPROVED")]
    )
    orch = _orch(remote, tmp_path, roles_dir, dev, rw1, rw2, FakeShell(results=[(0, "g")]))

    assert orch.run("t1") == "PUSHED"
    assert len(rw2.calls) == 2
    assert "must not emit blocker findings with category 'quality'" in rw2.calls[1].prompt


def test_rw2_sessions_disjoint_from_dev_and_rw1(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    dev = FakeRunner(["done", "fixed"])
    rw1 = FakeRunner([verdict_json("APPROVED"), verdict_json("APPROVED")])
    rw2 = FakeRunner(
        [verdict_json("CHANGES_REQUESTED", [blocker()]), verdict_json("APPROVED")]
    )
    shell = FakeShell(results=[(0, "g"), (0, "g")])
    orch = _orch(remote, tmp_path, roles_dir, dev, rw1, rw2, shell)
    orch.run("t1")

    assert all((c.resume or "rw2-").startswith("rw2-") for c in rw2.calls)
    assert all((c.resume or "dev-").startswith("dev-") for c in dev.calls)
    assert all((c.resume or "rw1-").startswith("rw1-") for c in rw1.calls)
