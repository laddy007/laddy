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


def _run_policy_loop(
    remote: Path,
    tmp_path: Path,
    roles_dir: Path,
    dev: FakeRunner,
    rw1: FakeRunner,
    rw2: FakeRunner,
    work: str = "work",
) -> str:
    """Run a full policy-enabled loop against the bare remote; returns the terminal."""
    from orchestrator.testgate import DockerGate

    orch = Orchestrator(
        gitops=GitOps(repo_url=remote.as_uri(), work_root=tmp_path / work),
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
    return orch.run("t1")


def _ci_clone(remote: Path, tmp_path: Path, name: str = "ci-clone") -> Path:
    ci = tmp_path / name
    _git("clone", str(remote), str(ci))
    _git("-C", str(ci), "checkout", "t1")
    return ci


@pytest.fixture()
def pushed_task(remote: Path, tmp_path: Path, roles_dir: Path) -> Path:
    """Run a full policy-enabled loop, then clone the pushed branch like CI would."""
    dev = TouchingRunner(["done"], "myapp/api_helper.py", "x = 1\n")
    rw1 = FakeRunner([verdict_json("APPROVED")])
    rw2 = FakeRunner([verdict_json("APPROVED")])
    assert (
        _run_policy_loop(remote, tmp_path, roles_dir, dev, rw1, rw2)
        == "MERGE_DECIDED:auto_merge"
    )
    return _ci_clone(remote, tmp_path)


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
    state["head_sha"] = gitops.code_sha(pushed_task, "t1")
    art.write_json(STATE, state)

    code, message = check(pushed_task, base="origin/main", task_id="t1")
    assert code == 1
    assert "policy_mismatch" in message


# --- H1: an honest stop_before_merge must never read as a green check --------


class DeletingRunner(FakeRunner):
    """Developer fake that deletes a file and adds a benign source file."""

    def __init__(self, outputs: list[str], delete: str, touch: str, content: str) -> None:
        super().__init__(outputs)
        self._delete = delete
        self._touch = touch
        self._content = content

    def run(self, prompt: str, cwd: Path, resume: str | None = None):
        (cwd / self._delete).unlink()
        target = cwd / self._touch
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self._content, encoding="utf-8")
        return super().run(prompt, cwd, resume)


@pytest.fixture()
def stopped_task(remote: Path, tmp_path: Path, roles_dir: Path) -> Path:
    """An HONEST stop: reviewers declare risk high on a benign, non-sensitive
    path, so the loop commits stop_before_merge truthfully and pushes."""
    dev = TouchingRunner(["done"], "myapp/api_helper.py", "x = 1\n")
    rw1 = FakeRunner([verdict_json("APPROVED", risk="high")])
    rw2 = FakeRunner([verdict_json("APPROVED", risk="high")])
    assert (
        _run_policy_loop(remote, tmp_path, roles_dir, dev, rw1, rw2)
        == "MERGE_DECIDED:stop_before_merge"
    )
    return _ci_clone(remote, tmp_path, "ci-stop")


def test_check_fails_on_honest_high_risk_stop(stopped_task: Path) -> None:
    # H1: committed stop == recomputed stop is CONSISTENT, but the decision
    # value is stop - exit 0 here would launder the stop into a green policy
    # gate (the caller reads only the exit code).
    code, message = check(stopped_task, base="origin/main", task_id="t1")
    assert code == 1
    assert "recomputed_stop_before_merge" in message
    assert "high_risk" in message


def test_fabricated_auto_merge_over_real_stop_still_mismatches(
    stopped_task: Path,
) -> None:
    # H1 guard: rewriting merge-decision.json to auto_merge over a real stop
    # must trip the consistency mismatch, not fall through to the stop branch.
    art = TaskArtifacts(stopped_task, "t1")
    art.write_json(
        MERGE_DECISION, {"decision": "auto_merge", "risk_level": "low", "reasons": []}
    )
    _git("-C", str(stopped_task), "add", "-A")
    _git("-C", str(stopped_task), *IDENTITY, "commit", "-m", "forge decision")

    code, message = check(stopped_task, base="origin/main", task_id="t1")
    assert code == 1
    assert "policy_mismatch" in message


def test_check_fails_on_honest_deleted_test_stop(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # H1 (the leaking shape): a deleted non-invariant test + a benign change is
    # an L2 diff with green gates whose only stop reason is test_files_deleted -
    # a reason with no independent local manifestation. check() must exit
    # non-zero, or the local authority would merge it.
    seed = tmp_path / "seed-tests"
    _git("clone", str(remote), str(seed))
    (seed / "tests").mkdir()
    (seed / "tests" / "test_sample.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    _git("-C", str(seed), "add", "-A")
    _git("-C", str(seed), *IDENTITY, "commit", "-m", "add test")
    _git("-C", str(seed), "push", "origin", "HEAD:main")

    dev = DeletingRunner(["done"], "tests/test_sample.py", "myapp/api_helper.py", "x = 1\n")
    rw1 = FakeRunner([verdict_json("APPROVED")])
    rw2 = FakeRunner([verdict_json("APPROVED")])
    assert (
        _run_policy_loop(remote, tmp_path, roles_dir, dev, rw1, rw2, work="work-del")
        == "MERGE_DECIDED:stop_before_merge"
    )
    ci = _ci_clone(remote, tmp_path, "ci-del")

    # the diff is ordinary logic (L2), not sensitive - nothing else stops it
    from orchestrator.policy import L2, classify_blast_radius
    from orchestrator.target_policy import TargetPolicy

    gitops = GitOps(repo_url="unused", work_root=ci.parent, default_branch="main")
    changed = gitops.changed_files(ci, "t1")
    assert classify_blast_radius(TargetPolicy.myapp(), changed) == L2

    code, message = check(ci, base="origin/main", task_id="t1")
    assert code == 1
    assert "recomputed_stop_before_merge" in message
    assert "test_files_deleted" in message


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


def test_check_report_only_honest_stop_exits_nonzero(
    remote: Path, tmp_path: Path
) -> None:
    # H1 applies to the report-only branch of check() too: an honestly-committed
    # stop (here: no verify round) is consistent but must not exit 0.
    seed = tmp_path / "report-stop"
    _git("clone", str(remote), str(seed))
    _git("-C", str(seed), "checkout", "-b", "audit2")
    (seed / TARGET_DIR_NAME / "specs" / "audit2.md").write_text(
        "---\ntype: audit\n---\n# audit\n", encoding="utf-8"
    )
    art = TaskArtifacts(seed, "audit2")
    art.write_text("report.md", "# findings\n")
    art.write_json("findings.json", [])
    art.append_log(action="investigator", outcome="ok")  # no verify entry
    art.write_json(
        MERGE_DECISION,
        {
            "decision": "stop_before_merge",
            "risk_level": "low",
            "reasons": ["verify_round_missing_or_failed"],
        },
    )
    write_policy_toml(seed)
    _git("-C", str(seed), "add", "-A")
    _git("-C", str(seed), *IDENTITY, "commit", "-m", "report without verify")

    code, message = check(seed, base="origin/main", task_id="audit2")
    assert code == 1
    assert "recomputed_stop_before_merge" in message
    assert "verify_round_missing_or_failed" in message
