"""Integration tests for the slice-1 orchestrator loop (fake runners, real git)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator import TARGET_DIR_NAME
from orchestrator.artifacts import HUMAN_SUMMARY, RW1_VERDICT, TaskArtifacts
from orchestrator.gitops import GitOps
from orchestrator.loop import Orchestrator, derive_resume_point
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


def _orchestrator(
    remote: Path,
    tmp_path: Path,
    roles_dir: Path,
    dev: FakeRunner,
    rw1: FakeRunner,
    shell: FakeShell,
    max_loops: int = 4,
    setup_commands: str = "",
) -> Orchestrator:
    dev.name = "dev"
    rw1.name = "rw1"
    return Orchestrator(
        gitops=GitOps(repo_url=remote.as_uri(), work_root=tmp_path / "work"),
        dev_runner=dev,
        rw1_runner=rw1,
        fast_commands="fake-tests",
        setup_commands=setup_commands,
        shell=shell,
        roles_dir=roles_dir,
        max_loops=max_loops,
        now=lambda: "2026-07-05T00:00:00Z",
    )


def _dev_output(fake_file: str = "impl.py") -> str:
    """Developer 'work': FakeRunner can't edit files, so the test pre-plants
    changes via the shell fake instead. Returned text is the dev report."""
    return f"implemented; touched {fake_file}"


def test_happy_path_pushes_and_logs(remote: Path, tmp_path: Path, roles_dir: Path) -> None:
    dev = FakeRunner([_dev_output()])
    rw1 = FakeRunner([verdict_json("APPROVED")])
    shell = FakeShell(results=[(0, "all green")])
    orch = _orchestrator(remote, tmp_path, roles_dir, dev, rw1, shell)

    # plant a file change the developer "made" before the loop commits it
    wt = orch.gitops.task_worktree("t1")
    (wt / "impl.py").write_text("x = 1\n", encoding="utf-8")

    terminal = orch.run("t1")

    assert terminal == "PUSHED"
    artifacts = TaskArtifacts(wt, "t1")
    actions = [e["action"] for e in artifacts.read_log()]
    assert actions == ["developer", "fast_tests", "rw1", "push", "terminal"]
    stored_verdict = artifacts.read_json(RW1_VERDICT)
    assert stored_verdict is not None
    assert stored_verdict["verdict"] == "APPROVED"
    summary = (artifacts.dir / HUMAN_SUMMARY).read_text(encoding="utf-8")
    assert "t1" in summary and "PUSHED" in summary
    # remote branch exists and carries the artifacts
    assert _git("-C", str(remote), "rev-parse", "refs/heads/t1")


def test_setup_bootstraps_fresh_worktree_before_fast_tests(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # A fresh worktree has no .venv; the setup command must run BEFORE the first
    # fast_tests so `. .venv/bin/activate` finds one (else every round dies on
    # "activate: No such file" and the task burns rounds to the cap).
    dev = FakeRunner([_dev_output()])
    rw1 = FakeRunner([verdict_json("APPROVED")])
    shell = FakeShell(results=[(0, "venv bootstrapped"), (0, "all green")])
    orch = _orchestrator(
        remote, tmp_path, roles_dir, dev, rw1, shell, setup_commands="make-venv"
    )
    wt = orch.gitops.task_worktree("t1")
    (wt / "impl.py").write_text("x = 1\n", encoding="utf-8")

    assert orch.run("t1") == "PUSHED"
    # setup ran first, fast_tests second - order is the whole point
    assert [c[0] for c in shell.calls] == ["make-venv", "fake-tests"]


def test_setup_runs_once_across_rounds(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # The bootstrap is per-worktree, not per-round: a fast_tests failure that
    # sends the loop back to the developer must NOT re-run setup next round.
    dev = FakeRunner([_dev_output(), _dev_output()])
    rw1 = FakeRunner([verdict_json("APPROVED")])
    shell = FakeShell(
        results=[(0, "venv bootstrapped"), (1, "FAILED test_x"), (0, "green")]
    )
    orch = _orchestrator(
        remote, tmp_path, roles_dir, dev, rw1, shell, setup_commands="make-venv"
    )
    wt = orch.gitops.task_worktree("t1")
    (wt / "impl.py").write_text("x = 1\n", encoding="utf-8")

    assert orch.run("t1") == "PUSHED"
    assert [c[0] for c in shell.calls] == ["make-venv", "fake-tests", "fake-tests"]
    # the marker lives under work_root, NOT the worktree, so commit_all -A never
    # lands it on the branch
    assert (tmp_path / "work" / "setup-done" / "t1").exists()
    assert not (wt / "setup-done").exists()


def test_empty_setup_commands_is_noop(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # No setup command (a target that self-bootstraps inside TEST_COMMANDS, or
    # the loop's direct-construction default): fast_tests runs straight, with no
    # extra shell call ahead of it.
    dev = FakeRunner([_dev_output()])
    rw1 = FakeRunner([verdict_json("APPROVED")])
    shell = FakeShell(results=[(0, "all green")])
    orch = _orchestrator(remote, tmp_path, roles_dir, dev, rw1, shell)
    wt = orch.gitops.task_worktree("t1")
    (wt / "impl.py").write_text("x = 1\n", encoding="utf-8")

    assert orch.run("t1") == "PUSHED"
    assert [c[0] for c in shell.calls] == ["fake-tests"]


def test_fast_test_failure_feeds_output_to_next_dev_prompt(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    dev = FakeRunner([_dev_output(), _dev_output()])
    rw1 = FakeRunner([verdict_json("APPROVED")])
    shell = FakeShell(results=[(1, "FAILED test_alpha - assert 1 == 2"), (0, "green")])
    orch = _orchestrator(remote, tmp_path, roles_dir, dev, rw1, shell)

    assert orch.run("t1") == "PUSHED"
    assert len(dev.calls) == 2
    assert "FAILED test_alpha" in dev.calls[1].prompt
    # dev session resumed, not restarted
    assert dev.calls[1].resume == "dev-s1"


def test_changes_requested_feeds_verdict_to_next_dev_prompt(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    dev = FakeRunner([_dev_output(), _dev_output()])
    rw1 = FakeRunner(
        [
            verdict_json("CHANGES_REQUESTED", [blocker(summary="races on save")]),
            verdict_json("APPROVED"),
        ]
    )
    shell = FakeShell(results=[(0, "green"), (0, "green")])
    orch = _orchestrator(remote, tmp_path, roles_dir, dev, rw1, shell)

    assert orch.run("t1") == "PUSHED"
    assert "races on save" in dev.calls[1].prompt
    # rw1 session resumed for the re-review
    assert rw1.calls[1].resume == "rw1-s1"


def test_sessions_never_shared(remote: Path, tmp_path: Path, roles_dir: Path) -> None:
    dev = FakeRunner([_dev_output(), _dev_output()])
    rw1 = FakeRunner(
        [verdict_json("CHANGES_REQUESTED", [blocker()]), verdict_json("APPROVED")]
    )
    shell = FakeShell(results=[(0, "g"), (0, "g")])
    orch = _orchestrator(remote, tmp_path, roles_dir, dev, rw1, shell)
    orch.run("t1")

    dev_resumes = {c.resume for c in dev.calls if c.resume}
    rw1_resumes = {c.resume for c in rw1.calls if c.resume}
    assert all(r.startswith("dev-") for r in dev_resumes)
    assert all(r.startswith("rw1-") for r in rw1_resumes)


def test_cap_reached_writes_summary_and_stops(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    dev = FakeRunner([_dev_output()] * 4)
    rw1 = FakeRunner(
        [verdict_json("CHANGES_REQUESTED", [blocker(summary=f"issue {i}")]) for i in range(4)]
    )
    shell = FakeShell(results=[(0, "g")] * 4)
    orch = _orchestrator(remote, tmp_path, roles_dir, dev, rw1, shell)

    terminal = orch.run("t1")

    assert terminal == "CAP_REACHED"
    assert len(dev.calls) == 4
    wt = orch.gitops.task_worktree("t1")
    summary = (TaskArtifacts(wt, "t1").dir / HUMAN_SUMMARY).read_text(encoding="utf-8")
    assert "CAP_REACHED" in summary
    assert "issue 0" in summary  # what was tried, per round
    # terminal marker is the log's last entry...
    log = TaskArtifacts(wt, "t1").read_log()
    assert log[-1]["action"] == "terminal"
    assert log[-1]["outcome"] == "CAP_REACHED"
    # ...but is NOT a _PHASE_ACTIONS entry, so resume still derives cap_reached
    # from the log as it stood before the marker (round-counting/fingerprints
    # ignore it).
    resumed = derive_resume_point(log, max_loops=4)
    assert resumed.phase == "cap_reached"


def test_failure_terminal_pushes_handback_to_hub(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # ntfy says "see handback" - on a rebuilt disposable VPS the handback
    # exists ONLY if the failure terminal pushed it to the hub.
    dev = FakeRunner([_dev_output()] * 4)
    rw1 = FakeRunner(
        [verdict_json("CHANGES_REQUESTED", [blocker(summary=f"issue {i}")]) for i in range(4)]
    )
    shell = FakeShell(results=[(0, "g")] * 4)
    orch = _orchestrator(remote, tmp_path, roles_dir, dev, rw1, shell)

    assert orch.run("t1") == "CAP_REACHED"
    # the hub branch exists and carries handback + terminal marker
    hub_files = _git("-C", str(remote), "ls-tree", "-r", "--name-only", "refs/heads/t1")
    assert f"{TARGET_DIR_NAME}/tasks/t1/handback.md" in hub_files
    hub_log = _git(
        "-C", str(remote), "show",
        f"refs/heads/t1:{TARGET_DIR_NAME}/tasks/t1/iteration-log.jsonl",
    )
    assert '"CAP_REACHED"' in hub_log


class _PushBlipGitOps(GitOps):
    """Fails the Nth push call with a GitError (a network blip)."""

    def __init__(self, *args: object, fail_on: int, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # pyright: ignore[reportArgumentType]
        self._fail_on = fail_on
        self.push_calls = 0

    def push(self, wt: Path, task_id: str) -> None:
        self.push_calls += 1
        if self.push_calls == self._fail_on:
            from orchestrator.gitops import GitError

            raise GitError("simulated network blip on push")
        super().push(wt, task_id)


def test_push_tail_crash_is_resumable_and_never_loses_the_notify(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # A failure AFTER the deliverable push must not strand the run: the ntfy
    # for PUSHED fires before the sticky marker is recorded (at-least-once),
    # and a re-kickoff completes the tail so summary + log reach the hub.
    from orchestrator.handoff import NtfyNotifier

    class RecordingNotifier(NtfyNotifier):
        def __init__(self) -> None:
            super().__init__(topic=None)
            self.sent: list[tuple[str, str]] = []

        def notify(self, task_id: str, terminal_state: str) -> None:
            self.sent.append((task_id, terminal_state))

    dev = FakeRunner([_dev_output()])
    rw1 = FakeRunner([verdict_json("APPROVED")])
    shell = FakeShell(results=[(0, "green")])
    notifier1 = RecordingNotifier()
    orch = Orchestrator(
        gitops=_PushBlipGitOps(
            repo_url=remote.as_uri(), work_root=tmp_path / "work", fail_on=2
        ),
        dev_runner=dev,
        rw1_runner=rw1,
        fast_commands="fake-tests",
        shell=shell,
        roles_dir=roles_dir,
        max_loops=4,
        notifier=notifier1,
        now=lambda: "2026-07-05T00:00:00Z",
    )
    dev.name, rw1.name = "dev", "rw1"
    assert orch.run("t1") == "INTERNAL_ERROR"
    # the deliverable push succeeded and the PUSHED ntfy already fired -
    # the blip hit only the artifact push afterwards
    assert ("t1", "PUSHED") in notifier1.sent

    # re-kickoff: a fresh orchestrator finishes the tail idempotently
    dev2 = FakeRunner([])
    rw1_2 = FakeRunner([])
    orch2 = _orchestrator(remote, tmp_path, roles_dir, dev2, rw1_2, FakeShell())
    assert orch2.run("t1") == "PUSHED"
    assert dev2.calls == [] and rw1_2.calls == []  # no phase re-ran
    # the hub now has the full record: summary + terminal marker
    hub_files = _git("-C", str(remote), "ls-tree", "-r", "--name-only", "refs/heads/t1")
    assert f"{TARGET_DIR_NAME}/tasks/t1/human-summary.md" in hub_files
    hub_log = _git(
        "-C", str(remote), "show",
        f"refs/heads/t1:{TARGET_DIR_NAME}/tasks/t1/iteration-log.jsonl",
    )
    assert '"action": "terminal"' in hub_log and '"PUSHED"' in hub_log


class _ExplodingRunner(FakeRunner):
    def run(self, prompt: str, cwd: Path, resume: str | None = None):
        raise RuntimeError("killed mid-run")


def test_resume_after_kill_continues_from_artifacts(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # Run 1: developer ok, fast tests fail, then dev round 2 raises (FakeRunner
    # exhausted). The broad catch-all turns that unexpected exception into a
    # RETRYABLE INTERNAL_ERROR terminal (not a silent death), so run() returns
    # INTERNAL_ERROR rather than propagating.
    dev = FakeRunner([_dev_output()])
    rw1 = FakeRunner([])
    shell = FakeShell(results=[(1, "FAILED test_x")])
    orch = _orchestrator(remote, tmp_path, roles_dir, dev, rw1, shell)
    assert orch.run("t1") == "INTERNAL_ERROR"

    # Run 2: fresh orchestrator over the same remote; must resume from artifacts.
    dev2 = FakeRunner([_dev_output()])
    rw1_2 = FakeRunner([verdict_json("APPROVED")])
    shell2 = FakeShell(results=[(0, "green")])
    orch2 = _orchestrator(remote, tmp_path, roles_dir, dev2, rw1_2, shell2)
    assert orch2.run("t1") == "PUSHED"

    # exactly one more developer pass; round-1 entries not repeated
    wt = orch2.gitops.task_worktree("t1")
    log = TaskArtifacts(wt, "t1").read_log()
    dev_entries = [e for e in log if e["action"] == "developer"]
    assert len(dev_entries) == 2
    # resume was artifact-based, not session-based: new dev session started fresh
    assert dev2.calls[0].resume == "dev-s1"  # session token reused as optimization


def test_rw1_changes_requested_without_blockers_is_retried_not_reworked(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # A CHANGES_REQUESTED verdict with zero blockers is contradictory (nothing
    # binding to address). It must be handled like any malformed verdict -
    # retried inside request_verdict - NOT accepted as a rework trigger, which
    # would burn developer rounds on an empty verdict all the way to CAP_REACHED.
    dev = FakeRunner([_dev_output()])
    rw1 = FakeRunner(
        [verdict_json("CHANGES_REQUESTED"), verdict_json("APPROVED")]
    )
    shell = FakeShell(results=[(0, "green")])
    orch = _orchestrator(remote, tmp_path, roles_dir, dev, rw1, shell)

    assert orch.run("t1") == "PUSHED"
    assert len(dev.calls) == 1  # no rework round happened
    assert len(rw1.calls) == 2
    assert "rejected by the schema validator" in rw1.calls[1].prompt


def test_malformed_verdict_retry_then_success(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    dev = FakeRunner([_dev_output()])
    rw1 = FakeRunner(["THIS IS NOT JSON", verdict_json("APPROVED")])
    shell = FakeShell(results=[(0, "green")])
    orch = _orchestrator(remote, tmp_path, roles_dir, dev, rw1, shell)

    assert orch.run("t1") == "PUSHED"
    assert len(rw1.calls) == 2
    assert "rejected by the schema validator" in rw1.calls[1].prompt
