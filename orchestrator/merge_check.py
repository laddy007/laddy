"""Merge-decision recompute helper for local_merge (design doc S4/S8, decision D9).

The VPS-committed merge-decision.json is NOT trusted: this module
RECOMPUTES the policy decision from the actual diff and the artifact log,
and fails on any mismatch. local_merge calls `check()` with an explicit
task_id (spec sec. 3) as one of the trial-merge gates before a local merge is
allowed to proceed.

Returns (exit_code, message): 0 = a MERGEABLE decision echoed in the message
("decision=<...>"), 1 = check failed (mismatch / missing artifacts / stale
SHAs) OR the recomputed decision is stop_before_merge. An honestly-committed
stop is consistent, but consistency is not a pass: the decision VALUE is part
of the verdict, so a stop must never exit 0 and launder itself into the local
authority's policy gate (H1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True)
class CommittedDecision:
    """Typed view of the UNTRUSTED branch-committed merge-decision.json.

    Only the field the check compares (``decision``) is modeled; everything
    else in the artifact is recomputed from trusted inputs, never read. A
    non-string decision folds to None, which can never equal a recomputed
    decision - fail closed, exactly like an absent key.
    """

    decision: str | None

    @classmethod
    def from_payload(cls, payload: object) -> CommittedDecision | None:
        """None for a payload that is not a JSON object (missing artifact)."""
        if not isinstance(payload, dict):
            return None
        decision = payload.get("decision")
        return cls(decision=decision if isinstance(decision, str) else None)


@dataclass(frozen=True)
class CommittedState:
    """Typed view of the UNTRUSTED branch-committed state.json.

    Only ``head_sha`` is compared (against the recomputed code sha); a
    non-string value folds to None and can never match - fail closed.
    """

    head_sha: str | None

    @classmethod
    def from_payload(cls, payload: object) -> CommittedState | None:
        """None for a payload that is not a JSON object (missing artifact)."""
        if not isinstance(payload, dict):
            return None
        head_sha = payload.get("head_sha")
        return cls(head_sha=head_sha if isinstance(head_sha, str) else None)


def check(repo: Path, base: str, task_id: str) -> tuple[int, str]:
    """Returns (exit_code, message). Pure enough to unit-test on a temp repo."""
    task = task_id
    artifacts = TaskArtifacts(repo, task_id)

    # A truncated/garbled committed artifact is a FAILED check, not a crash
    # (M7): return (1, reason) with the parse failure recorded, so the caller
    # holds THIS task and the rest of its batch keeps processing. The branch
    # authored these files, so unparseable JSON is its defect - fail closed.
    try:
        committed = CommittedDecision.from_payload(artifacts.read_json(MERGE_DECISION))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return 1, f"reason=unparseable_merge_decision: {exc!r}"
    if committed is None:
        return 1, "reason=missing_merge_decision"
    try:
        state = CommittedState.from_payload(artifacts.read_json(STATE))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return 1, f"reason=unparseable_state: {exc!r}"

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
    changed = gitops.changed_files(repo, task)

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
        if state is None:
            return 1, "reason=missing_state"
        # recompute code_sha from the actual history and compare to state.json
        # (a fabricated state.json must not survive)
        actual_code_sha = gitops.code_sha(repo, task)
        if state.head_sha != actual_code_sha:
            return 1, (
                f"reason=state_sha_mismatch state={state.head_sha} "
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
            diff_lines=gitops.diff_line_count(repo, task),
            declared_risk=declared_risk_from_verdicts(artifacts),
            gates=gates,
            changed_statuses=gitops.changed_statuses(repo, task),
            migration_texts=_read,
            senior_deadlock=deadlock,
        )

    if recomputed.decision != committed.decision:
        return 1, (
            f"reason=policy_mismatch committed={committed.decision} "
            f"recomputed={recomputed.decision} recomputed_reasons={list(recomputed.reasons)}"
        )
    if recomputed.decision == "stop_before_merge":
        # Consistent stop == stop: the chain is honest, but the verdict says
        # STOP. The caller reads only the exit code, so exit 0 here would turn
        # an honestly-computed stop into a green policy gate whenever its
        # reasons (test_files_deleted, high_risk, senior deadlock, ...) have no
        # independent local manifestation (H1). Honor the decision value.
        return 1, (
            "reason=recomputed_stop_before_merge "
            f"recomputed_reasons={list(recomputed.reasons)}"
        )
    return 0, f"decision={recomputed.decision}"
