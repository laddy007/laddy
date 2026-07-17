"""Tests for the merge-decision terminal + notification wiring in the loop."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator import TARGET_DIR_NAME
from orchestrator.artifacts import MERGE_DECISION, STATE, TaskArtifacts
from orchestrator.gitops import GitOps
from orchestrator.handoff import NtfyNotifier
from orchestrator.loop import Orchestrator
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
    (seed / "myapp").mkdir()
    (seed / "myapp" / "models.py").write_text("STATE = 1\n", encoding="utf-8")
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


class TouchingRunner(FakeRunner):
    """Developer fake that actually edits a file so the diff is non-empty."""

    def __init__(self, outputs, path: str, content: str) -> None:
        super().__init__(outputs)
        self._path = path
        self._content = content

    def run(self, prompt: str, cwd: Path, resume: str | None = None):
        target = cwd / self._path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self._content, encoding="utf-8")
        return super().run(prompt, cwd, resume)


def _orch(
    remote: Path,
    tmp_path: Path,
    roles_dir: Path,
    dev: FakeRunner,
    rw1: FakeRunner,
    notifier: NtfyNotifier,
) -> Orchestrator:
    from orchestrator.testgate import DockerGate

    dev.name, rw1.name = "dev", "rw1"
    rw2 = FakeRunner([verdict_json("APPROVED")])
    rw2.name = "rw2"
    return Orchestrator(
        gitops=GitOps(repo_url=remote.as_uri(), work_root=tmp_path / "work"),
        dev_runner=dev,
        rw1_runner=rw1,
        rw2_runner=rw2,
        docker_gate=DockerGate(
            frontend_gate="FE", compose_rel="c.yml", shell=FakeShell(results=[(0, "gate green")])
        ),
        policy_enabled=True,
        notifier=notifier,
        fast_commands="t",
        shell=FakeShell(results=[(0, "g")]),
        roles_dir=roles_dir,
        max_loops=4,
    )


def test_auto_merge_decision_written_and_notified(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    posts: list[str] = []
    dev = TouchingRunner(["done"], "myapp/api_helper.py", "x = 1\n")
    rw1 = FakeRunner([verdict_json("APPROVED")])
    notifier = NtfyNotifier("topic", post_fn=lambda url, msg: posts.append(msg))
    orch = _orch(remote, tmp_path, roles_dir, dev, rw1, notifier)

    terminal = orch.run("t1")

    assert terminal == "MERGE_DECIDED:auto_merge"
    wt = orch.gitops.task_worktree("t1")
    art = TaskArtifacts(wt, "t1")
    decision = art.read_json(MERGE_DECISION)
    assert decision is not None
    assert decision["decision"] == "auto_merge"
    state = art.read_json(STATE)
    assert state is not None
    assert state["rw1"]["approved"] is True
    assert state["rw1"]["sha"] == state["head_sha"]
    assert posts == ["t1: merged automatically"]


def test_sensitive_path_stops_before_merge(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    posts: list[str] = []
    dev = TouchingRunner(["done"], "myapp/models.py", "STATE = 2\n")
    rw1 = FakeRunner([verdict_json("APPROVED")])
    notifier = NtfyNotifier("topic", post_fn=lambda url, msg: posts.append(msg))
    orch = _orch(remote, tmp_path, roles_dir, dev, rw1, notifier)

    terminal = orch.run("t1")

    assert terminal == "MERGE_DECIDED:stop_before_merge"
    wt = orch.gitops.task_worktree("t1")
    decision = TaskArtifacts(wt, "t1").read_json(MERGE_DECISION)
    assert decision is not None
    assert any("policy_sensitive_paths" in r for r in decision["reasons"])
    assert posts == ["t1: stopped before merge, your decision needed"]
    # branch is still pushed - the decision is executed off-VPS
    assert _git("-C", str(remote), "rev-parse", "refs/heads/t1")


def test_resumed_merge_decided_runs_a_developer_round(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    """director-resume flagship (the spec's cited mcp example): a task that
    ended MERGE_DECIDED:stop_before_merge, whose tail is push:ok (so the pure
    derivation yields 'done'), must - once resumed - run a REAL developer round
    that receives the Director note, NOT silently re-record the same terminal via
    the 'done' branch. Drives the resumed task through the full _run_phases with
    policy enabled (the coverage the un-stick-only tests could not give)."""
    # 1. first run stops before merge (sensitive path touched).
    dev1 = TouchingRunner(["done"], "myapp/models.py", "STATE = 2\n")
    orch1 = _orch(remote, tmp_path, roles_dir, dev1,
                  FakeRunner([verdict_json("APPROVED")]), NtfyNotifier(None))
    assert orch1.run("t1") == "MERGE_DECIDED:stop_before_merge"

    wt = orch1.gitops.task_worktree("t1")
    art = TaskArtifacts(wt, "t1")
    # 2. Director resumes with a corrected ask.
    art.append_log(action="director_resume", outcome="ok",
                   reason="models change needs a migration guard")

    # 3. second run (fresh fakes): the resume must DEVELOP, not re-record.
    dev2 = TouchingRunner(["reworked per the note"], "myapp/api_helper.py", "y = 2\n")
    orch2 = _orch(remote, tmp_path, roles_dir, dev2,
                  FakeRunner([verdict_json("APPROVED")]), NtfyNotifier(None))
    terminal = orch2.run("t1")

    assert terminal.startswith("MERGE_DECIDED:")  # productive re-run reached a decision
    log = art.read_log()
    idx = max(i for i, e in enumerate(log) if e.get("action") == "director_resume")
    dev_after = [e for e in log[idx + 1:] if e.get("action") == "developer"]
    assert len(dev_after) == 1  # exactly one developer round ran after the resume
    assert "models change needs a migration guard" in dev2.calls[0].prompt  # note delivered


def test_artifact_commits_do_not_invalidate_approvals(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    """Verdict/log commits move HEAD but not code_sha - approvals stay keyed."""
    dev = TouchingRunner(["done"], "myapp/api_helper.py", "x = 1\n")
    rw1 = FakeRunner([verdict_json("APPROVED")])
    orch = _orch(remote, tmp_path, roles_dir, dev, rw1, NtfyNotifier(None))

    terminal = orch.run("t1")
    assert terminal == "MERGE_DECIDED:auto_merge"

    wt = orch.gitops.task_worktree("t1")
    # HEAD (with artifact commits) differs from the code SHA the gates keyed on
    assert orch.gitops.head_sha(wt) != orch.gitops.code_sha(wt)


def test_cap_reached_writes_handback_and_notifies(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    posts: list[str] = []
    dev = FakeRunner(["a", "b", "c", "d"])
    rw1 = FakeRunner(
        [verdict_json("CHANGES_REQUESTED", [blocker(summary=f"i{i}")]) for i in range(4)]
    )
    notifier = NtfyNotifier("topic", post_fn=lambda url, msg: posts.append(msg))
    orch = Orchestrator(
        gitops=GitOps(repo_url=remote.as_uri(), work_root=tmp_path / "work"),
        dev_runner=dev,
        rw1_runner=rw1,
        policy_enabled=True,
        notifier=notifier,
        fast_commands="t",
        shell=FakeShell(results=[(0, "g")] * 4),
        roles_dir=roles_dir,
        max_loops=4,
    )
    dev.name, rw1.name = "dev", "rw1"

    assert orch.run("t1") == "CAP_REACHED"
    wt = orch.gitops.task_worktree("t1")
    handback = TaskArtifacts(wt, "t1").read_text("handback.md")
    assert handback is not None
    assert "CAP_REACHED" in handback
    assert posts == ["t1: iteration cap reached, see handback"]
