"""Policy recompute for the --local Director-fix route (C2+C3 audit, H5).

The sanctioned escape hatch ("fix locally, re-judge with --local") judges a
Director-authored commit. merge_check.check() can never pass on that tree:
a fix committed ON TOP of the held branch advances code_sha past the
VPS-written state.json (state_sha_mismatch), and a fix authored OFF local
main (the documented worktree recipe) carries no merge-decision.json at all
(missing_merge_decision). Those equality checks attest the VPS artifact
chain, which a trusted local fix makes stale BY CONSTRUCTION - so the
--local route used to skip the policy recompute wholesale (attestation
N/A). That skip also dropped the S0/H1 stop check: an honestly-STOPPED
branch (test_files_deleted, declared high risk, senior deadlock) could be
laundered into a merge by committing a trivial fix on top and re-judging
with --local.

check_local_fix() is that missing recompute, scoped to exactly what the
trusted route changes and nothing more. The route is trusted because the
--local flag comes from the Director's own CLI invocation, the judged sha
is the merged sha (TOCTOU pin), and the dirty-tree guard refused anything
uncommitted before any gate ran.

NOT required here (each is invalidated by the fix commit by construction,
and each has a fresh trusted-local replacement in gather_gates):
  - state.json head_sha equality      -> the binding gate re-runs at the new sha
  - committed-decision equality       -> the decision described the older tip
  - sha-pinned rw1/rw2/authoritative  -> local rw2 re-run + security panel +
    gate states from the log             containerized full suite judge the new sha

STILL fails closed (exit 1, reason=recomputed_stop_before_merge): every stop
condition recomputable from the FIXED tree that has NO local replacement:
  - deleted test files and destructive migrations (diff-derived, artifact-free)
  - a reviewer-DECLARED high risk carried in the inherited verdict artifacts
  - a senior deadlock recorded in the inherited iteration log
  - the full report-only recompute (path guard, nondraft specs, verify round)
    when the spec on the tree is report-only

Deliberately NOT a stop here: sensitive/security paths and the high COMPUTED
risk they imply. Their local manifestation is blast classification -> L3 ->
RISK_DECISION: the change is digested and only a typed exact-task-id
confirmation can merge it (never auto-merge). Stopping here instead would
make every L3 --local fix an unclearable BROKEN hold - H5 all over again
(when laddy dogfoods itself, almost every code change is L3).

The non-local (VPS-authored) route is untouched: it still runs
merge_check.check() with every equality check intact.
"""

from __future__ import annotations

from pathlib import Path

from orchestrator import TARGET_DIR_NAME
from orchestrator.artifacts import TaskArtifacts
from orchestrator.gitops import GitOps
from orchestrator.loop import (
    declared_risk_from_verdicts,
    report_verify_confirmed,
)
from orchestrator.policy import (
    RISK_ORDER,
    deleted_test_files,
    destructive_migrations,
    nondraft_report_specs,
    report_only_decision,
)
from orchestrator.spec import SpecError, parse_spec
from orchestrator.target_policy import load_target_policy


def check_local_fix(repo: Path, base: str, task_id: str) -> tuple[int, str]:
    """Returns (exit_code, message) like merge_check.check(): 0 = no
    recomputable stop on the fix tree, 1 = a stop still stands (fail closed).
    ``repo`` is the detached worktree at the Director's fix sha."""
    task = task_id
    artifacts = TaskArtifacts(repo, task_id)

    # Same GitOps helpers (and therefore the same --no-renames + task-artifact
    # exclusion) as merge_check.check, so the two recomputes cannot drift.
    gitops = GitOps(
        repo_url="unused", work_root=repo.parent, default_branch=base.split("/")[-1]
    )
    changed = gitops.changed_files(repo, task)

    def _read(path: str) -> str:
        return (repo / path).read_text(encoding="utf-8")

    try:
        report_only = parse_spec(
            repo / TARGET_DIR_NAME / "specs" / f"{task}.md"
        ).report_only
    except (SpecError, OSError):
        # A fix authored OFF local main carries no spec for the task at all:
        # nothing spec-scoped is recomputable, and the whole judged diff is
        # Director-authored. (The non-local path still hard-fails on an
        # unreadable spec - merge_check.check is untouched.)
        report_only = False

    if report_only:
        recomputed = report_only_decision(
            task_id=task,
            changed_files=changed,
            verify_confirmed=report_verify_confirmed(artifacts),
            nondraft_specs=nondraft_report_specs(task, changed, _read),
        )
        if recomputed.decision == "stop_before_merge":
            return 1, (
                "reason=recomputed_stop_before_merge "
                f"recomputed_reasons={list(recomputed.reasons)}"
            )
        return 0, f"decision={recomputed.decision}"

    reasons: list[str] = []
    # Per-target policy from the TRUSTED base ref, exactly as merge_check.check
    # (M1): a fix tree cannot weaken its own classification either.
    policy = load_target_policy(repo, ref=base)
    if deleted := deleted_test_files(policy, gitops.changed_statuses(repo, task)):
        reasons.append(f"test_files_deleted: {', '.join(deleted[:5])}")
    if destructive := destructive_migrations(policy, changed, _read):
        reasons.append(f"destructive_migrations: {', '.join(destructive[:5])}")
    declared = declared_risk_from_verdicts(artifacts)
    # declared is folded onto the RISK_ORDER enum at its single home
    # (declared_risk_from_verdicts -> policy.normalize_risk, unknown -> high);
    # the .get default stays as a fail-safe backstop (M8).
    if RISK_ORDER.get(declared, 2) >= RISK_ORDER["high"]:
        reasons.append(f"high_risk: declared={declared}")
    if any(
        e.get("action") == "senior" and e.get("outcome") == "deadlock"
        for e in artifacts.read_log()
    ):
        reasons.append("senior_escalation_without_clean_verdict")

    if reasons:
        return 1, f"reason=recomputed_stop_before_merge recomputed_reasons={reasons}"
    return 0, "decision=local_fix_no_recomputable_stop"
