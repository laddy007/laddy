"""Merge-decision recompute helper for local_merge (design doc S4/S8, decision D9).

The VPS-committed merge-decision.json is NOT trusted: this module
RECOMPUTES the policy decision from the actual diff and the artifact log,
and fails on any mismatch. local_merge calls `check()` with an explicit
task_id (spec §3) as one of the trial-merge gates before a local merge is
allowed to proceed.

Returns (exit_code, message): 0 = decision echoed in the message
("decision=<...>"), 1 = check failed (mismatch / missing artifacts / stale
SHAs).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator import TARGET_DIR_NAME
from orchestrator.artifacts import MERGE_DECISION, STATE, TaskArtifacts
from orchestrator.gitops import GitOps
from orchestrator.loop import (
    declared_risk_from_verdicts,
    gate_states_from_log,
    report_verify_confirmed,
)
from orchestrator.policy import (
    merge_decision,
    nondraft_report_specs,
    report_only_decision,
)
from orchestrator.spec import SpecError, parse_spec
from orchestrator.target_policy import load_target_policy


def check(repo: Path, base: str, task_id: str) -> tuple[int, str]:
    """Returns (exit_code, message). Pure enough to unit-test on a temp repo."""
    task = task_id
    artifacts = TaskArtifacts(repo, task_id)

    committed: Any = artifacts.read_json(MERGE_DECISION)
    if not isinstance(committed, dict):
        return 1, "reason=missing_merge_decision"
    state: Any = artifacts.read_json(STATE)

    try:
        spec = parse_spec(repo / TARGET_DIR_NAME / "specs" / f"{task}.md")
    except (SpecError, OSError) as exc:
        return 1, f"reason=unreadable_spec: {exc}"

    # Reuse the SAME GitOps helpers the VPS decision used (one implementation
    # of changed_files / changed_statuses / diff sizing, both with
    # --no-renames + <agent-dir>/tasks excluded) so the two computations cannot
    # drift and a renamed-away sensitive test cannot slip through.
    gitops = GitOps(
        repo_url="unused", work_root=repo.parent, default_branch=base.split("/")[-1]
    )
    changed = gitops.changed_files(repo)

    if spec.report_only:
        recomputed = report_only_decision(
            task_id=task,
            changed_files=changed,
            verify_confirmed=report_verify_confirmed(artifacts),
            nondraft_specs=nondraft_report_specs(
                task, changed, lambda p: (repo / p).read_text(encoding="utf-8")
            ),
        )
    else:
        if not isinstance(state, dict):
            return 1, "reason=missing_state"
        # recompute code_sha from the actual history and compare to state.json
        # (a fabricated state.json must not survive)
        actual_code_sha = gitops.code_sha(repo)
        if state.get("head_sha") != actual_code_sha:
            return 1, (
                f"reason=state_sha_mismatch state={state.get('head_sha')} "
                f"actual={actual_code_sha}"
            )
        # rebuild gate states + declared risk from the artifacts, NOT from the
        # untrusted merge-decision.json (a fabricated risk_level must not
        # launder away a reviewer-declared high risk)
        gates = gate_states_from_log(artifacts.read_log(), actual_code_sha)

        def _read(path: str) -> str:
            return (repo / path).read_text(encoding="utf-8")

        deadlock = any(
            e.get("action") == "senior" and e.get("outcome") == "deadlock"
            for e in artifacts.read_log()
        )
        # Per-target policy from the TRUSTED base ref (not the branch): the
        # off-VPS recompute must not honor a branch that weakened its own
        # policy.toml (M1). classify inputs (changed/diff) stay branch-derived.
        recomputed = merge_decision(
            policy=load_target_policy(repo, ref=base),
            changed_files=changed,
            diff_lines=gitops.diff_line_count(repo),
            declared_risk=declared_risk_from_verdicts(artifacts),
            gates=gates,
            changed_statuses=gitops.changed_statuses(repo),
            migration_texts=_read,
            senior_deadlock=deadlock,
        )

    if recomputed.decision != committed.get("decision"):
        return 1, (
            f"reason=policy_mismatch committed={committed.get('decision')} "
            f"recomputed={recomputed.decision} recomputed_reasons={list(recomputed.reasons)}"
        )
    return 0, f"decision={recomputed.decision}"
