"""Tests for report-only tasks (audit/investigate) + explorer/debugger palette."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orchestrator import TARGET_DIR_NAME
from orchestrator.artifacts import (
    EXPLORATION,
    FINDINGS,
    FINDINGS_PROPOSED,
    REPORT,
    TaskArtifacts,
)
from orchestrator.gitops import GitOps
from orchestrator.loop import Orchestrator
from orchestrator.run import _derive_status
from orchestrator.spec import parse_spec
from tests.fakes import (
    FakeRunner,
    FakeShell,
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
    (seed / TARGET_DIR_NAME / "specs" / "t1.md").write_text(
        "---\ntype: audit\n---\n# Audit create section\n", encoding="utf-8"
    )
    (seed / "src.py").write_text("x = 1\n", encoding="utf-8")
    write_policy_toml(seed)
    _git("-C", str(seed), "add", "-A")
    _git("-C", str(seed), *IDENTITY, "commit", "-m", "init")
    _git("-C", str(seed), "push", "origin", "HEAD:main")
    return bare


@pytest.fixture()
def roles_dir(tmp_path: Path) -> Path:
    roles = tmp_path / "roles"
    roles.mkdir()
    for name in ("developer", "rw1", "investigator", "verify", "explorer", "debugger"):
        (roles / f"{name}.md").write_text(f"{name.upper()} ROLE\n", encoding="utf-8")
    return roles


def _investigator_output(findings: list[dict[str, object]], fix_spec: str = "") -> str:
    return json.dumps(
        {"report": "# Audit report\n\nDetail.\n", "findings": findings, "fix_spec": fix_spec}
    )


def _orch(
    remote: Path,
    tmp_path: Path,
    roles_dir: Path,
    inv: FakeRunner,
    verify: FakeRunner,
    composition: tuple[str, ...] = ("investigator", "verify"),
) -> Orchestrator:
    inv.name, verify.name = "inv", "verify"
    return Orchestrator(
        gitops=GitOps(repo_url=remote.as_uri(), work_root=tmp_path / "work"),
        dev_runner=inv,
        rw1_runner=verify,
        composition=composition,
        fast_commands="unused",
        shell=FakeShell(),
        roles_dir=roles_dir,
        max_loops=4,
    )


def test_audit_flow_confirms_findings_and_pushes(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    proposed = [blocker(summary="sql injection in create"), blocker(summary="hallucinated")]
    confirmed = [blocker(summary="sql injection in create")]
    inv = FakeRunner([_investigator_output(proposed)])
    verify = FakeRunner([verdict_json("APPROVED", confirmed)])
    orch = _orch(remote, tmp_path, roles_dir, inv, verify)

    assert orch.run("t1") == "PUSHED"
    wt = orch.gitops.task_worktree("t1")
    art = TaskArtifacts(wt, "t1")
    assert art.read_text(REPORT) is not None
    assert len(art.read_json(FINDINGS_PROPOSED) or []) == 2
    stored = art.read_json(FINDINGS)
    assert stored is not None and len(stored) == 1  # refuted finding dropped
    assert stored[0]["summary"] == "sql injection in create"
    # verify prompt carried the proposed findings as data
    assert "hallucinated" in verify.calls[0].prompt
    # no test gates ran (design: docker gate skipped for report-only)
    log = art.read_log()
    assert [e["action"] for e in log] == ["investigator", "verify", "push", "terminal"]
    assert _git("-C", str(remote), "rev-parse", "refs/heads/t1")


def test_investigate_writes_draft_fix_spec(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    fix = "# Fix the injection\n\n## Goal\n...\n"
    inv = FakeRunner([_investigator_output([blocker()], fix_spec=fix)])
    verify = FakeRunner([verdict_json("APPROVED", [blocker()])])
    orch = _orch(remote, tmp_path, roles_dir, inv, verify)

    assert orch.run("t1") == "PUSHED"
    wt = orch.gitops.task_worktree("t1")
    fix_path = wt / TARGET_DIR_NAME / "specs" / "t1-fix.md"
    assert fix_path.is_file()
    spec = parse_spec(fix_path)
    assert spec.is_draft is True  # kickoff will refuse it until promoted


def test_investigate_forces_draft_over_investigator_supplied_status(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # an investigator emitting `status: ready` must NOT smuggle a runnable spec
    fix = "---\ntype: feature\nstatus: ready\n---\n# Fix\n\n## Goal\nx\n"
    inv = FakeRunner([_investigator_output([blocker()], fix_spec=fix)])
    verify = FakeRunner([verdict_json("APPROVED", [blocker()])])
    orch = _orch(remote, tmp_path, roles_dir, inv, verify)

    assert orch.run("t1") == "PUSHED"
    wt = orch.gitops.task_worktree("t1")
    spec = parse_spec(wt / TARGET_DIR_NAME / "specs" / "t1-fix.md")
    assert spec.is_draft is True  # forced, despite the emitted status: ready
    assert spec.task_type == "feature"  # other front-matter keys preserved


def test_force_draft_status_unit() -> None:
    from orchestrator.loop import _force_draft_status
    from orchestrator.spec import DRAFT_STATUS, parse_spec

    def _status(text: str, tmp: Path) -> str | None:
        p = tmp / "s.md"
        p.write_text(_force_draft_status(text), encoding="utf-8")
        return parse_spec(p).status

    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        assert _status("# no front matter\n", tmp) == DRAFT_STATUS
        assert _status("---\ntype: bug\n---\n# body\n", tmp) == DRAFT_STATUS
        assert _status("---\ntype: bug\nstatus: ready\n---\n# body\n", tmp) == DRAFT_STATUS


def test_report_only_path_guard_blocks_source_changes(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    class SourceTouchingRunner(FakeRunner):
        def run(self, prompt: str, cwd: Path, resume: str | None = None):
            (cwd / "src.py").write_text("x = 2  # sneaky edit\n", encoding="utf-8")
            return super().run(prompt, cwd, resume)

    inv = SourceTouchingRunner([_investigator_output([blocker()])])
    verify = FakeRunner([verdict_json("APPROVED", [blocker()])])
    orch = _orch(remote, tmp_path, roles_dir, inv, verify)

    assert orch.run("t1") == "PATH_GUARD_VIOLATION"
    wt = orch.gitops.task_worktree("t1")
    log = TaskArtifacts(wt, "t1").read_log()
    # terminal marker (Task: report-only failures must surface via
    # _derive_status) is appended right after the path_guard entry.
    assert log[-2]["action"] == "path_guard"
    assert "src.py" in log[-2]["detail"]
    assert log[-1] == {"ts": log[-1]["ts"], "action": "terminal", "outcome": "PATH_GUARD_VIOLATION"}
    spec_path = wt / TARGET_DIR_NAME / "specs" / "t1.md"
    status = _derive_status(spec_path, tmp_path / "work", queued=set())
    assert status == "failed:PATH_GUARD_VIOLATION"
    # nothing pushed
    rc = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "--verify", "refs/heads/t1"],
        capture_output=True,
    ).returncode
    assert rc != 0


def test_report_verify_is_bound_to_content(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # a verify round only confirms the exact report/findings it blessed;
    # rewriting the report afterward must invalidate the confirmation
    from orchestrator.loop import report_verify_confirmed

    proposed = [blocker(summary="real hole")]
    inv = FakeRunner([_investigator_output(proposed)])
    verify = FakeRunner([verdict_json("APPROVED", proposed)])
    orch = _orch(remote, tmp_path, roles_dir, inv, verify)
    assert orch.run("t1") == "PUSHED"

    wt = orch.gitops.task_worktree("t1")
    art = TaskArtifacts(wt, "t1")
    assert report_verify_confirmed(art) is True

    # tamper: rewrite the report after verify -> confirmation no longer holds
    art.write_text("report.md", "# TAMPERED after verify\n")
    assert report_verify_confirmed(art) is False


def test_investigator_error_run_output_is_never_parsed(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # An errored CLI run can still emit a complete, parseable payload (an
    # interrupted run's partial output). Like request_verdict, the loop must
    # treat a non-ok exit as untrustworthy: retry, never parse its text.
    from orchestrator.agents import AgentResult

    errored = AgentResult(
        text=_investigator_output([blocker(summary="from the FAILED run")]),
        session_id="inv-err",
        exit_reason="error",
        returncode=1,
    )
    inv = FakeRunner([errored, _investigator_output([blocker(summary="real finding")])])
    verify = FakeRunner([verdict_json("APPROVED", [blocker(summary="real finding")])])
    orch = _orch(remote, tmp_path, roles_dir, inv, verify)

    assert orch.run("t1") == "PUSHED"
    assert len(inv.calls) == 2  # transient crash retried, not terminal
    wt = orch.gitops.task_worktree("t1")
    report = TaskArtifacts(wt, "t1").read_json(FINDINGS_PROPOSED)
    assert report is not None and report[0]["summary"] == "real finding"


def test_investigator_malformed_output_is_retried_with_feedback(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    inv = FakeRunner(["not json at all", _investigator_output([blocker()])])
    verify = FakeRunner([verdict_json("APPROVED", [blocker()])])
    orch = _orch(remote, tmp_path, roles_dir, inv, verify)

    assert orch.run("t1") == "PUSHED"
    assert len(inv.calls) == 2
    assert "RETRY" in inv.calls[1].prompt  # error fed back, full prompt re-sent


def test_malformed_investigator_output_is_terminal(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # persistently malformed output (all retries burned) stays terminal
    inv = FakeRunner(["not json at all", "still not json", "nope"])
    verify = FakeRunner([])
    orch = _orch(remote, tmp_path, roles_dir, inv, verify)
    assert orch.run("t1") == "INVESTIGATOR_MALFORMED"

    # report-only terminal-failure states must write the `terminal` marker
    # (same as the main-path _terminal_failure) so a queued investigate/audit
    # task that dies here reads as failed:<state>, not in-progress/pushed.
    wt = orch.gitops.task_worktree("t1")
    log = TaskArtifacts(wt, "t1").read_log()
    assert log[-1] == {
        "ts": log[-1]["ts"], "action": "terminal", "outcome": "INVESTIGATOR_MALFORMED",
    }
    spec_path = wt / TARGET_DIR_NAME / "specs" / "t1.md"
    status = _derive_status(spec_path, tmp_path / "work", queued=set())
    assert status == "failed:INVESTIGATOR_MALFORMED"


def test_malformed_verify_output_is_terminal(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    inv = FakeRunner([_investigator_output([blocker()])])
    # request_verdict retries malformed output (default max_retries=2) before
    # giving up -> queue 3 malformed attempts total.
    verify = FakeRunner(["not json at all", "still not json", "nope"])
    orch = _orch(remote, tmp_path, roles_dir, inv, verify)
    assert orch.run("t1") == "VERIFY_MALFORMED"

    wt = orch.gitops.task_worktree("t1")
    log = TaskArtifacts(wt, "t1").read_log()
    assert log[-1] == {
        "ts": log[-1]["ts"], "action": "terminal", "outcome": "VERIFY_MALFORMED",
    }
    spec_path = wt / TARGET_DIR_NAME / "specs" / "t1.md"
    status = _derive_status(spec_path, tmp_path / "work", queued=set())
    assert status == "failed:VERIFY_MALFORMED"


def test_report_only_resume_skips_completed_investigator(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # run 1: investigator done, then verify raises (runner exhausted). The
    # catch-all records a RETRYABLE INTERNAL_ERROR (not a silent death).
    inv = FakeRunner([_investigator_output([blocker()])])
    verify = FakeRunner([])
    orch = _orch(remote, tmp_path, roles_dir, inv, verify)
    assert orch.run("t1") == "INTERNAL_ERROR"

    # run 2: resumes at verify (INTERNAL_ERROR is retryable); investigator NOT re-run
    inv2 = FakeRunner([])
    verify2 = FakeRunner([verdict_json("APPROVED", [blocker()])])
    orch2 = _orch(remote, tmp_path, roles_dir, inv2, verify2)
    assert orch2.run("t1") == "PUSHED"
    assert inv2.calls == []


# --- explorer / debugger palette (design S3, Slice 3) -------------------------


def test_explorer_runs_once_before_developer_and_feeds_prompt(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    dev = FakeRunner(["explored the code: root cause in src.py", "implemented"])
    rw1 = FakeRunner([verdict_json("APPROVED")])
    orch = Orchestrator(
        gitops=GitOps(repo_url=remote.as_uri(), work_root=tmp_path / "work"),
        dev_runner=dev,
        rw1_runner=rw1,
        composition=("explorer", "developer", "rw1"),
        fast_commands="t",
        shell=FakeShell(results=[(0, "g")]),
        roles_dir=roles_dir,
        max_loops=4,
    )
    dev.name, rw1.name = "dev", "rw1"

    assert orch.run("t1") == "PUSHED"
    wt = orch.gitops.task_worktree("t1")
    art = TaskArtifacts(wt, "t1")
    assert art.read_text(EXPLORATION) == "explored the code: root cause in src.py"
    log = art.read_log()
    assert [e["action"] for e in log] == [
        "explorer", "developer", "fast_tests", "rw1", "push", "terminal",
    ]
    # exploration embedded in the developer prompt
    assert "root cause in src.py" in dev.calls[1].prompt


def test_run_explorer_runs_once_and_returns_text(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    dev = FakeRunner(["EXPLORATION TEXT HERE"])
    rw1 = FakeRunner([])
    orch = Orchestrator(
        gitops=GitOps(repo_url=remote.as_uri(), work_root=tmp_path / "work"),
        dev_runner=dev,
        rw1_runner=rw1,
        composition=("explorer", "developer", "rw1"),
        fast_commands="t",
        shell=FakeShell(),
        roles_dir=roles_dir,
        max_loops=4,
    )
    dev.name, rw1.name = "dev", "rw1"

    text = orch.run_explorer("t1")
    assert "EXPLORATION TEXT HERE" in text
    wt = orch.gitops.task_worktree("t1")
    log = TaskArtifacts(wt, "t1").read_log()
    assert sum(1 for e in log if e.get("action") == "explorer") == 1

    # idempotent: second call does not append a second explorer entry
    orch.run_explorer("t1")
    log2 = TaskArtifacts(wt, "t1").read_log()
    assert sum(1 for e in log2 if e.get("action") == "explorer") == 1


def _explorer_orch(
    remote: Path, tmp_path: Path, roles_dir: Path, dev: FakeRunner, rw1: FakeRunner
) -> Orchestrator:
    dev.name, rw1.name = "dev", "rw1"
    return Orchestrator(
        gitops=GitOps(repo_url=remote.as_uri(), work_root=tmp_path / "work"),
        dev_runner=dev,
        rw1_runner=rw1,
        composition=("explorer", "developer", "rw1"),
        fast_commands="t",
        shell=FakeShell(results=[(0, "g")]),
        roles_dir=roles_dir,
        max_loops=4,
    )


def test_explorer_transient_error_is_retried(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # one crashed CLI run must not poison the exploration artifact - the text
    # of a non-ok run is never used; the run is retried
    from orchestrator.agents import AgentResult

    errored = AgentResult(
        text="half-written garbage", session_id="x", exit_reason="error", returncode=1
    )
    dev = FakeRunner([errored, "explored: root cause in src.py", "implemented"])
    rw1 = FakeRunner([verdict_json("APPROVED")])
    orch = _explorer_orch(remote, tmp_path, roles_dir, dev, rw1)

    assert orch.run("t1") == "PUSHED"
    wt = orch.gitops.task_worktree("t1")
    art = TaskArtifacts(wt, "t1")
    assert art.read_text(EXPLORATION) == "explored: root cause in src.py"
    explorer_entries = [e for e in art.read_log() if e["action"] == "explorer"]
    assert [e["outcome"] for e in explorer_entries] == ["ok"]


def test_explorer_persistent_failure_continues_without_exploration(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    # exploration is advisory context, not a gate: when the CLI keeps dying,
    # the loop records the failure and proceeds to the developer without it
    from orchestrator.agents import AgentResult

    def _err() -> AgentResult:
        return AgentResult(
            text="garbage", session_id=None, exit_reason="error", returncode=1
        )

    dev = FakeRunner([_err(), _err(), _err(), "implemented"])
    rw1 = FakeRunner([verdict_json("APPROVED")])
    orch = _explorer_orch(remote, tmp_path, roles_dir, dev, rw1)

    assert orch.run("t1") == "PUSHED"
    wt = orch.gitops.task_worktree("t1")
    art = TaskArtifacts(wt, "t1")
    assert art.read_text(EXPLORATION) is None  # no garbage artifact written
    explorer_entries = [e for e in art.read_log() if e["action"] == "explorer"]
    assert [e["outcome"] for e in explorer_entries] == ["error"]
    # the developer prompt carried no exploration section
    assert "Exploration findings" not in dev.calls[3].prompt


def test_debugger_lens_used_for_test_failure_round(
    remote: Path, tmp_path: Path, roles_dir: Path
) -> None:
    dev = FakeRunner(["implemented", "debugged and fixed"])
    rw1 = FakeRunner([verdict_json("APPROVED")])
    orch = Orchestrator(
        gitops=GitOps(repo_url=remote.as_uri(), work_root=tmp_path / "work"),
        dev_runner=dev,
        rw1_runner=rw1,
        composition=("developer", "debugger", "rw1"),
        fast_commands="t",
        shell=FakeShell(results=[(1, "FAILED test_q"), (0, "g")]),
        roles_dir=roles_dir,
        max_loops=4,
    )
    dev.name, rw1.name = "dev", "rw1"

    assert orch.run("t1") == "PUSHED"
    # first round: developer role; fix round: debugger role, same session
    assert "DEVELOPER ROLE" in dev.calls[0].prompt
    assert "DEBUGGER ROLE" in dev.calls[1].prompt
    assert dev.calls[1].resume == "dev-s1"
    wt = orch.gitops.task_worktree("t1")
    log = TaskArtifacts(wt, "t1").read_log()
    dev_entries = [e for e in log if e["action"] == "developer"]
    assert [e["role"] for e in dev_entries] == ["developer", "debugger"]
