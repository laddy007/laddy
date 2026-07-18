"""Local merge authority (trust-model doc S5/S6) - the real binding gate.

Runs on the Director's TRUSTED machine, not the VPS. For each pushed
bare ``<task>`` branch it re-derives everything from scratch on trusted
infra - it never trusts the VPS gate log - and either MERGES into local
main or HOLDS for a human risk decision. It NEVER edits code or fixes a
failing test: a needed fix is a new VPS task, not this tool's job.

``--local <ref>`` is the trusted-machine escape hatch for a task the local
gate held BROKEN: the Director authors the fix by hand with ordinary git and
this tool judges that locally-committed revision through the same applicable gate
(no fetch of the remote branch, no VPS round trip). It stays fix-free - the
only new capability is sourcing the judged/merged commit from a local ref
instead of a fetched one. It does not trust the code more, it trusts the
route: the Director is the trusted author, the same applicable trusted-local
gate judges the diff, and the judged sha is the merged sha (dirty-tree guarded),
so nothing unverified slips in. The inherited VPS artifact attestation is N/A
for that newer commit. A stopgap until bounce-to-VPS exists.

Decision by blast radius (policy.classify_blast_radius, trust-model S8):
  L1 safe-by-construction : merge after the mechanical gates, no review
  L2 ordinary logic       : the agents ARE the gate (rw2 + security panel)
  L3 sensitive surface    : never auto-merge; digest -> human risk decision

EVERY merge side-effect into local main - L1, L2, and L3 alike - additionally
requires the merge-safety confirmation: the operator types the EXACT task id
(H4). A wrong or blank id declines and merges nothing; --no-input is a true
dry run that never prompts and never merges.

Deterministic gates (block on red): local full test re-run, diff-coverage,
semgrep, gitleaks, and (for a fetched VPS tip) artifact attestation via
merge_check - or, under --local, the fail-closed policy recompute on the fix
tree via merge_check_local (H5). Judgment gates (escalate, never silently
block): rw2 re-run, the security panel.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

from orchestrator import TARGET_DIR_NAME, default_work_root
from orchestrator.agents import AgentRunner

# Hub-main tripwire (spec S5, audit M3) - deletion / rewind / divergence
# detection lives in orchestrator.hub_tripwire (a leaf module, like
# merge_subject); the boolean wrapper stays re-exported for existing call
# sites and tests.
from orchestrator.hub_tripwire import (
    HubMainCheck,
    HubMainState,
    check_hub_main,
    hub_main_ancestor_of_local,
)
from orchestrator.human_text import untrusted_inline

# Re-exported for backward-compat call sites (tests/fakes.py, historical
# imports) - the implementation lives in orchestrator.merge_subject, a leaf
# module with no orchestrator imports of its own, so oracle/scope.py can
# depend on the wire format without cycling back through local_merge.py.
from orchestrator.merge_subject import (
    _MERGE_SUBJECT,
    merge_subject,
    parse_merge_subject,
)
from orchestrator.policy import L2, L3, classify_blast_radius
from orchestrator.target_policy import load_target_policy
from orchestrator.testgate import BindingGate, BindingResult, restored_infra_paths
from orchestrator.verdict import Verdict, VerdictError, request_verdict

__all__ = [
    "ArtifactAttestation",
    "ArtifactAttestationState",
    "GateResults",
    "HubMainCheck",
    "HubMainState",
    "LocalMergeEngine",
    "MergePreparationError",
    "MergeRequest",
    "MergeVerdict",
    "_MERGE_SUBJECT",
    "check_hub_main",
    "decide",
    "hub_main_ancestor_of_local",
    "merge_subject",
    "parse_merge_subject",
    "render_advisory",
    "run_security_panel",
]

# (repo, base, task_id) -> (exit_code, message) - wraps merge_check.check.
# ``base`` is the trusted LOCAL base branch name (config.default_branch): the
# policy recompute loads policy.toml from that local ref, not from a possibly
# -stale origin/<base> remote-tracking ref (M1).
MergeCheckFn = Callable[[Path, str, str], tuple[int, str]]

# --- results & verdict -------------------------------------------------------


class ArtifactAttestationState(str, Enum):
    """Whether the VPS artifact chain describes the commit under judgment."""

    PASSED = "passed"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class ArtifactAttestation:
    """Result of checking the VPS-authored state/decision artifact chain.

    The check is applicable to a fetched VPS task tip: its committed artifacts
    must describe that exact code history AND the recomputed policy decision
    must be mergeable - merge_check exits non-zero for a consistent
    stop_before_merge too, so an honestly-committed stop holds here instead of
    laundering into a green policy gate (H1). It is deliberately not applicable
    to ``--local`` because a Director-authored code commit makes those inherited
    artifacts stale by construction; the fresh trusted-local gates judge that
    commit instead. The --local route still runs the fail-closed policy
    recompute on the fix tree (merge_check_local, H5): a stop it recomputes is
    carried here as FAILED, so an honestly-stopped branch cannot be laundered
    by re-judging a trivial fix with --local. Keeping this as a typed state
    avoids laundering N/A into a fake successful policy check.
    """

    state: ArtifactAttestationState
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.state is ArtifactAttestationState.FAILED


@dataclass(frozen=True)
class GateResults:
    """Everything the local gate gathered for one branch."""

    blast: str  # L1 | L2 | L3
    artifact_attestation: ArtifactAttestation
    tests_passed: bool
    tests_tail: str
    coverage_ok: bool
    coverage_detail: str
    scan_findings: tuple[str, ...]  # semgrep/gitleaks hits; empty = clean
    rw2: Verdict | None
    security_verdicts: tuple[Verdict, ...]
    sensitive_files: tuple[str, ...] = ()  # the paths that made it L3
    head_sha: str = ""  # the exact commit these gates verified (TOCTOU pin)
    # Gate-infra paths this branch changed whose branch version the gate did NOT
    # run: it restored trusted main's copy over them (NÁLEZ 1). Empty for every
    # branch that leaves the gate infra alone - i.e. almost all of them.
    infra_overridden: tuple[str, ...] = ()


# Hold kinds (trust-model S8) - the CLI treats them differently:
#   risk_decision : gates all GREEN, only sensitive (L3) -> offer merge Y/N
#   broken        : a gate actually FAILED -> diagnostic, no merge offer
AUTO_MERGE = "auto_merge"
RISK_DECISION = "risk_decision"
BROKEN = "broken"
#   dry_run       : --no-input dry run -> would auto-merge, but nothing is
#                   touched (no local-main mutation, no push)
DRY_RUN = "dry_run"
#   declined      : gates green and mergeable, but the operator did not type
#                   the exact task id (H4) -> nothing merged, branch stays ready
DECLINED = "declined"


@dataclass(frozen=True)
class MergeVerdict:
    task_id: str
    decision: str  # "merge" | "hold"
    kind: str = AUTO_MERGE  # AUTO_MERGE | RISK_DECISION | BROKEN
    reasons: tuple[str, ...] = ()
    digest: str = ""
    # Judgment-gate findings (security panel / rw2) WAIVED by --advisory: empty
    # unless the branch merged under advisory. A non-empty advisory on a merged
    # verdict is the trigger to write the durable merge-advisory.md record.
    advisory: tuple[str, ...] = ()

    @property
    def merged(self) -> bool:
        return self.decision == "merge"


def _security_blockers(gates: GateResults) -> list[str]:
    out: list[str] = []
    for v in gates.security_verdicts:
        out.extend(f.summary for f in v.blockers)
    return out


_L3_REASON = "sensitive surface (L3) - human risk decision required"


def build_digest(
    task_id: str,
    gates: GateResults,
    kind: str,
    reasons: Sequence[str],
    advisory: Sequence[str] = (),
) -> str:
    """One-screen summary. For a RISK_DECISION it names what is sensitive and
    asks for the merge-safety confirmation (type the exact task id); for a
    BROKEN hold it diagnoses what failed, why, and what is needed - and does
    NOT offer a merge (you fix broken code, not merge it).

    ``advisory`` (non-empty only on an --advisory RISK_DECISION) lists the
    judgment-gate findings being WAIVED, so the confirmation prompt is honest:
    it never claims "all gates passed" for a change the panel objected to."""
    safe_task = untrusted_inline(task_id)
    lines = [f"# Merge hold: {safe_task}  (blast {gates.blast}, {kind})", ""]

    if kind == RISK_DECISION:
        lines += ["## Sensitive surface touched", ""]
        lines += [f"- `{untrusted_inline(p)}`" for p in gates.sensitive_files] or [
            "- (sensitive path)"
        ]
        if advisory:
            lines += ["", "## Waived judgment-gate findings (--advisory)", ""]
            lines += [f"- {untrusted_inline(r)}" for r in advisory]
            lines += [
                "",
                "The deterministic gates passed, but the security panel / rw2",
                "flagged the above and they are being WAIVED by --advisory: this",
                "is a risk call on a change that is NOT fully verified. The waived",
                "findings are recorded durably in merge-advisory.md.",
                "",
                "## Your decision",
                "",
                f"Merge `{safe_task}` into main under --advisory? Type the",
                "exact task id to merge; anything else declines - you decide",
                "on this summary, not by reading the diff.",
                "",
            ]
        else:
            lines += [
                "",
                "All correctness/security gates passed; this only needs your risk",
                "call because it touches a policy-sensitive surface.",
                "",
                "## Your decision",
                "",
                f"Merge `{safe_task}` into main? Type the exact task id to",
                "merge; anything else declines - you decide on this summary,",
                "not by reading the diff.",
                "",
            ]
        return "\n".join(lines)

    # BROKEN: diagnostic, no merge offer
    lines += ["## What failed", ""]
    lines += [f"- {untrusted_inline(r)}" for r in reasons]
    sec = _security_blockers(gates)
    if sec:
        lines += ["", "## Security panel findings", ""] + [
            f"- {untrusted_inline(s)}" for s in sec
        ]
    if gates.rw2 is not None and gates.rw2.blockers:
        lines += ["", "## rw2 findings", ""] + [
            f"- {untrusted_inline(f.summary)}" for f in gates.rw2.blockers
        ]
    if not gates.tests_passed:
        lines += [
            "",
            "## Local test failure (tail)",
            "",
            "```",
            untrusted_inline(gates.tests_tail[-1500:], limit=1500),
            "```",
        ]
    lines += ["", "## What is needed", ""]
    if gates.infra_overridden:
        # A branch must not supply the container it is judged in, so the gate
        # restores that infra from trusted main - which is exactly why
        # re-running does not clear it: the next run restores the same paths.
        # Naming the limit beats sending the Director around the loop again.
        lines += [
            "This branch changes the gate's own infrastructure, which the gate",
            "restores from trusted main before it runs, so re-running does not clear it:",
            "the next run restores the same paths. No gate here can judge the branch's",
            "own copy - landing those paths is your call, on a route you trust.",
            "",
            "Any red gate above may be the restore's doing rather than a defect:",
            "the suite ran against main's infra, not this branch's.",
        ]
    else:
        lines += [
            "This change is not mergeable as-is. Re-run the task on the VPS to fix",
            "the failing gate(s), or address them and push a new revision of the",
            "branch.",
            "",
            "Or fix it right here on the trusted machine and re-judge locally:",
            "commit the fix ON TOP of this branch with ordinary git, then run",
            "`merge-verified.sh <task> --local <ref>` (a sha, branch, or worktree",
            "path). --local does not trust the code more - it trusts the route:",
            "you are the trusted author and the same applicable gate still judges",
            "the diff (the historical VPS artifact attestation is N/A),",
            "and the judged sha is the merged sha, so nothing unverified",
            "slips in. It is a stopgap until bounce-to-VPS exists (and a",
            "legitimate escape hatch after).",
        ]
    lines += ["", f"`{safe_task}` is NOT merged and NOT deleted.", ""]
    return "\n".join(lines)


def decide(
    task_id: str, gates: GateResults, *, advisory_mode: bool = False
) -> MergeVerdict:
    """Pure decision (trust-model S8). merge | hold(risk_decision|broken).

    ``advisory_mode`` (--advisory) waives ONLY the judgment gates (the security
    panel and the rw2 re-run): when a judgment finding is the sole thing keeping
    a branch from merging, the branch merges and the waived findings are carried
    on the verdict's ``advisory`` tuple for a durable record. The deterministic
    gates (policy recompute, local test suite, diff-coverage, secret/FS scan,
    and the infra-override guard) are NEVER waivable - any red one still forces
    a BROKEN hold, even under advisory. ``decide`` stays pure: it writes nothing.
    """
    deterministic: list[str] = []
    judgment: list[str] = []

    # deterministic hard gates - any red is BROKEN and can NEVER be waived
    if gates.artifact_attestation.failed:
        deterministic.append(
            "VPS artifact attestation failed/mismatch: "
            + untrusted_inline(gates.artifact_attestation.detail)
        )
    if not gates.tests_passed:
        deterministic.append("local full test suite is red")
    if not gates.coverage_ok:
        deterministic.append(
            "diff-coverage below threshold: " + untrusted_inline(gates.coverage_detail)
        )
    if gates.scan_findings:
        deterministic.append(
            f"security scan flagged {len(gates.scan_findings)} item(s): "
            + "; ".join(untrusted_inline(item) for item in gates.scan_findings[:5])
        )
    # Not a defect in the branch - a limit of what this gate can say about it.
    # It still blocks: the alternative is merging a gate-infra change no gate
    # ever ran, or blaming the branch for a red suite the restore caused.
    if gates.infra_overridden:
        deterministic.append(
            "gate infra changed by this branch was NOT verified - the gate ran "
            "trusted main's copy of: "
            + ", ".join(untrusted_inline(path) for path in gates.infra_overridden)
        )
    # judgment gates - waivable under --advisory (recorded, never silently lost)
    if sec := _security_blockers(gates):
        judgment.append(
            "security panel blocker(s): "
            + "; ".join(untrusted_inline(summary) for summary in sec[:5])
        )
    if gates.rw2 is not None and gates.rw2.blockers:
        judgment.append(
            "rw2 blocker(s): "
            + "; ".join(
                untrusted_inline(finding.summary)
                for finding in gates.rw2.blockers[:5]
            )
        )

    # The blocking set is the deterministic reasons ALWAYS, plus the judgment
    # reasons only when advisory is off. --advisory may remove judgment from
    # blocking; it may never touch deterministic (the trust invariant).
    blocking = deterministic + ([] if advisory_mode else judgment)
    if blocking:
        # No _L3_REASON here: this branch returns before the L3 confirmation
        # can ever be offered, so naming a "human risk decision" would advertise a
        # decision the Director is not being given. The blast level still
        # reaches them through the digest header.
        reasons = tuple(blocking)
        return MergeVerdict(
            task_id, "hold", BROKEN, reasons,
            build_digest(task_id, gates, BROKEN, reasons),
        )
    # From here the branch merges: any judgment findings are waived (only
    # non-empty in advisory mode) and ride along as the advisory record.
    advisory = tuple(judgment) if advisory_mode else ()
    if gates.blast == L3:
        reasons = (_L3_REASON,)
        return MergeVerdict(
            task_id, "hold", RISK_DECISION, reasons,
            build_digest(task_id, gates, RISK_DECISION, reasons, advisory),
            advisory=advisory,
        )
    return MergeVerdict(task_id, "merge", AUTO_MERGE, advisory=advisory)


def render_advisory(task_id: str, advisory: Sequence[str]) -> str:
    """The durable record of the judgment-gate findings an --advisory merge
    WAIVED. Written to .laddy/tasks/<task>/merge-advisory.md and committed into
    local main so it survives deletion of the task branch (for later cleanup).

    Honest labeling (constraint 5): this is NOT a clean bill of health. The
    deterministic gates passed, but the security panel / rw2 flagged the
    findings below and they were waived by an explicit --advisory merge - the
    file says so plainly so it is never mistaken for a fully-verified merge."""
    lines = [
        f"# Advisory merge: {untrusted_inline(task_id)}",
        "",
        "This branch was merged under `--advisory`. The deterministic gates",
        "(VPS artifact attestation when applicable, local test suite,",
        "diff-coverage, secret/FS scan, and the infra-override guard) all passed,",
        "but the JUDGMENT gates below were",
        "WAIVED, not cleared. This is NOT a fully-verified merge: the findings",
        "were recorded and the branch merged anyway, for later cleanup.",
        "",
        "## Waived judgment-gate findings (security panel / rw2)",
        "",
    ]
    lines += [f"- {untrusted_inline(r)}" for r in advisory] or ["- (none recorded)"]
    lines += [""]
    return "\n".join(lines)


# --- security panel ----------------------------------------------------------


def run_security_panel(
    runners: Sequence[AgentRunner], prompt: str, cwd: Path
) -> list[Verdict]:
    """Run every panel member; a malformed member is treated as a blocking
    abstention (its absence must not silently pass a security review)."""
    verdicts: list[Verdict] = []
    for runner in runners:
        try:
            verdict, _ = request_verdict(runner, prompt, cwd, validate=None)
        except VerdictError as exc:
            # a panel member that cannot produce a valid verdict is a flag,
            # not a pass: synthesize a blocker so decide() holds for a human
            verdict = _abstention_blocker(runner.name, str(exc))
        verdicts.append(verdict)
    return verdicts


# An abstention reason quotes agent-controlled text (a schema error echoes the
# value it rejected) into a report a human reads. Bounded so a runaway blob
# cannot bury the rest of the digest; the full text is the runner's to log.
_ABSTENTION_REASON_MAX = 300


def _abstention_blocker(member: str, reason: str = "") -> Verdict:
    from orchestrator.verdict import Finding

    # WHY it abstained is the whole diagnostic value: quota, a rejected model
    # flag and a schema violation all abstain identically, and only this
    # message tells them apart. request_verdict already knows - carry it.
    detail = reason.strip()
    if len(detail) > _ABSTENTION_REASON_MAX:
        detail = f"{detail[:_ABSTENTION_REASON_MAX]}..."
    return Verdict(
        verdict="CHANGES_REQUESTED",
        risk_level="high",
        files_reviewed=(),
        claims_verified=(),
        findings=(
            Finding(
                severity="blocker",
                category="security",
                file="",
                line=0,
                summary=f"security panel member {member!r} did not return a "
                "valid verdict; holding for human review"
                + (f" - {detail}" if detail else ""),
                failure_scenario="unreviewed change on a security-relevant path",
            ),
        ),
        test_assessment="",
        residual_risks=(),
    )


# --- engine ------------------------------------------------------------------

# Injected collaborators (real impls in the CLI; fakes in tests):
#   list_ready() -> task ids with a complete pushed branch ready to merge
#   verify_one(task_id) -> GateResults   (gathers all gates on trusted infra)
#   merge_one(request)  -> bool          (atomically integrate code and any
#                                         advisory record into local main)
ListReady = Callable[[], Sequence[str]]
VerifyOne = Callable[[str], GateResults]


@dataclass(frozen=True)
class MergeRequest:
    """Everything the mutating merge boundary needs for one atomic commit."""

    task_id: str
    verified_sha: str
    advisory: tuple[str, ...] = ()


class MergePreparationError(RuntimeError):
    """The uncommitted merge was aborted before trusted main moved."""


# The sha is the exact commit the gates saw; merging by sha (not by branch ref)
# closes the verify->merge TOCTOU. Advisory findings travel in the same request
# so the executor can stage their durable record before it creates the commit.
MergeOne = Callable[[MergeRequest], bool]


@dataclass
class LocalMergeEngine:
    list_ready: ListReady
    verify_one: VerifyOne
    merge_one: MergeOne
    on_verdict: Callable[[MergeVerdict], None] = field(default=lambda v: None)
    # consulted before ANY merge side-effect on local main - an L1/L2
    # AUTO_MERGE decision as well as an L3 RISK_DECISION hold (H4): return
    # True to approve. The interactive implementation accepts only the EXACT
    # task id. Default declines (fail closed: nothing merges unconfirmed).
    confirm: Callable[[MergeVerdict], bool] = field(default=lambda v: False)
    # dry run (--no-input): report what WOULD auto-merge but never mutate local
    # main or push. Without this, --no-input still auto-merged every L1/L2 green
    # change into local main - contradicting its "dry run" contract (and the
    # "merge into local main only on request" rule).
    dry_run: bool = field(default=False)
    # --advisory: waive the judgment gates (security panel + rw2), recording
    # their findings on the verdict instead of holding BROKEN. Deterministic
    # gates still fail closed (decide() enforces this - the engine only forwards
    # the flag). Default off: judgment-gate decision semantics stay unchanged.
    advisory_mode: bool = field(default=False)

    def run(self) -> list[MergeVerdict]:
        """Process every ready branch sequentially.

        Sequential-with-re-verify is real: verify_one() runs the deterministic
        gate on the branch trial-merged into the CURRENT local main (#11), so a
        branch is re-verified against whatever prior merges this batch already
        landed - a pair that passes in isolation but conflicts once combined is
        caught here, not left in a red main. A hold never blocks the others.
        Never fixes anything - there is no fix path. A BROKEN hold is never
        offered for merge. EVERY merge side-effect on local main is put to the
        confirm() callback PER TASK with that task's verdict - an L1/L2
        AUTO_MERGE decision as well as an L3 RISK_DECISION (H4); interactively
        that means typing the exact task id. A declined task merges nothing
        and the batch continues; a dry run never confirms and never merges.
        """
        results: list[MergeVerdict] = []
        for task_id in self.list_ready():
            gates = self.verify_one(task_id)
            verdict = decide(task_id, gates, advisory_mode=self.advisory_mode)
            if verdict.decision == "hold" and verdict.kind == RISK_DECISION:
                if self.confirm(verdict):
                    # replace() (not a fresh 3-arg construct) so verdict.advisory
                    # survives the L3 confirm - else an advisory L3 merge would
                    # silently drop its record (AC5). kind stays RISK_DECISION.
                    verdict = replace(verdict, decision="merge")
            elif verdict.merged and not self.dry_run and not self.confirm(verdict):
                # AUTO_MERGE (L1/L2): the merge side-effect needs the SAME
                # merge-safety confirmation as L3 (H4) - auto-merge means "no
                # review required", never "no human at the merge". A dry run is
                # excluded here only because it never merges at all (the swap
                # below), so there is nothing to confirm and nothing prompts.
                reasons = (
                    "merge not confirmed (the exact task id was not typed); "
                    "nothing merged",
                )
                verdict = MergeVerdict(
                    task_id, "hold", DECLINED, reasons,
                    f"# Merge hold: {untrusted_inline(task_id)} (not confirmed)\n\n"
                    "The merge-safety confirmation declined this merge: the exact\n"
                    "task id was not typed. Nothing was merged and no state was\n"
                    "changed; the branch stays ready. Re-run merge-verified.sh to\n"
                    "be asked again.\n",
                )
            if verdict.merged and self.dry_run:
                # dry run: record what WOULD auto-merge, but touch nothing. The
                # advisory tuple is CARRIED (not dropped): the whole point of the
                # preview is to inspect before committing to --advisory, so a
                # branch that would waive judgment findings must read differently
                # from a fully-clean one (constraint 5 - honest labeling).
                if verdict.advisory:
                    reasons = (
                        "would merge under --advisory (dry run: --no-input, "
                        "nothing changed); judgment gates WOULD be waived:",
                        *verdict.advisory,
                    )
                    digest = (
                        f"# Dry run (--advisory): {untrusted_inline(task_id)}\n\n"
                        "Would merge into local main under --advisory, WAIVING the "
                        "judgment-gate findings below and recording them in "
                        "merge-advisory.md. This is NOT a fully-verified merge.\n\n"
                        "## Judgment-gate findings that WOULD be waived\n\n"
                        + "\n".join(f"- {r}" for r in verdict.advisory)
                        + "\n\nRe-run without --no-input to apply.\n"
                    )
                else:
                    reasons = ("would auto-merge (dry run: --no-input, nothing changed)",)
                    digest = (
                        f"# Dry run: {untrusted_inline(task_id)}\n\n"
                        "Would auto-merge into local main; "
                        "re-run without --no-input to apply.\n"
                    )
                verdict = MergeVerdict(
                    task_id, "hold", DRY_RUN, reasons, digest,
                    advisory=verdict.advisory,
                )
            if verdict.merged:
                request = MergeRequest(task_id, gates.head_sha, verdict.advisory)
                try:
                    merged = self.merge_one(request)
                except MergePreparationError as exc:
                    reasons = (
                        "merge preparation failed before commit; nothing landed: "
                        + untrusted_inline(str(exc)),
                    )
                    verdict = MergeVerdict(
                        task_id,
                        "hold",
                        BROKEN,
                        reasons,
                        build_digest(task_id, gates, BROKEN, reasons),
                    )
                else:
                    if not merged:
                        verdict = MergeVerdict(
                            task_id,
                            "hold",
                            BROKEN,
                            ("branch no longer applies cleanly to main after a "
                             "prior merge; re-run the task",),
                            f"# Merge hold: {untrusted_inline(task_id)}\n\n"
                            "Branch no longer applies cleanly to main. Re-run "
                            "the task on the VPS.\n",
                        )
            self.on_verdict(verdict)
            results.append(verdict)
        return results


# --- real gatherers (trusted local infra; the CLI wires these) ---------------

SECURITY_PANEL_PROMPT = """\
{role}

Task ID: {task}

Adversarially review the current branch (diff against origin/{base}) for a
SECURITY defect only. Output ONLY the verdict JSON object.
"""

RW2_LOCAL_PROMPT = """\
{role}

Task ID: {task}

Re-review the current branch (diff against origin/{base}) as an independent
cross-vendor guard on TRUSTED infrastructure. Output ONLY the verdict JSON.
"""


def _git(repo: Path, *args: str, check: bool = True) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def discover_ready(
    repo: Path, base_branch: str = "main", branch_remote: str = "origin"
) -> list[str]:
    """Task ids whose branch is pushed AND carries a committed
    merge-decision.json (the VPS loop reached a terminal push).

    The hub is a closed namespace (spec: discovery selector): every branch
    except ``base_branch`` IS a task. The readiness filter is unchanged -
    the selector widened, the bar did not move.

    --prune drops the tracking refs of deleted TASK branches (push_and_cleanup
    deletes merged ones), but it must never touch the base branch's tracking
    ref: that ref is the tripwire's memory of the last verified hub main (M3),
    and pruning it would make a hub whose main was deleted look like a benign
    fresh hub on the next run. The negative refspec excludes the base branch
    from both fetching and pruning; check_hub_main alone advances that ref.
    """
    _git(
        repo, "fetch", "--prune", branch_remote,
        f"+refs/heads/*:refs/remotes/{branch_remote}/*",
        f"^refs/heads/{base_branch}",
    )
    _, out = _git(
        repo, "for-each-ref", "--format=%(refname:strip=3)",
        f"refs/remotes/{branch_remote}",
    )
    ready: list[str] = []
    for task in out.splitlines():
        if task in (base_branch, "HEAD") or not task:
            continue
        artifact = f"{TARGET_DIR_NAME}/tasks/{task}/merge-decision.json"
        code, _ = _git(
            repo, "cat-file", "-e", f"{branch_remote}/{task}:{artifact}", check=False
        )
        if code == 0:
            ready.append(task)
    return ready


@dataclass
class GateTools:
    """Injected real tools for gathering gates (fakeable in tests)."""

    merge_check_fn: MergeCheckFn
    # The deterministic gate (tests + coverage + semgrep + gitleaks) runs as ONE
    # containerized pass at the pinned sha, so untrusted branch code never
    # executes on the Director's machine (trust-model S6/S10).
    binding_gate: BindingGate
    rw2_runner: AgentRunner
    security_runners: Sequence[AgentRunner]
    roles_dir: Path
    # --local ONLY: the fail-closed policy recompute on the Director's fix tree
    # (merge_check_local.check_local_fix, H5). None = use the real one; injected
    # here so tests can spy/fake it like merge_check_fn. NEVER consulted on the
    # fetched-branch path (which keeps running merge_check_fn unchanged).
    local_check_fn: MergeCheckFn | None = None


# Agent-config entry points a headless claude/codex loads from its working
# directory: project hooks and MCP servers execute host commands, steering files
# inject instructions. The review panel runs these CLIs in the untrusted branch
# worktree on the Director's TRUSTED machine (C2), so any shipped on the branch
# would run/steer on the host - read-only tool scoping governs what the model
# does, not the CLI's own startup config loading. Stripped from the review
# worktree before any CLI runs; a code reviewer reviews the diff (agent-config
# changes are also L3 by policy), it never honors the branch's agent config.
#
# Matched by NAME at every depth (H7), not just the repo root: the CLIs
# auto-ingest steering/MCP config from subdirectories they descend into, and
# ENGINE_SENSITIVE_GLOBS flags **/CLAUDE.md etc. for the same reason. Names
# compare casefolded: on a case-insensitive filesystem (WSL DrvFs) the CLI
# opening "CLAUDE.md" finds a branch-shipped "Claude.md" just the same.
_UNTRUSTED_CONFIG_DIR_NAMES: frozenset[str] = frozenset({".claude", ".codex"})
_UNTRUSTED_CONFIG_FILE_NAMES: frozenset[str] = frozenset(
    {"claude.md", "agents.md", "gemini.md", ".mcp.json"}
)


def _neutralize_agent_config(wt: Path) -> None:
    """Remove branch-shipped agent config from a review worktree (C2), at any
    depth (H7): nested ``pkg/CLAUDE.md`` / ``pkg/.mcp.json`` / ``pkg/.claude/``
    steer or execute exactly like their root counterparts.

    Only the working tree is touched (never a commit), so the commit-range diffs
    that drive classification and merge_check are unaffected - a malicious
    agent-config change still shows in the diff and routes to L3. Removing the
    checkout's own root CLAUDE.md/AGENTS.md (e.g. laddy dogfooding itself as
    the target) has always been this function's contract - reviewers read the
    diff, never the branch's steering files - and the recursion just extends
    that same contract to nested paths.
    """

    def _remove(target: Path) -> None:
        if target.is_symlink():
            target.unlink(missing_ok=True)  # unlink the link, never follow it
        elif target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)

    for root, dirs, files in os.walk(wt, topdown=True):
        descend: list[str] = []
        for name in dirs:
            folded = name.casefold()
            if folded == ".git":
                continue  # git metadata: keep, never descend
            if folded in _UNTRUSTED_CONFIG_DIR_NAMES:
                _remove(Path(root) / name)
                continue  # removed: nothing left to descend into
            descend.append(name)
        dirs[:] = descend
        for name in files:
            if name.casefold() in _UNTRUSTED_CONFIG_FILE_NAMES:
                _remove(Path(root) / name)


def _worktree_at_sha(repo: Path, task_id: str, work_root: Path, sha: str) -> Path:
    """Detached worktree at ``sha`` so gates run in isolation without disturbing
    the Director's main checkout.

    Branch-shipped agent config is stripped before any reviewer CLI can load it
    (C2); classification is unaffected (it diffs commits, not the working tree).
    The remote and local modes share this tail: only how ``sha`` is obtained
    differs (a fetched remote branch vs. a local ref), never what the gate sees.
    """
    wt = work_root / f"verify-{task_id}"
    _git(repo, "worktree", "prune")
    if wt.exists():
        _git(repo, "worktree", "remove", "--force", str(wt), check=False)
    _git(repo, "worktree", "add", "--detach", str(wt), sha)
    _neutralize_agent_config(wt)
    return wt


def _branch_worktree(
    repo: Path, task_id: str, work_root: Path, branch_remote: str = "origin"
) -> Path:
    """Detached worktree at <branch_remote>/<task> (fetched first) so gates run
    in isolation without disturbing the Director's main checkout."""
    _git(repo, "fetch", branch_remote, task_id)
    sha = _rev(repo, f"{branch_remote}/{task_id}")
    return _worktree_at_sha(repo, task_id, work_root, sha)


def _resolve_local_ref(repo: Path, ref: str) -> str:
    """Resolve ``ref`` to a commit sha already in the target repo, with NO fetch.

    ``--local`` judges a locally-committed revision the Director authored by hand
    (the engine never edits code - it only sources the commit from a local ref
    instead of a fetched one). ``ref`` may be a sha / branch / tag (resolved as a
    plain rev) or a worktree PATH like ``../fix`` (the Behaviour recipe): a path
    is not a rev, so on the rev lookup failing we try it as a git worktree and
    take its HEAD, then require that commit to exist in this repo's object store
    (a worktree shares it). Raises on anything that does not resolve - an
    unresolvable ref must fail cleanly (non-zero), never merge nothing silently.
    """
    code, sha = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    if code == 0 and sha:
        return sha
    p = Path(ref)
    if p.is_dir():
        code, sha = _git(p, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False)
        if code == 0 and sha:
            present, _ = _git(repo, "cat-file", "-e", sha, check=False)
            if present == 0:
                return sha
    raise RuntimeError(
        f"--local ref {ref!r} does not resolve to a commit in {repo} - commit the "
        "fix and pass its sha, branch, or worktree path"
    )


def _local_worktree(repo: Path, task_id: str, work_root: Path, ref: str) -> Path:
    """Detached worktree at the local sha ``ref`` resolves to, WITHOUT fetching
    the remote branch - the whole point of --local is to judge the Director's
    local commit rather than re-deriving from the remote tip."""
    sha = _resolve_local_ref(repo, ref)
    return _worktree_at_sha(repo, task_id, work_root, sha)


def _worktree_sha(wt: Path) -> str:
    _, sha = _git(wt, "rev-parse", "HEAD")
    return sha


def _rev(repo: Path, ref: str) -> str:
    _, sha = _git(repo, "rev-parse", ref)
    return sha


def _binding_on_merged_tree(
    repo: Path,
    task_id: str,
    work_root: Path,
    base_sha: str,
    branch_sha: str,
    binding_gate: BindingGate,
    coverage_package: str,
) -> BindingResult:
    """Run the deterministic gate on the branch TRIAL-MERGED into current local
    main, not on the branch alone (#11).

    Two branches can each pass in isolation yet leave ``main`` red once combined
    (a semantic conflict with no textual conflict); testing the merged tree
    catches that, and it re-verifies against whatever prior merges this batch
    already landed. ``base_sha`` (current local main) is both the trusted ref
    for the gate infra AND the scanner baseline, so coverage/semgrep/gitleaks
    scope THIS change against the tree it is actually merging into - not the
    stale ``origin/main`` remote-tracking ref. A textual merge conflict is a
    fail (the real merge would conflict too), reported as a broken gate.
    """
    mwt = work_root / f"verify-merge-{task_id}"
    _git(repo, "worktree", "prune")
    if mwt.exists():
        _git(repo, "worktree", "remove", "--force", str(mwt), check=False)
    _git(repo, "worktree", "add", "--detach", str(mwt), base_sha)
    try:
        code, _ = _git(
            mwt,
            "-c", "user.name=myapp-verify",
            "-c", "user.email=verify@myapp.local",
            "merge", "--no-ff", branch_sha,
            "-m", f"verify trial-merge {task_id}",
            check=False,
        )
        if code != 0:
            _git(mwt, "merge", "--abort", check=False)
            msg = "branch does not merge cleanly into current main (conflict); re-run the task"
            return BindingResult(
                tests_passed=False,
                tests_tail=msg,
                coverage_ok=False,
                coverage_detail=msg,
                scan_findings=(msg,),
            )
        merge_sha = _worktree_sha(mwt)
        return binding_gate.run(
            mwt, merge_sha, coverage_package, trusted_ref=base_sha, compare_ref=base_sha
        )
    finally:
        _git(repo, "worktree", "remove", "--force", str(mwt), check=False)


def gather_gates(
    task_id: str,
    repo: Path,
    work_root: Path,
    tools: GateTools,
    branch_remote: str = "origin",
    base_branch: str = "main",
    local_ref: str | None = None,
) -> GateResults:
    """Run every gate for one branch on trusted local infra, in a worktree.

    ``local_ref`` selects --local mode: source the worktree from the local sha
    that ``local_ref`` resolves to (no fetch of the remote branch) instead of
    the fetched remote tip. The trusted policy/classification, trial merge,
    binding gate, security panel, rw2, and ``head_sha`` TOCTOU pin are identical.
    Only the VPS artifact attestation is N/A: state.json and the VPS log describe
    the older task tip by construction once the Director adds a local code fix.
    The policy recompute is NOT skipped with it (H5): check_local_fix re-derives
    every stop still recomputable from the FIXED tree (deleted tests, destructive
    migrations, inherited declared high risk, senior deadlock, the report-only
    guard) and a recomputed stop is a deterministic FAILED - fail closed.
    """
    wt = (
        _local_worktree(repo, task_id, work_root, local_ref)
        if local_ref is not None
        else _branch_worktree(repo, task_id, work_root, branch_remote)
    )
    try:
        verified_sha = _worktree_sha(wt)
        # The recompute (and --local's stop recompute below) is keyed to the
        # trusted LOCAL base branch (M1): policy loads from local <base_branch>
        # - the same trusted ref the binding gate uses - never from a possibly
        # -stale origin/<base> remote-tracking ref, and never from a literal
        # "main" on a target whose default branch is named differently.
        if local_ref is None:
            code, msg = tools.merge_check_fn(wt, base_branch, task_id)
            artifact_attestation = ArtifactAttestation(
                ArtifactAttestationState.PASSED
                if code == 0
                else ArtifactAttestationState.FAILED,
                msg,
            )
        else:
            local_check = tools.local_check_fn
            if local_check is None:
                from orchestrator.merge_check_local import (
                    check_local_fix as local_check,
                )
            code, msg = local_check(wt, base_branch, task_id)
            if code == 0:
                artifact_attestation = ArtifactAttestation(
                    ArtifactAttestationState.NOT_APPLICABLE,
                    "Director-authored --local commit is judged by fresh "
                    f"trusted-local gates; policy recompute found no stop ({msg})",
                )
            else:
                # A stop the fix tree still manifests (deleted tests, declared
                # high risk, senior deadlock, ...) is NOT waived by the trusted
                # route: --local accommodates only the checks the fix commit
                # invalidates by construction (H5), never the decision value.
                artifact_attestation = ArtifactAttestation(
                    ArtifactAttestationState.FAILED, msg
                )

        from orchestrator.gitops import GitOps

        gitops = GitOps(
            repo_url="unused", work_root=work_root, default_branch=base_branch
        )
        changed = gitops.changed_files(wt, task_id)
        statuses = gitops.changed_statuses(wt, task_id)
        # Per-target policy from TRUSTED main (base_branch) via git show, NOT the
        # branch worktree - a branch cannot weaken its own classification (M1);
        # editing .laddy/policy.toml is itself L3.
        base_sha = _rev(repo, base_branch)
        policy = load_target_policy(repo, ref=base_sha)
        blast = classify_blast_radius(policy, changed, statuses)
        from orchestrator.policy import security_paths, sensitive_paths

        sensitive = tuple(
            sensitive_paths(policy, changed) + security_paths(policy, changed)
        )

        # The deterministic gate runs on the branch TRIAL-MERGED into CURRENT
        # local main (not the branch alone) and baselined to it (#11): catches a
        # pair of branches that pass in isolation but conflict once combined,
        # and re-verifies against prior merges this batch already landed. The
        # gate infra (compose/Dockerfile/semgrep) is restored from local main,
        # not the branch, so a branch cannot ship a hostile container definition
        # that escapes during verification (NÁLEZ 1). verified_sha (the branch
        # tip) stays the TOCTOU pin merge_branch integrates.
        binding = _binding_on_merged_tree(
            repo, task_id, work_root, base_sha, verified_sha, tools.binding_gate,
            policy.coverage_package,
        )

        security: tuple[Verdict, ...] = ()
        rw2: Verdict | None = None
        if blast in (L2, L3):
            prompt = SECURITY_PANEL_PROMPT.format(
                role=_role(tools.roles_dir, "security"), task=task_id, base=base_branch
            )
            security = tuple(run_security_panel(tools.security_runners, prompt, wt))
        if blast == L2:
            rw2 = _rw2(tools.rw2_runner, task_id, wt, tools.roles_dir, base_branch)

        return GateResults(
            blast=blast,
            artifact_attestation=artifact_attestation,
            tests_passed=binding.tests_passed,
            tests_tail=binding.tests_tail,
            coverage_ok=binding.coverage_ok,
            coverage_detail=binding.coverage_detail,
            scan_findings=binding.scan_findings,
            rw2=rw2,
            security_verdicts=security,
            sensitive_files=sensitive,
            head_sha=verified_sha,
            infra_overridden=restored_infra_paths(changed),
        )
    finally:
        _git(repo, "worktree", "remove", "--force", str(wt), check=False)


def _role(roles_dir: Path, name: str) -> str:
    return (roles_dir / f"{name}.md").read_text(encoding="utf-8")


def _rw2(
    runner: AgentRunner, task_id: str, wt: Path, roles_dir: Path, base_branch: str
) -> Verdict | None:
    prompt = RW2_LOCAL_PROMPT.format(
        role=_role(roles_dir, "rw2"), task=task_id, base=base_branch
    )
    try:
        verdict, _ = request_verdict(runner, prompt, wt)
        return verdict
    except VerdictError as exc:
        return _abstention_blocker("rw2", str(exc))


def merge_branch(
    repo: Path,
    task_id: str,
    verified_sha: str,
    base_branch: str = "main",
    branch_remote: str = "origin",
    fetch: bool = True,
    advisory: Sequence[str] = (),
) -> bool:
    """Merge the VERIFIED commit of <task> into local main.

    Merges ``verified_sha`` (the exact commit the local gate saw), NOT the
    branch ref: the untrusted VPS can push the task branch between verify
    and merge, so integrating the ref would let an unverified commit sneak
    into main (verify->merge TOCTOU). Fetching refreshes the object store so
    the sha is present; the merge names the sha, so a moved tip is
    irrelevant. Never edits code - a conflict is a hold, not a fix.

    ``fetch=False`` (--local) skips the remote refresh: the judged sha is a
    local commit already in the object store, and there may be no reachable
    remote at all. The sha is still the one the gates pinned, so judged ==
    merged holds identically.

    When ``advisory`` is non-empty, its durable record is written and staged
    while the merge is still uncommitted. Code and record then enter trusted
    main in one merge commit. Any preparation or commit failure aborts the
    merge, leaving the original main commit in place; there is no post-merge
    callback and no destructive reset rollback.

    ``branch_remote`` is where task branches live (see discover_ready); the
    merge target ``base_branch`` is always integrated locally and later
    pushed to "origin" (GitHub) by push_and_cleanup, regardless of
    branch_remote.
    """
    if not verified_sha:
        raise ValueError("merge_branch requires the verified sha (TOCTOU pin)")
    if fetch:
        _git(repo, "fetch", branch_remote, task_id)
    _git(repo, "checkout", base_branch)
    _, original_head = _git(repo, "rev-parse", "HEAD")
    code, _ = _git(
        repo,
        "-c", "user.name=myapp-merge",
        "-c", "user.email=merge@myapp.local",
        "merge", "--no-ff", "--no-commit", verified_sha,
        check=False,
    )
    if code != 0:
        _git(repo, "merge", "--abort", check=False)
        return False

    merge_head_code, _ = _git(
        repo, "rev-parse", "--verify", "MERGE_HEAD", check=False
    )
    if merge_head_code != 0:
        return False

    try:
        if advisory:
            from orchestrator.artifacts import TaskArtifacts

            relative_record = (
                f"{TARGET_DIR_NAME}/tasks/{task_id}/merge-advisory.md"
            )
            record = repo / relative_record
            try:
                record.lstat()
            except FileNotFoundError:
                pass
            else:
                # A task is merged once. An existing record either came from
                # the untrusted branch or was already present on trusted main;
                # overwriting either would destroy provenance, so fail closed.
                raise MergePreparationError(
                    f"advisory record path already exists: {relative_record}"
                )
            TaskArtifacts(repo, task_id).write_text(
                "merge-advisory.md", render_advisory(task_id, advisory)
            )
            _git(repo, "add", "--", relative_record)

        commit_code, commit_output = _git(
            repo,
            "-c", "user.name=myapp-merge",
            "-c", "user.email=merge@myapp.local",
            "commit", "-m", merge_subject(task_id, verified_sha),
            check=False,
        )
        if commit_code != 0:
            raise MergePreparationError(
                "git could not create the atomic merge commit: " + commit_output
            )
    except Exception as exc:
        _git(repo, "merge", "--abort", check=False)
        _, restored_head = _git(repo, "rev-parse", "HEAD")
        if restored_head != original_head:
            raise RuntimeError(
                "merge abort did not restore the original trusted main commit"
            ) from exc
        if isinstance(exc, MergePreparationError):
            raise
        raise MergePreparationError(str(exc)) from exc
    return True


def push_and_cleanup(
    repo: Path,
    merged_tasks: Sequence[str],
    base_branch: str = "main",
    branch_remote: str = "origin",
) -> None:
    """Push local main to GitHub ("origin") and DELETE each merged <task>
    branch from ``branch_remote`` (where it actually lives - "origin" unless
    overridden, e.g. "vps" for a bare-hub VPS with read-only GitHub access).

    Pure git, no AI. The merged commits are already in local main, so deleting
    the remote branch loses nothing - it just stops merged branches cluttering
    the ones still awaiting the Director. Held branches are never touched.
    """
    _git(repo, "push", "origin", base_branch)
    for task_id in merged_tasks:
        _git(repo, "push", branch_remote, "--delete", task_id, check=False)


def _oracle_notice(repo: Path) -> None:
    """Non-progress oracle trigger notice (oracle design 2026-07-12).

    The merge tool is where merges happen, so it is the one place an
    "oracle review due" cannot be forgotten - the AUTO half of "automate
    the trigger, keep the run manual". Purely informational: it must never
    block or break a merge run.
    """
    try:
        from orchestrator.oracle import trigger as _trigger

        status = _trigger.check(repo)
    except Exception as exc:  # noqa: BLE001 - a notice must never break merging
        print(f"[oracle] trigger check failed: {exc}")
        return
    if status.due:
        print(f"[oracle] review DUE: {'; '.join(status.reasons)}")
        print("[oracle] next: python -m orchestrator.oracle status  (then scope/prepare)")


# --- CLI ---------------------------------------------------------------------


def _default_tools(config: object) -> GateTools:
    from orchestrator.agents import ClaudeRunner, CodexRunner
    from orchestrator.config import OrchestratorConfig
    from orchestrator.merge_check import check as _merge_check
    from orchestrator.merge_check_local import check_local_fix as _local_check
    from orchestrator.run import default_roles_dir

    assert isinstance(config, OrchestratorConfig)
    # The LOCAL panel reviews untrusted code on the trusted machine, so it uses
    # the least-privilege review commands (no write/exec), NOT the convergence
    # commands (trust-model S4/S10). config enforces this even under override.
    return GateTools(
        merge_check_fn=_merge_check,
        # deterministic gate: tests + coverage + semgrep(offline) + gitleaks,
        # all in one containerized run at the pinned sha (nothing on the host)
        binding_gate=BindingGate(),
        # rw2 = cross-vendor (Codex); security panel = diverse models
        rw2_runner=CodexRunner(config.review_codex_cmd),
        security_runners=(
            ClaudeRunner(config.review_senior_cmd),  # Opus lens, read-only
            CodexRunner(config.review_codex_cmd),  # cross-vendor lens, read-only
        ),
        roles_dir=default_roles_dir(),
        # --local ONLY: the fail-closed policy recompute on the fix tree (H5)
        local_check_fn=_local_check,
    )


def _interactive_confirm(v: MergeVerdict) -> bool:
    """Merge-safety confirmation for ANY merge into local main (H4).

    L1/L2 AUTO_MERGE and L3 RISK_DECISION alike: the operator must type the
    EXACT task id. A wrong or blank id declines - nothing is merged and the
    task simply stays ready/held. A task id the terminal cannot reproduce
    (hostile control characters) is untypeable and therefore unmergeable:
    that is fail-closed, not a defect.
    """
    if v.digest:  # L3 risk context; an L1/L2 auto-merge has no digest
        print("\n" + v.digest)
    # The candidate id is rendered safely here; the input() prompt itself
    # stays entirely static so attacker-influenced text can never restyle it.
    print(f"\n[confirm] merge candidate: {untrusted_inline(v.task_id)}")
    print("[confirm] merging into local main requires typing the EXACT task id;")
    print("[confirm] anything else declines and merges nothing.")
    typed = input("[confirm] type the exact task id to merge (blank declines) > ")
    if typed.strip() != v.task_id:
        print("[confirm] declined - the typed id does not match; nothing merged.")
        return False
    return True


def _ask(prompt: str) -> bool:
    return input(prompt).strip().lower() == "y"


# One headline per tamper shape (M3) so the Director knows WHAT happened, not
# just that something did; check_hub_main's detail then names the shas.
_TRIPWIRE_HEADLINES: Mapping[HubMainState, str] = {
    HubMainState.DELETED: (
        "[TRIPWIRE] hub main DISAPPEARED: the hub had a main and now has none."
    ),
    HubMainState.REWOUND: (
        "[TRIPWIRE] hub main was REWOUND: force-pushed off the fast-forward path."
    ),
    HubMainState.DIVERGED: (
        "[TRIPWIRE] hub main is NOT an ancestor of local main."
    ),
}


def _print_tripwire(check: HubMainCheck, branch_remote: str) -> None:
    print(_TRIPWIRE_HEADLINES[check.state])
    if check.detail:
        print(f"[TRIPWIRE] {check.detail}")
    print("The VPS must never write main - this is suspicion of an")
    print("unauthorized write (spec S5). NOTHING was merged. Inspect the")
    print(f"hub ({branch_remote}) and ~/laddy on the VPS, then decide.")


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    confirm: Callable[[MergeVerdict], bool] | None = None,
    ask: Callable[[str], bool] | None = None,
    pusher: Callable[[Path, Sequence[str]], None] | None = None,
) -> int:
    import argparse
    import os

    from orchestrator.artifacts import TaskArtifacts
    from orchestrator.config import OrchestratorConfig

    parser = argparse.ArgumentParser(prog="orchestrator.local_merge")
    parser.add_argument("--repo", default=".", type=Path)
    parser.add_argument("--work-root", default=None)
    parser.add_argument(
        "--no-input",
        action="store_true",
        help="dry run: never prompt, hold everything, mutate nothing (no merge, no push)",
    )
    parser.add_argument(
        "--local",
        default=None,
        metavar="REF",
        help="judge a locally-committed <REF> (sha/branch/worktree path) of ONE "
        "task with the full gate, no fetch - the Director-authored fix path",
    )
    parser.add_argument(
        "--advisory",
        action="store_true",
        help="waive the JUDGMENT gates (security panel + rw2): record their "
        "findings to merge-advisory.md (committed into local main) and merge "
        "anyway. Deterministic gates (VPS attestation when applicable, tests, "
        "coverage, scan, and infra) still fail closed. Opt-in; off by default.",
    )
    parser.add_argument("task", nargs="*", help="task ids; default = all ready")
    args = parser.parse_args(argv)

    local_ref: str | None = args.local
    if local_ref is not None:
        # --local judges exactly one named, locally-committed revision; discovery
        # is bypassed and a missing/blank ref or a task count != 1 is a hard error
        # (nothing is merged).
        if len(args.task) != 1:
            parser.error("--local requires exactly one task id")
        if not local_ref.strip():
            parser.error("--local requires a non-empty <ref>")

    repo = args.repo.resolve()
    config = OrchestratorConfig.from_env(env if env is not None else os.environ)
    work_root = Path(args.work_root) if args.work_root else default_work_root(repo, "merge")
    work_root.mkdir(parents=True, exist_ok=True)
    tools = _default_tools(config)

    # Dirty-tree guard (--local only): what is judged must be exactly a committed
    # revision, so nothing uncommitted can ride along into the merged tree
    # (judged == merged). Refuse before any gate runs; nothing is merged.
    if local_ref is not None:
        _, porcelain = _git(repo, "status", "--porcelain")
        if porcelain.strip():
            print("[dirty] the target working tree has uncommitted changes.")
            print("--local judges a committed revision so judged == merged;")
            print("commit or stash your changes first. NOTHING was merged.")
            return 1

    # Tripwire (spec S5, M3) - before the engine runs, and deliberately with NO
    # fetch first: check_hub_main consults the hub via ls-remote and compares
    # against the remote-tracking ref, git's own record of the last verified
    # hub main. The old pre-check `fetch --prune` erased exactly that evidence,
    # so a deleted hub main read as a benign fresh hub (M3a). In --local mode
    # an absent/unreachable remote only warns and proceeds - the whole point of
    # --local is "no VPS"; in the default mode a hub that cannot be consulted
    # cannot be tripwire-checked either, so the run fails closed.
    check = check_hub_main(repo, config.branch_remote, config.default_branch)
    if check.state is HubMainState.UNREACHABLE:
        if local_ref is None:
            print(f"[TRIPWIRE] branch remote '{config.branch_remote}' is "
                  "absent/unreachable;")
            print("the hub-main tripwire cannot be verified, so NOTHING is")
            print("merged (fail closed). Fix the remote and re-run.")
            return 2
        print(f"[WARN] branch remote '{config.branch_remote}' is absent/unreachable;")
        print("--local proceeds with no hub to consult (the 'no VPS' case).")
    elif not check.ok:
        _print_tripwire(check, config.branch_remote)
        return 2

    _confirm = confirm or ((lambda v: False) if args.no_input else _interactive_confirm)
    _ask_fn = ask or ((lambda p: False) if args.no_input else _ask)
    _push = pusher or (
        lambda r, tasks: push_and_cleanup(
            r, tasks,
            base_branch=config.default_branch,
            branch_remote=config.branch_remote,
        )
    )
    merged: list[str] = []

    def _report(v: MergeVerdict) -> None:
        safe_task = untrusted_inline(v.task_id)
        if v.merged:
            merged.append(v.task_id)
            if v.advisory:
                # Honest labeling (constraint 5): an advisory merge is visibly
                # distinct and never presented as a fully-verified merge.
                print(
                    f"[merge*] {safe_task}: MERGED under --advisory - judgment "
                    "gates WAIVED, NOT fully verified"
                )
                print(
                    f"         waived findings recorded in {TARGET_DIR_NAME}/tasks/"
                    f"{safe_task}/merge-advisory.md"
                )
            else:
                print(f"[merge] {safe_task}: MERGED into local main")
            return
        art = TaskArtifacts(repo, v.task_id)
        art.write_text("merge-hold.md", v.digest)
        if v.kind == BROKEN:
            # diagnostic: what failed, why, what is needed (no merge offer)
            print(
                f"[broken] {safe_task}: "
                + "; ".join(untrusted_inline(reason) for reason in v.reasons)
            )
            print(f"         see {TARGET_DIR_NAME}/tasks/{safe_task}/merge-hold.md")
        elif v.kind == DRY_RUN:
            if v.advisory:
                # Preview must flag an advisory-eligible branch distinctly, never
                # as a clean auto-merge (constraint 5): a real run would waive
                # these findings and commit them into main.
                print(
                    f"[dry-run*] {safe_task}: WOULD merge under --advisory - "
                    "judgment gates WOULD be WAIVED (nothing changed)"
                )
                print(f"           see {TARGET_DIR_NAME}/tasks/{safe_task}/merge-hold.md")
            else:
                print(f"[dry-run] {safe_task}: WOULD auto-merge (nothing changed)")
        else:
            print(f"[hold]  {safe_task}: declined / not confirmed")

    if args.advisory:
        print("[ADVISORY] --advisory is ON. The security panel and rw2 judgment")
        print("gates are WAIVED: their findings are recorded to merge-advisory.md")
        print("(committed into local main) and the branch merges anyway. The")
        print("deterministic gates (attestation/tests/coverage/scan/infra) STILL block.")

    engine = LocalMergeEngine(
        # --local processes only the one named task (discovery bypassed): the
        # gate sources the local ref, and the merge skips the fetch (the judged
        # sha is a local commit; there may be no reachable remote).
        list_ready=lambda: (
            args.task if local_ref is not None
            else (
                args.task
                or discover_ready(
                    repo,
                    base_branch=config.default_branch,
                    branch_remote=config.branch_remote,
                )
            )
        ),
        # In the default path the call signature is unchanged (no local_ref
        # kwarg), so existing gather_gates fakes/tests are untouched; --local
        # adds the kwarg only when it is actually in local mode.
        verify_one=lambda t: gather_gates(
            t, repo, work_root, tools,
            branch_remote=config.branch_remote, base_branch=config.default_branch,
            **({"local_ref": local_ref} if local_ref is not None else {}),
        ),
        merge_one=lambda request: merge_branch(
            repo, request.task_id, request.verified_sha,
            base_branch=config.default_branch,
            branch_remote=config.branch_remote,
            fetch=local_ref is None,
            advisory=request.advisory,
        ),
        on_verdict=_report,
        confirm=_confirm,
        dry_run=args.no_input,
        advisory_mode=args.advisory,
    )
    results = engine.run()
    held = [v for v in results if not v.merged]
    print(f"\n{len(merged)} merged, {len(held)} held.")
    _oracle_notice(repo)

    if merged and _ask_fn(
        f"[push] push main to origin and delete {len(merged)} merged branch(es)? (y/N) > "
    ):
        _push(repo, merged)
        print(
            "[push] main pushed; deleted: "
            + ", ".join(untrusted_inline(task) for task in merged)
        )
    elif merged:
        print(
            "[push] not pushed. Merged locally: "
            + ", ".join(untrusted_inline(task) for task in merged)
            + f". Push with: git push origin {config.default_branch}"
        )

    return 1 if held else 0


if __name__ == "__main__":
    raise SystemExit(main())
