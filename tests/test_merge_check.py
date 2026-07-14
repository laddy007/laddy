"""Tests for the off-VPS merge check (recompute, don't trust)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator import TARGET_DIR_NAME
from orchestrator.artifacts import MERGE_DECISION, STATE, TaskArtifacts
from orchestrator.gitops import GitOps
from orchestrator.handoff import NtfyNotifier
from orchestrator.loop import Orchestrator
from orchestrator.merge_check import check
from tests.fakes import FakeRunner, FakeShell, verdict_json, write_policy_toml
from tests.test_loop_policy import TouchingRunner


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
    for name in ("developer", "rw1", "rw2"):
        (roles / f"{name}.md").write_text(f"{name.upper()} ROLE\n", encoding="utf-8")
    return roles


@pytest.fixture()
def pushed_task(remote: Path, tmp_path: Path, roles_dir: Path) -> Path:
    """Run a full policy-enabled loop, then clone the pushed branch like CI would."""
    from orchestrator.testgate import DockerGate

    dev = TouchingRunner(["done"], "myapp/api_helper.py", "x = 1\n")
    rw1 = FakeRunner([verdict_json("APPROVED")])
    rw2 = FakeRunner([verdict_json("APPROVED")])
    orch = Orchestrator(
        gitops=GitOps(repo_url=remote.as_uri(), work_root=tmp_path / "work"),
        dev_runner=dev,
        rw1_runner=rw1,
        rw2_runner=rw2,
        docker_gate=DockerGate(
            frontend_gate="FE", compose_rel="c.yml", shell=FakeShell(results=[(0, "gate green")])
        ),
        policy_enabled=True,
        notifier=NtfyNotifier(None),
        fast_commands="t",
        shell=FakeShell(results=[(0, "g")]),
        roles_dir=roles_dir,
        max_loops=4,
    )
    dev.name, rw1.name, rw2.name = "dev", "rw1", "rw2"
    assert orch.run("t1") == "MERGE_DECIDED:auto_merge"

    ci = tmp_path / "ci-clone"
    _git("clone", str(remote), str(ci))
    _git("-C", str(ci), "checkout", "t1")
    return ci


def test_check_passes_on_untampered_branch(pushed_task: Path) -> None:
    code, message = check(pushed_task, base="origin/main", task_id="t1")
    assert (code, message) == (0, "decision=auto_merge")


def test_check_fails_on_tampered_decision(pushed_task: Path) -> None:
    # attacker/agent rewrites the committed decision to auto_merge after
    # touching a sensitive file
    (pushed_task / "myapp" / "models.py").write_text("STATE = 666\n", encoding="utf-8")
    _git("-C", str(pushed_task), "add", "-A")
    _git("-C", str(pushed_task), *IDENTITY, "commit", "-m", "sneaky model change")

    code, message = check(pushed_task, base="origin/main", task_id="t1")
    assert code == 1
    # the new code commit moved code_sha past the recorded state
    assert "state_sha_mismatch" in message


def test_check_fails_on_forged_state_sha(pushed_task: Path) -> None:
    # forge state.json to claim the new sha - gate replay still keys approvals
    # to the OLD sha, so the recomputed decision is stop_before_merge
    (pushed_task / "myapp" / "models.py").write_text("STATE = 666\n", encoding="utf-8")
    _git("-C", str(pushed_task), "add", "-A")
    _git("-C", str(pushed_task), *IDENTITY, "commit", "-m", "sneaky model change")

    art = TaskArtifacts(pushed_task, "t1")
    state = art.read_json(STATE)
    assert state is not None
    gitops = GitOps(repo_url="unused", work_root=pushed_task.parent)
    state["head_sha"] = gitops.code_sha(pushed_task)
    art.write_json(STATE, state)

    code, message = check(pushed_task, base="origin/main", task_id="t1")
    assert code == 1
    assert "policy_mismatch" in message


def test_check_fails_on_missing_artifacts(remote: Path, tmp_path: Path) -> None:
    seed = tmp_path / "bare-branch"
    _git("clone", str(remote), str(seed))
    _git("-C", str(seed), "checkout", "-b", "t9")
    code, message = check(seed, base="origin/main", task_id="t9")
    assert code == 1
    assert "missing_merge_decision" in message


def test_check_report_only_uses_path_guard(remote: Path, tmp_path: Path) -> None:
    seed = tmp_path / "report-branch"
    _git("clone", str(remote), str(seed))
    _git("-C", str(seed), "checkout", "-b", "audit1")
    (seed / TARGET_DIR_NAME / "specs" / "audit1.md").write_text(
        "---\ntype: audit\n---\n# audit\n", encoding="utf-8"
    )
    from orchestrator.loop import report_content_sha

    art = TaskArtifacts(seed, "audit1")
    art.write_text("report.md", "# findings\n")
    art.write_json("findings.json", [])
    art.append_log(action="investigator", outcome="ok")
    # the verify entry is bound to the exact report/findings content it blessed
    art.append_log(
        action="verify", outcome="ok", confirmed=1, content_sha=report_content_sha(art)
    )
    art.write_json(MERGE_DECISION, {"decision": "auto_merge", "risk_level": "low", "reasons": []})
    write_policy_toml(seed)
    _git("-C", str(seed), "add", "-A")
    _git("-C", str(seed), *IDENTITY, "commit", "-m", "report")

    code, message = check(seed, base="origin/main", task_id="audit1")
    assert (code, message) == (0, "decision=auto_merge")

    # tamper: rewrite report.md after verify -> content_sha no longer matches
    art.write_text("report.md", "# TAMPERED\n")
    write_policy_toml(seed)
    _git("-C", str(seed), "add", "-A")
    _git("-C", str(seed), *IDENTITY, "commit", "-m", "tamper report")
    code, message = check(seed, base="origin/main", task_id="audit1")
    assert code == 1
    assert "policy_mismatch" in message

    # and a smuggled source file is caught by the path guard
    (seed / "sneaky.py").write_text("x=1\n", encoding="utf-8")
    write_policy_toml(seed)
    _git("-C", str(seed), "add", "-A")
    _git("-C", str(seed), *IDENTITY, "commit", "-m", "sneak")
    code, message = check(seed, base="origin/main", task_id="audit1")
    assert code == 1
    assert "policy_mismatch" in message
