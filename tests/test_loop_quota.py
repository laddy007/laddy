"""Loop-level quota handling: wait + retry same step, budget timeout."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator import TARGET_DIR_NAME
from orchestrator.agents import AgentResult
from orchestrator.artifacts import TaskArtifacts
from orchestrator.gitops import GitOps
from orchestrator.handoff import NtfyNotifier
from orchestrator.loop import Orchestrator
from orchestrator.quota import QuotaPolicy
from tests.fakes import FakeRunner, FakeShell, verdict_json, write_policy_toml


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


IDENTITY = ("-c", "user.name=test", "-c", "user.email=test@example.com")
_NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


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
    _git("-C", str(seed), *IDENTITY, "commit", "-m", "init")
    _git("-C", str(seed), "push", "origin", "HEAD:main")
    return bare


@pytest.fixture()
def roles_dir(tmp_path: Path) -> Path:
    roles = tmp_path / "roles"
    roles.mkdir()
    (roles / "developer.md").write_text("DEVELOPER ROLE RULES\n", encoding="utf-8")
    (roles / "rw1.md").write_text("RW1 ROLE RULES\n", encoding="utf-8")
    return roles


class RecordingNotifier(NtfyNotifier):
    def __init__(self) -> None:
        super().__init__(topic=None)
        self.sent: list[tuple[str, str]] = []

    def notify(self, task_id: str, terminal_state: str) -> None:
        self.sent.append((task_id, terminal_state))


def _quota_result() -> AgentResult:
    return AgentResult(
        text="usage limit reached",
        session_id=None,
        exit_reason="quota",
        returncode=1,
        quota_reset_at=None,
    )


def _build(
    remote: Path,
    tmp_path: Path,
    roles_dir: Path,
    dev: FakeRunner,
    rw1: FakeRunner,
    policy: QuotaPolicy,
    sleeps: list[float],
    notifier: RecordingNotifier,
) -> Orchestrator:
    dev.name = "dev"
    rw1.name = "rw1"
    return Orchestrator(
        gitops=GitOps(repo_url=remote.as_uri(), work_root=tmp_path / "work"),
        dev_runner=dev,
        rw1_runner=rw1,
        fast_commands="fake-tests",
        shell=FakeShell([(0, "ok"), (0, "ok"), (0, "ok"), (0, "ok")]),
        roles_dir=roles_dir,
        max_loops=4,
        now=lambda: "2026-07-11T00:00:00Z",
        notifier=notifier,
        quota_policy=policy,
        sleep_fn=sleeps.append,
        clock=lambda: _NOW,
    )


def test_quota_wait_resumes_same_step_and_does_not_burn_rounds(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # dev hits quota twice, then succeeds; rw1 approves -> PUSHED
    dev = FakeRunner([_quota_result(), _quota_result(), "implemented"])
    rw1 = FakeRunner([verdict_json("APPROVED")])
    sleeps: list[float] = []
    notifier = RecordingNotifier()
    orch = _build(remote, tmp_path, roles_dir, dev, rw1, QuotaPolicy(), sleeps, notifier)

    terminal = orch.run("t1")

    assert terminal == "PUSHED"
    assert sleeps == [15 * 60.0, 30 * 60.0]
    wt = orch.gitops.task_worktree("t1")
    entries = TaskArtifacts(wt, "t1").read_log()
    waits = [e for e in entries if e["action"] == "quota_exhausted"]
    resumed = [e for e in entries if e["action"] == "quota_resumed"]
    assert len(waits) == 2 and len(resumed) == 1
    assert waits[0]["attempt"] == 0 and waits[1]["attempt"] == 1
    # only ONE developer round consumed despite two waits
    assert len([e for e in entries if e["action"] == "developer"]) == 1
    # exactly one QUOTA_WAIT notification for the whole episode
    assert notifier.sent.count(("t1", "QUOTA_WAIT")) == 1
    assert ("t1", "PUSHED") in notifier.sent


def test_quota_budget_exhaustion_is_terminal_with_handback(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    dev = FakeRunner([_quota_result(), _quota_result(), _quota_result(), "unused"])
    rw1 = FakeRunner([])
    sleeps: list[float] = []
    notifier = RecordingNotifier()
    policy = QuotaPolicy(max_wait=timedelta(minutes=40))  # 15 + 30 > 40
    orch = _build(remote, tmp_path, roles_dir, dev, rw1, policy, sleeps, notifier)

    terminal = orch.run("t1")

    assert terminal == "QUOTA_TIMEOUT"
    wt = orch.gitops.task_worktree("t1")
    artifacts = TaskArtifacts(wt, "t1")
    assert artifacts.read_text("handback.md") is not None
    assert ("t1", "QUOTA_TIMEOUT") in notifier.sent


def test_quota_timeout_is_retryable_a_rekickoff_resumes(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # QuotaBudget is per-run: after the quota window resets, a re-kickoff has
    # a fresh budget and must RESUME the task - QUOTA_TIMEOUT must not be a
    # sticky terminal that needs manual log surgery.
    dev = FakeRunner([_quota_result(), _quota_result(), _quota_result()])
    sleeps: list[float] = []
    policy = QuotaPolicy(max_wait=timedelta(minutes=40))
    orch = _build(
        remote, tmp_path, roles_dir, dev, FakeRunner([]), policy, sleeps, RecordingNotifier()
    )
    assert orch.run("t1") == "QUOTA_TIMEOUT"

    # re-kickoff after the reset: fresh orchestrator over the same worktree
    dev2 = FakeRunner(["implemented"])
    rw1_2 = FakeRunner([verdict_json("APPROVED")])
    notifier2 = RecordingNotifier()
    orch2 = _build(
        remote, tmp_path, roles_dir, dev2, rw1_2, QuotaPolicy(), [], notifier2
    )
    assert orch2.run("t1") == "PUSHED"
    assert len(dev2.calls) == 1
    assert ("t1", "PUSHED") in notifier2.sent
