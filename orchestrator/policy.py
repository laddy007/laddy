"""Merge policy: sensitive paths, risk tiers, path guards, merge decision
(design doc S8).

Single home for all path policy so no guard restates a subset elsewhere
(CLAUDE.md R1/R3). The same functions run on the VPS (writing
merge-decision.json) and off-VPS in the Actions merge check (recomputing
the decision from the actual diff - decision D9).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any

from orchestrator import TARGET_DIR_NAME
from orchestrator.spec import DONE_STATUS, DRAFT_STATUS, SpecError, parse_spec_text
from orchestrator.target_policy import TargetPolicy

# Product-specific policy (sensitive/security/invariant paths, coverage package,
# frontend gate, ...) is NOT hardcoded here - it lives in the target's own
# <target>/.laddy/policy.toml and is threaded in as a TargetPolicy (M1). These
# functions stay pure: the caller loads the policy and passes it.


def touches_invariant_tests(
    policy: TargetPolicy, changed_files: Sequence[str]
) -> bool:
    """Architecture-contract / append-only tests: touching one is a senior-review
    trigger (S6) and a stop-before-merge condition (S8, test weakening)."""
    return any(f in policy.invariant_tests for f in changed_files)


# --- Report-only path guard (design S8) --------------------------------------

# A report-only task (audit/investigate) may only touch its own artifacts and
# markdown spec drafts. Any source file in the diff is a policy violation.
# Deliberately NOT here: the docs/ subtree (a prefix admits executable files
# like docs/conftest.py, and there is no docs/ tree by design) and non-.md
# files under the spec dir (only markdown specs are inert enough; path_guard
# enforces the .md suffix for the spec dir separately).
REPORT_ALLOWED_PREFIXES = (
    f"{TARGET_DIR_NAME}/tasks/{{task}}/",
)

_SPEC_PREFIX = f"{TARGET_DIR_NAME}/specs/"


def path_guard(task_id: str, changed_files: Sequence[str]) -> tuple[bool, list[str]]:
    """Returns (ok, offending_paths) for a report-only diff."""
    allowed = tuple(p.format(task=task_id) for p in REPORT_ALLOWED_PREFIXES)
    offending = [
        f
        for f in changed_files
        if not (
            f.startswith(allowed)
            or (f.startswith(_SPEC_PREFIX) and f.endswith(".md"))
        )
    ]
    return (not offending, offending)


def nondraft_report_specs(
    task_id: str, changed_files: Sequence[str], read_text: Callable[[str], str]
) -> list[str]:
    """Report-only spec files in the diff that are NOT draft-proposal (C3).

    A report-only task may PROPOSE fix specs, but only as drafts: a merged
    non-draft spec runs autonomously on the next kickoff, so an investigator
    that writes ``specs/<other>.md`` with ``status: ready`` would auto-merge a
    self-authored runnable task. Any changed spec must be a draft; a non-draft,
    missing status, or a parse error is a violation (the loop's
    ``_force_draft_status`` only covers the fix-spec it writes itself, not
    files the agent writes directly).

    The task's OWN spec (``specs/<task>.md``) is the runnable input being
    executed, not a new proposal, so it is exempt from the draft requirement -
    but only while its NEW content stays a report-only task that will not
    re-run autonomously (H3). Legitimate edits (the clarify gate appends a
    ``## Clarifications`` block) keep type and status untouched; flipping the
    type to an executable composition (report_only -> off), promoting the
    status (e.g. ``status: ready``), or making the spec unparseable would land
    a self-authored runnable task through the report-only lane, so each is a
    violation.
    """
    own = f"{_SPEC_PREFIX}{task_id}.md"
    offending: list[str] = []
    for f in changed_files:
        if not (f.startswith(_SPEC_PREFIX) and f.endswith(".md")):
            continue  # non-.md spec-dir files are path_guard violations already
        try:
            spec = parse_spec_text(read_text(f))
        except (SpecError, OSError):
            offending.append(f)  # an unparseable spec cannot be certified safe
            continue
        if f == own:
            if not spec.report_only or spec.status not in (
                None,
                DRAFT_STATUS,
                DONE_STATUS,
            ):
                offending.append(f)
        elif not spec.is_draft:
            offending.append(f)
    return offending


# --- Policy-sensitive / security paths (design S8: never auto-merge) ----------
#
# The glob LISTS are per-target (target_policy.TargetPolicy, from
# <target>/.laddy/policy.toml); the engine-generic set (secrets, agent config,
# supply chain, engine surfaces, gate infra, the policy file itself) and the
# invariant tests are merged in by TargetPolicy.all_sensitive_globs. NOT
# sensitive by design: pytest config (conftest.py / pytest.ini) - routing it to
# L3 would drop the rw2 cross-vendor review (L2 only) for a summary-only human
# gate, weaker for spotting a malicious collection hook (which needs the diff
# READ); the binding gate's exit-code authority (testgate.py) already closes the
# forge-the-result vector.

# path-like tokens in free spec text: bare or backtick-quoted paths with a "/"
_PATH_TOKEN_RE = re.compile(r"[\w.\-*/]*/[\w.\-*/]+")


def spec_is_high_risk(
    policy: TargetPolicy, spec_text: str, risk: str | None
) -> bool:
    """A task is high-risk if its spec declares ``risk: high`` or references a
    path matching a sensitive glob (the same list merge-decision applies to
    changed files, so there is one definition of 'sensitive')."""
    if (risk or "").strip().lower() == "high":
        return True
    globs = policy.all_sensitive_globs
    for token in _PATH_TOKEN_RE.findall(spec_text):
        if any(fnmatch(token, g) for g in globs):
            return True
    return False


RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

# Mechanical size thresholds for computed risk (S13: max(declared, computed)).
_MEDIUM_FILES = 15
_MEDIUM_LINES = 300


def sensitive_paths(policy: TargetPolicy, changed_files: Sequence[str]) -> list[str]:
    globs = policy.all_sensitive_globs
    return [f for f in changed_files if any(fnmatch(f, g) for g in globs)]


def security_paths(policy: TargetPolicy, changed_files: Sequence[str]) -> list[str]:
    return [
        f for f in changed_files if any(fnmatch(f, g) for g in policy.security_globs)
    ]


def computed_risk(
    policy: TargetPolicy, changed_files: Sequence[str], diff_lines: int
) -> str:
    if sensitive_paths(policy, changed_files) or security_paths(policy, changed_files):
        return "high"
    if len(changed_files) > _MEDIUM_FILES or diff_lines > _MEDIUM_LINES:
        return "medium"
    return "low"


def effective_risk(declared: str, computed: str) -> str:
    return declared if RISK_ORDER.get(declared, 2) >= RISK_ORDER.get(computed, 2) else computed


def user_visible(policy: TargetPolicy, changed_files: Sequence[str]) -> bool:
    return any(f.startswith(policy.user_visible_prefixes) for f in changed_files)


# --- Blast-radius classification (trust-model doc S8: L1/L2/L3) ---------------
#
# The local merge tool routes by blast radius, NOT by "can a human read it":
#   L1 safe_by_construction -> auto-merge after mechanical gates, no review
#   L2 ordinary_logic        -> the agents ARE the gate (tests + rw2 + security)
#   L3 sensitive             -> security panel -> digested risk -> human Y/N
# "human reads the logic diff" is rejected as a fake gate (trust-model S1.2).

# L1: change classes with (almost) nowhere for malice to hide. Deliberately
# conservative - when unsure, a file falls through to L2 (agents gate it).
# NOTE: fnmatch's ``*`` crosses ``/`` (unlike a shell glob), so a bare
# ``docs/*`` / ``docs/**/*`` would match ``docs/anything/evil.py`` and route
# executable code into the no-review L1 auto-merge path — a fail-open hole
# under the "branch is attacker-controlled" threat model. L1 is therefore an
# INERT-EXTENSION allowlist (markdown + the target's declared data catalogues),
# never a directory allowlist: any non-inert file falls through to L2 so the
# agent panel gates it. The catalogue globs are per-target (policy.all_safe_globs
# = engine markdown + the toml's ``safe_globs``).

L1 = "L1"  # safe-by-construction
L2 = "L2"  # ordinary logic
L3 = "L3"  # sensitive surface


def _is_safe_by_construction(policy: TargetPolicy, path: str) -> bool:
    """Only docs / declared data catalogues qualify - true data, no logic.

    Test files are deliberately excluded even when purely added: a test is
    executable Python that runs on the host at every ``pytest`` after merge,
    and an added ``conftest.py`` can neutralize the gate through collection
    hooks. Code is never safe-by-construction; added tests go to L2 so the
    agents review them (NÁLEZ 3).

    Task specs are excluded for the same reason even though they are markdown
    (H2): ``<agent-dir>/specs/*.md`` is an EXECUTABLE task description - a
    merged non-draft spec runs autonomously on the next kickoff/enqueue - so a
    spec never rides the L1 no-review lane; it falls through to L2 where the
    agents gate it. The exclusion is engine-side and unconditional: a target's
    ``safe_globs`` cannot re-admit the spec dir.
    """
    if path.startswith(_SPEC_PREFIX):
        return False
    return any(fnmatch(path, g) for g in policy.all_safe_globs)


def classify_blast_radius(
    policy: TargetPolicy,
    changed_files: Sequence[str],
    changed_statuses: Mapping[str, str] | None = None,  # noqa: ARG001 - kept for caller API stability; L1 no longer status-dependent
) -> str:
    """Route a diff to L1 / L2 / L3 by blast radius (trust-model doc S8).

    Sensitive/security surface wins (L3); else all-safe-by-construction is L1;
    else ordinary logic is L2.
    """
    if not changed_files:
        # An empty changed set is an anomaly, most realistically a FAILED
        # diff-gather upstream. Never let that fail open into the no-review
        # auto-merge path (it used to return L1): treat it as sensitive so the
        # merge holds for a human instead of auto-merging "nothing".
        return L3
    if sensitive_paths(policy, changed_files) or security_paths(policy, changed_files):
        return L3
    if all(_is_safe_by_construction(policy, f) for f in changed_files):
        return L1
    return L2


def deleted_test_files(
    policy: TargetPolicy, changed_statuses: Mapping[str, str]
) -> list[str]:
    """changed_statuses: path -> git status letter (A/M/D/R...).

    Test locations are per-target (``policy.all_test_dirs``): the engine
    default (literal tests/) always applies and a target can only ADD its own
    dirs (src/tests/, frontend/__tests__/, ...), never remove the default (M4).
    """
    prefixes = policy.all_test_dirs
    return [
        p
        for p, s in changed_statuses.items()
        if s.startswith("D") and p.startswith(prefixes)
    ]


def destructive_migrations(
    policy: TargetPolicy, changed_files: Sequence[str], read_text: Any
) -> list[str]:
    """Migration files (per-target ``migration_globs``) containing
    drop_table/drop_column (heuristic)."""
    out: list[str] = []
    for f in changed_files:
        if any(fnmatch(f, g) for g in policy.migration_globs):
            try:
                text = read_text(f)
            except OSError:
                continue  # deleted migration - caught by sensitive_paths anyway
            if text and ("drop_table" in text or "drop_column" in text):
                out.append(f)
    return out


@dataclass(frozen=True)
class GateStates:
    """SHA-keyed gate results, read from state.json / verdict artifacts."""

    head_sha: str
    rw1_sha: str | None
    rw1_approved: bool
    rw2_sha: str | None
    rw2_go: bool
    authoritative_sha: str | None
    authoritative_passed: bool
    authoritative_flaky: bool


@dataclass(frozen=True)
class MergeDecision:
    decision: str  # "auto_merge" | "auto_merge_notify" | "stop_before_merge"
    risk_level: str
    reasons: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "risk_level": self.risk_level,
            "reasons": list(self.reasons),
        }


def merge_decision(
    *,
    policy: TargetPolicy,
    changed_files: Sequence[str],
    diff_lines: int,
    declared_risk: str,
    gates: GateStates,
    changed_statuses: Mapping[str, str] | None = None,
    migration_texts: Any = None,
    senior_deadlock: bool = False,
    unclear_intent: bool = False,
) -> MergeDecision:
    """The S8 decision matrix. Pure; callers gather the inputs (incl. the
    per-target policy)."""
    risk = effective_risk(
        declared_risk, computed_risk(policy, changed_files, diff_lines)
    )
    reasons: list[str] = []

    # merge precondition: all three gates on the CURRENT HEAD SHA
    if not (gates.rw1_approved and gates.rw1_sha == gates.head_sha):
        reasons.append("stale_or_missing_rw1_approval")
    if not (gates.rw2_go and gates.rw2_sha == gates.head_sha):
        reasons.append("stale_or_missing_rw2_approval")
    if not (gates.authoritative_passed and gates.authoritative_sha == gates.head_sha):
        reasons.append("stale_or_missing_authoritative_green")
    if gates.authoritative_flaky:
        reasons.append("flaky_authoritative_tests")

    if sensitive := sensitive_paths(policy, changed_files):
        reasons.append(f"policy_sensitive_paths: {', '.join(sensitive[:5])}")
    if security := security_paths(policy, changed_files):
        reasons.append(f"security_auth_paths: {', '.join(security[:5])}")
    if changed_statuses and (deleted := deleted_test_files(policy, changed_statuses)):
        reasons.append(f"test_files_deleted: {', '.join(deleted[:5])}")
    if migration_texts is not None and (
        destructive := destructive_migrations(policy, changed_files, migration_texts)
    ):
        reasons.append(f"destructive_migrations: {', '.join(destructive[:5])}")
    if senior_deadlock:
        reasons.append("senior_escalation_without_clean_verdict")
    if unclear_intent:
        reasons.append("unclear_product_intent")
    if risk == "high":
        reasons.append("high_risk")

    if reasons:
        return MergeDecision("stop_before_merge", risk, tuple(reasons))
    if risk == "medium" and user_visible(policy, changed_files):
        return MergeDecision("auto_merge_notify", risk)
    return MergeDecision("auto_merge", risk)


def report_only_decision(
    *,
    task_id: str,
    changed_files: Sequence[str],
    verify_confirmed: bool,
    nondraft_specs: Sequence[str] = (),
) -> MergeDecision:
    """Report-only precondition: {verify APPROVED, path-guard pass, every
    proposed spec is a draft} (design S5, C3)."""
    ok, offending = path_guard(task_id, changed_files)
    reasons: list[str] = []
    if not ok:
        reasons.append(f"path_guard_violation: {', '.join(offending[:5])}")
    if nondraft_specs:
        reasons.append(f"nondraft_spec_in_report_only: {', '.join(nondraft_specs[:5])}")
    if not verify_confirmed:
        reasons.append("verify_round_missing_or_failed")
    if reasons:
        return MergeDecision("stop_before_merge", "low", tuple(reasons))
    return MergeDecision("auto_merge", "low")
