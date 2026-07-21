"""H5 (C2+C3 audit): the --local Director-fix route vs the policy recompute.

Sub-case (a): a fix committed ON TOP of the held branch advances code_sha
past the VPS-written state.json -> merge_check.check() = state_sha_mismatch.
Sub-case (b): a fix authored OFF local main (the documented worktree recipe)
carries no merge-decision.json at all -> missing_merge_decision. So the
plain check() can never pass on a Director fix tree - which is why the
--local route skipped it (attestation N/A). That skip also dropped the
policy recompute WHOLESALE: an honestly-STOPPED branch (test_files_deleted,
declared high risk, senior deadlock) could be laundered into a merge by
committing a trivial fix on top and re-judging with --local (the S0/H1
stop-check did not apply under --local).

These tests pin both halves: check() still rejects the fix trees (the
non-local bar is untouched), and the gate chain still fails closed on every
stop that IS recomputable from the fixed tree, via the local-mode recompute
(merge_check_local.check_local_fix).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator import TARGET_DIR_NAME
from orchestrator.artifacts import LOG, RW1_VERDICT, TaskArtifacts
from orchestrator.local_merge import (
    BROKEN,
    RISK_DECISION,
    ArtifactAttestationState,
    decide,
    gather_gates,
)
from orchestrator.merge_check import check
from orchestrator.merge_check_local import check_local_fix
from orchestrator.policy import L2, L3
from tests.fakes import verdict_json

# Shared --local helpers from the local-merge suite (same repo shape, same
# green-gate fakes) so the two files cannot drift on the fixture recipe.
from tests.test_local_merge import (
    _ID,
    _g,
    _green_shell,
    _local_fix_commit,
    _push_ready_branch,
    _tools,
    make_local_repo,
)


@pytest.fixture()
def local_repo(tmp_path: Path) -> Path:
    return make_local_repo(tmp_path)


def _fix_atop_branch(
    local_repo: Path, tmp_path: Path, name: str = "fix-atop"
) -> tuple[Path, str]:
    """Sub-case (a): the Director commits a one-line fix ON TOP of the fetched
    held branch (t1 with its VPS artifacts), exactly as the BROKEN digest
    instructs. Returns (worktree_path, fix_sha)."""
    _g("-C", str(local_repo), "fetch", "origin", "t1")
    fix = tmp_path / name
    _g("-C", str(local_repo), "worktree", "add", "-b", name, str(fix), "origin/t1")
    (fix / "myapp" / "api_helper.py").write_text("x = 2\n", encoding="utf-8")
    _g("-C", str(fix), "add", "-A")
    _g("-C", str(fix), *_ID, "commit", "-m", "fix: one-line director fix")
    sha = _g("-C", str(fix), "rev-parse", "HEAD")
    return fix, sha


def _push_stopped_branch_deleting_a_test(local_repo: Path, tmp_path: Path) -> None:
    """Simulate the VPS honestly stopping: t1 deletes tests/test_del.py and
    commits a truthful stop_before_merge decision, then pushes."""
    # main gains the test file first, so the branch diff DELETES it
    (local_repo / "tests").mkdir()
    (local_repo / "tests" / "test_del.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    _g("-C", str(local_repo), "add", "-A")
    _g("-C", str(local_repo), *_ID, "commit", "-m", "add a test on main")
    _g("-C", str(local_repo), "push", "origin", "main")

    wt = tmp_path / "vps-stop"
    bare = str(tmp_path / "remote.git")
    _g("clone", bare, str(wt))
    _g("-C", str(wt), "checkout", "-b", "t1")
    (wt / "myapp").mkdir(exist_ok=True)
    (wt / "myapp" / "api_helper.py").write_text("x = 1\n", encoding="utf-8")
    (wt / "tests" / "test_del.py").unlink()
    art = TaskArtifacts(wt, "t1")
    art.write_json(
        "merge-decision.json",
        {
            "decision": "stop_before_merge",
            "risk_level": "low",
            "reasons": ["test_files_deleted: tests/test_del.py"],
        },
    )
    art.write_json("state.json", {"head_sha": "x"})
    _g("-C", str(wt), "add", "-A")
    _g("-C", str(wt), *_ID, "commit", "-m", "work (deletes a test)")
    _g("-C", str(wt), "push", "origin", "t1")


# --- H5 repro evidence: the plain check() can never pass on a fix tree -------


def test_h5a_plain_check_rejects_a_fix_atop_the_branch(
    local_repo: Path, tmp_path: Path
) -> None:
    # Sub-case (a): the fix advanced code_sha; the VPS state.json cannot follow.
    _push_ready_branch(local_repo, tmp_path, sensitive=False)
    fix, _sha = _fix_atop_branch(local_repo, tmp_path)
    code, msg = check(fix, "origin/main", "t1")
    assert code == 1
    assert "state_sha_mismatch" in msg
    # the local-mode recompute accommodates exactly that staleness - and only it
    code, msg = check_local_fix(fix, "origin/main", "t1")
    assert code == 0, msg


def test_h5b_plain_check_rejects_a_fix_authored_off_local_main(
    local_repo: Path, tmp_path: Path
) -> None:
    # Sub-case (b): the documented worktree recipe - no merge-decision.json.
    fix, _sha = _local_fix_commit(local_repo, tmp_path)
    code, msg = check(fix, "origin/main", "t1")
    assert code == 1
    assert "missing_merge_decision" in msg
    code, msg = check_local_fix(fix, "origin/main", "t1")
    assert code == 0, msg


# --- the actual defect: --local skipped the policy recompute wholesale -------


def test_local_fix_cannot_launder_a_recomputable_stop(
    local_repo: Path, tmp_path: Path
) -> None:
    """A branch the VPS honestly STOPPED (test_files_deleted) is held on the
    remote path (S0/H1). A trivial Director fix on top re-judged with --local
    must NOT clear that stop: the deletion is still in the fixed tree's diff
    and is recomputable with no VPS artifact needed. Fail closed: BROKEN."""
    _push_stopped_branch_deleting_a_test(local_repo, tmp_path)
    fix, _sha = _fix_atop_branch(local_repo, tmp_path, name="fix-stop")

    tools = _tools(
        local_repo,
        _green_shell(),
        security_outputs=[verdict_json("APPROVED")],
        rw2_outputs=[verdict_json("APPROVED")],
    )
    gates = gather_gates("t1", local_repo, tmp_path / "mw", tools, local_ref=str(fix))

    assert gates.artifact_attestation.state is ArtifactAttestationState.FAILED
    assert "recomputed_stop_before_merge" in gates.artifact_attestation.detail
    assert "test_files_deleted" in gates.artifact_attestation.detail
    v = decide("t1", gates)
    assert v.decision == "hold"
    assert v.kind == BROKEN


def test_local_fix_cannot_launder_an_inherited_declared_high_risk(
    local_repo: Path, tmp_path: Path
) -> None:
    """A reviewer-DECLARED high risk (rw1 verdict artifact inherited on the fix
    tree, non-sensitive paths) has no other local manifestation - the local
    recompute must still stop on it (S0/H1 under --local)."""
    _push_ready_branch(local_repo, tmp_path, sensitive=False)
    fix, _sha = _fix_atop_branch(local_repo, tmp_path, name="fix-risk")
    TaskArtifacts(fix, "t1").write_json(RW1_VERDICT, {"risk_level": "high"})
    _g("-C", str(fix), "add", "-A")
    _g("-C", str(fix), *_ID, "commit", "-m", "carry rw1 verdict")

    tools = _tools(
        local_repo,
        _green_shell(),
        security_outputs=[verdict_json("APPROVED")],
        rw2_outputs=[verdict_json("APPROVED")],
    )
    gates = gather_gates("t1", local_repo, tmp_path / "mw", tools, local_ref=str(fix))

    assert gates.artifact_attestation.state is ArtifactAttestationState.FAILED
    assert "high_risk" in gates.artifact_attestation.detail
    assert decide("t1", gates).kind == BROKEN


def test_local_fix_cannot_launder_an_inherited_senior_deadlock(
    local_repo: Path, tmp_path: Path
) -> None:
    """A senior deadlock recorded in the inherited iteration log is recomputable
    on the fix tree and must still stop the --local route."""
    _push_ready_branch(local_repo, tmp_path, sensitive=False)
    fix, _sha = _fix_atop_branch(local_repo, tmp_path, name="fix-deadlock")
    log = fix / TARGET_DIR_NAME / "tasks" / "t1" / LOG
    log.write_text(
        json.dumps({"action": "senior", "outcome": "deadlock"}) + "\n",
        encoding="utf-8",
    )
    _g("-C", str(fix), "add", "-A")
    _g("-C", str(fix), *_ID, "commit", "-m", "carry iteration log")

    tools = _tools(
        local_repo,
        _green_shell(),
        security_outputs=[verdict_json("APPROVED")],
        rw2_outputs=[verdict_json("APPROVED")],
    )
    gates = gather_gates("t1", local_repo, tmp_path / "mw", tools, local_ref=str(fix))

    assert gates.artifact_attestation.state is ArtifactAttestationState.FAILED
    assert "senior_escalation_without_clean_verdict" in gates.artifact_attestation.detail
    assert decide("t1", gates).kind == BROKEN


# --- acceptance: the sanctioned escape hatch actually works ------------------


def test_ac1_one_line_fix_atop_held_branch_passes_the_gate_chain(
    local_repo: Path, tmp_path: Path
) -> None:
    """AC1 (H5 sub-case a): fix committed on top of the held branch, green
    gates -> the chain passes and would merge the judged sha."""
    _push_ready_branch(local_repo, tmp_path, sensitive=False)
    fix, sha = _fix_atop_branch(local_repo, tmp_path, name="fix-ac1")

    tools = _tools(
        local_repo,
        _green_shell(),
        security_outputs=[verdict_json("APPROVED")],
        rw2_outputs=[verdict_json("APPROVED")],
    )
    gates = gather_gates("t1", local_repo, tmp_path / "mw", tools, local_ref=str(fix))

    assert gates.head_sha == sha
    assert not gates.artifact_attestation.failed
    assert gates.blast == L2 and gates.tests_passed
    assert decide("t1", gates).decision == "merge"


def test_local_sensitive_fix_routes_to_risk_decision_not_broken(
    local_repo: Path, tmp_path: Path
) -> None:
    """Sensitive/security paths are deliberately NOT a stop in the local
    recompute: their local manifestation is blast L3 -> RISK_DECISION (a typed
    human confirmation), and stopping here instead would make every L3 --local
    fix an unclearable BROKEN hold - H5 all over again (when laddy dogfoods
    itself almost every code change is L3)."""
    fix, _sha = _local_fix_commit(local_repo, tmp_path, sensitive=True)

    tools = _tools(
        local_repo,
        _green_shell(),
        security_outputs=[verdict_json("APPROVED")],
        rw2_outputs=[],
    )
    gates = gather_gates("t1", local_repo, tmp_path / "mw", tools, local_ref=str(fix))

    assert gates.blast == L3
    assert not gates.artifact_attestation.failed
    v = decide("t1", gates)
    assert v.decision == "hold"
    assert v.kind == RISK_DECISION


def test_local_recompute_keeps_the_report_only_guard(
    local_repo: Path, tmp_path: Path
) -> None:
    """Report-only parity: when the fix tree's spec is report-only, the full
    report-only recompute (path guard included) still applies - a source file
    in the diff is a recomputable stop, --local or not."""
    fix, _sha = _local_fix_commit(local_repo, tmp_path)
    spec = fix / TARGET_DIR_NAME / "specs" / "t1.md"
    spec.write_text("---\ntype: audit\n---\n# t1\n", encoding="utf-8")
    _g("-C", str(fix), "add", "-A")
    _g("-C", str(fix), *_ID, "commit", "-m", "spec: report-only")

    code, msg = check_local_fix(fix, "origin/main", "t1")
    assert code == 1
    assert "recomputed_stop_before_merge" in msg
    assert "path_guard_violation" in msg


def test_ac2_remote_path_does_not_use_the_local_recompute(
    local_repo: Path, tmp_path: Path
) -> None:
    """AC2: the non---local (VPS-authored) route still runs the full
    merge_check attestation collaborator and never the local recompute."""
    _push_ready_branch(local_repo, tmp_path, sensitive=False)
    calls: list[str] = []
    tools = _tools(
        local_repo,
        _green_shell(),
        security_outputs=[verdict_json("APPROVED")],
        rw2_outputs=[verdict_json("APPROVED")],
    )
    real_merge_check = tools.merge_check_fn

    def spy(repo: Path, base: str, task: str) -> tuple[int, str]:
        calls.append(task)
        return real_merge_check(repo, base, task)

    tools.merge_check_fn = spy
    tools.local_check_fn = lambda repo, base, task: pytest.fail(
        "the local recompute must not run on the fetched-branch path"
    )
    gates = gather_gates("t1", local_repo, tmp_path / "mw", tools)

    assert calls == ["t1"]
    assert gates.artifact_attestation.state is ArtifactAttestationState.PASSED
