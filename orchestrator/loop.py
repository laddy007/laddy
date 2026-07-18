"""Loop sequencing + resume derivation (design doc S5, S6, S9).

State is derived by replaying the append-only iteration log - never from
agent sessions. ``claude -p --resume`` session ids are carried along as a
token optimization only; losing them changes cost, not behavior.

Slice 2 adds the rw2 cross-vendor guard, the authoritative Docker gate on
the current HEAD SHA, and the backstop (oscillation fingerprint -> senior
reviewer escalation).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from orchestrator import TARGET_DIR_NAME
from orchestrator.agents import AgentRunner
from orchestrator.artifacts import (
    AUTHORITATIVE,
    EXPLORATION,
    FINDINGS,
    FINDINGS_PROPOSED,
    MERGE_DECISION,
    REPORT,
    RW1_VERDICT,
    RW2_VERDICT,
    SENIOR_VERDICT,
    STATE,
    TaskArtifacts,
)
from orchestrator.fingerprint import failure_fingerprint as failure_fp
from orchestrator.fingerprint import repeats, verdict_fingerprint
from orchestrator.gitops import GitOps
from orchestrator.handoff import NtfyNotifier, write_handback, write_human_summary
from orchestrator.policy import (
    RISK_ORDER,
    GateStates,
    MergeDecision,
    merge_decision,
    nondraft_report_specs,
    path_guard,
    report_only_decision,
    touches_invariant_tests,
)
from orchestrator.quota import QuotaAwareRunner, QuotaBudget, QuotaPolicy, QuotaTimeout
from orchestrator.target_policy import load_target_policy
from orchestrator.terminals import terminal_spec
from orchestrator.testgate import DockerGate, ShellRunner, frontend_touched, run_fast
from orchestrator.verdict import (
    VerdictError,
    request_payload,
    request_verdict,
    validate_rw2,
)
from orchestrator.verdict import (
    extract_json as _extract_json,
)


@dataclass(frozen=True)
class ResumePoint:
    round: int
    phase: str
    dev_session: str | None
    rw1_session: str | None
    rw2_session: str | None


_PHASE_ACTIONS = ("developer", "fast_tests", "rw1", "rw2", "authoritative", "senior", "push")


def _recorded_terminal(entries: Sequence[Mapping[str, Any]]) -> str | None:
    """Return the already-recorded terminal state of a run, or None.

    A sticky terminal is replay-idempotent: re-invoking run() after a run
    finished (a VPS reboot + re-kickoff, a manual --phase loop) must NOT
    re-execute the terminal - that re-fires the single ntfy and, worst case
    for a report-only task whose investigator was malformed, pushes an EMPTY
    deliverable as an auto-merge. Both the loop's `terminal` log entries and
    a completed `push` count; the caller short-circuits on a non-None result.

    RETRYABLE states (terminals.py: INTERNAL_ERROR, QUOTA_TIMEOUT) record a
    transient environment condition so the Director is notified, but a
    re-kickoff after the cause is addressed (git back up, quota window reset -
    QuotaBudget is per-run) must RESUME and finish the task, not return the
    stale state forever. They read as non-terminal here; the log entry stays
    as a record and derive_resume_point replays the last real phase.
    """
    for entry in reversed(entries):
        action = entry.get("action")
        if action == "terminal":
            outcome = str(entry.get("outcome"))
            return outcome if terminal_spec(outcome).sticky else None
        if action == "push" and entry.get("outcome") == "ok":
            return "PUSHED"
    return None


def derive_resume_point(
    entries: Sequence[Mapping[str, Any]],
    max_loops: int,
    *,
    with_rw2: bool = False,
    with_authoritative: bool = False,
) -> ResumePoint:
    """Replay the log and compute the next action (pure function)."""
    dev_session: str | None = None
    rw1_session: str | None = None
    rw2_session: str | None = None
    rounds_used = 0
    last: Mapping[str, Any] | None = None

    for entry in entries:
        action = entry.get("action")
        if action == "developer":
            rounds_used += 1
            dev_session = entry.get("session_id") or dev_session
        elif action == "rw1":
            rw1_session = entry.get("session_id") or rw1_session
        elif action == "rw2":
            rw2_session = entry.get("session_id") or rw2_session
        if action in _PHASE_ACTIONS:
            last = entry

    after_rw1 = "rw2" if with_rw2 else ("authoritative" if with_authoritative else "push")
    after_go = "authoritative" if with_authoritative else "push"

    if last is None:
        next_phase = "developer"
    else:
        key = (last["action"], last["outcome"])
        transitions: dict[tuple[str, str], str] = {
            ("developer", "ok"): "fast_tests",
            ("developer", "error"): "developer",
            ("fast_tests", "fail"): "developer",
            ("fast_tests", "pass"): "rw1",
            ("rw1", "changes_requested"): "developer",
            ("rw1", "malformed"): "developer",
            ("rw1", "approved"): after_rw1,
            ("rw2", "nogo"): "developer",
            ("rw2", "malformed"): "developer",
            ("rw2", "go"): after_go,
            ("authoritative", "fail"): "developer",
            ("authoritative", "pass"): "push",
            ("senior", "changes_requested"): "developer",
            ("senior", "approved"): after_go,
            ("senior", "deadlock"): "deadlock",
            ("push", "ok"): "done",
        }
        next_phase = transitions.get(key, "developer")

    if next_phase == "developer" and rounds_used >= max_loops:
        next_phase = "cap_reached"

    next_round = (
        rounds_used + 1 if next_phase in ("developer", "cap_reached") else max(rounds_used, 1)
    )
    return ResumePoint(
        round=next_round,
        phase=next_phase,
        dev_session=dev_session,
        rw1_session=rw1_session,
        rw2_session=rw2_session,
    )


# --- Backstop helpers (design S6), pure over log entries ---------------------


def _fingerprints(entries: Sequence[Mapping[str, Any]], action: str) -> list[str | None]:
    return [e.get("fingerprint") for e in entries if e.get("action") == action]


def nonconvergence_detected(entries: Sequence[Mapping[str, Any]]) -> bool:
    """Same rw2 binding finding or same authoritative failure repeating.

    Only rounds SINCE the last senior verdict count: a senior intervention
    resolves the dispute it was called for, so the backstop arms afresh.
    """
    since = list(entries)
    for i in range(len(since) - 1, -1, -1):
        if since[i].get("action") == "senior":
            since = since[i + 1 :]
            break
    if repeats(_fingerprints(since, "rw2")):
        return True
    if repeats(_fingerprints(since, "authoritative")):
        return True
    # reviewer disagreement on a binding matter: rw1 keeps approving,
    # rw2 keeps blocking
    nogos = [e for e in since if e.get("action") == "rw2" and e.get("outcome") == "nogo"]
    return len(nogos) >= 2


def senior_ran(entries: Sequence[Mapping[str, Any]]) -> bool:
    return any(e.get("action") == "senior" for e in entries)


def report_content_sha(artifacts: TaskArtifacts) -> str:
    """Content hash of a report-only task's deliverables (report + findings).

    Binds the verify round to the exact content being merged: if report.md
    or findings.json is rewritten after verify (tampering / resume-rewrite),
    the hash changes and report_only auto-merge is refused - the report-only
    analogue of the code path's SHA-keyed approvals.
    """
    h = hashlib.sha256()
    for name in (REPORT, FINDINGS):
        data = artifacts.read_text(name)
        h.update(b"\x00" if data is None else data.encode("utf-8"))
    return h.hexdigest()


def report_verify_confirmed(artifacts: TaskArtifacts) -> bool:
    """A verify round is valid only if its recorded content hash still matches
    the current report/findings (see report_content_sha)."""
    current = report_content_sha(artifacts)
    return any(
        e.get("action") == "verify"
        and e.get("outcome") == "ok"
        and e.get("content_sha") == current
        for e in artifacts.read_log()
    )


def declared_risk_from_verdicts(artifacts: TaskArtifacts) -> str:
    """Max declared risk over the rw1/rw2 verdict artifacts (single home).

    Both the VPS decision and the off-VPS merge check derive declared risk
    this way - never from the (untrusted) merge-decision.json - so a
    fabricated decision cannot launder a reviewer-declared high risk.
    Uses policy.RISK_ORDER; an unknown level is treated as high (fail
    safe), matching policy.effective_risk's default.
    """
    risk = "low"
    for name in (RW1_VERDICT, RW2_VERDICT):
        verdict = artifacts.read_json(name)
        if isinstance(verdict, dict):
            level = str(verdict.get("risk_level", "low"))
            if RISK_ORDER.get(level, 2) > RISK_ORDER.get(risk, 2):
                risk = level
    return risk


def _force_draft_status(spec_text: str) -> str:
    """Return the spec text with front-matter status forced to draft-proposal.

    Any investigator-supplied status is dropped; a leading front-matter
    block is preserved (minus its status line) so type/roles survive.
    """
    stripped = spec_text.lstrip("\n")
    if not stripped.startswith("---"):
        return f"---\nstatus: draft-proposal\n---\n{spec_text}"
    lines = stripped.splitlines()
    # find the closing '---' of the front-matter block
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if close is None:
        # malformed (no close) - wrap the whole thing as body under a fresh block
        return f"---\nstatus: draft-proposal\n---\n{spec_text}"
    fm = [ln for ln in lines[1:close] if not ln.lstrip().lower().startswith("status:")]
    body = lines[close + 1 :]
    rebuilt = ["---", "status: draft-proposal", *fm, "---", *body]
    return "\n".join(rebuilt) + "\n"


def _parse_investigator_payload(text: str) -> tuple[str, list[Any], Any]:
    """Typed parse of the investigator JSON: (report, findings, fix_spec).

    Raises VerdictError on any shape problem so request_payload's bounded
    retry (with the error fed back) applies uniformly.
    """
    try:
        payload = json.loads(_extract_json(text))
        return (
            str(payload["report"]),
            list(payload.get("findings", [])),
            payload.get("fix_spec"),
        )
    except (VerdictError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise VerdictError(f"investigator payload invalid: {exc}") from exc


def gate_states_from_log(
    entries: Sequence[Mapping[str, Any]],
    head_sha: str,
    *,
    require_rw2: bool = True,
    require_authoritative: bool = True,
) -> GateStates:
    """SHA-keyed gate results replayed from the append-only log.

    Single source for both the VPS loop and the off-VPS merge check -
    neither trusts a mutable state snapshot.
    """
    rw1_sha = rw2_sha = auth_sha = None
    rw1_ok = rw2_ok = auth_ok = auth_flaky = False
    for entry in entries:
        action = entry.get("action")
        if action == "rw1":
            rw1_sha, rw1_ok = entry.get("sha"), entry.get("outcome") == "approved"
        elif action == "rw2":
            rw2_sha, rw2_ok = entry.get("sha"), entry.get("outcome") == "go"
        elif action == "senior" and entry.get("outcome") == "approved":
            # a binding senior override stands in for the gate it resolved
            if not rw2_ok:
                rw2_sha, rw2_ok = entry.get("sha"), True
            if not rw1_ok:
                rw1_sha, rw1_ok = entry.get("sha"), True
        elif action == "authoritative":
            auth_sha = entry.get("sha")
            auth_ok = entry.get("outcome") == "pass"
            auth_flaky = bool(entry.get("flaky"))
    if not require_authoritative:
        auth_sha, auth_ok = head_sha, True
    if not require_rw2:
        rw2_sha, rw2_ok = head_sha, True
    return GateStates(
        head_sha=head_sha,
        rw1_sha=rw1_sha,
        rw1_approved=rw1_ok,
        rw2_sha=rw2_sha,
        rw2_go=rw2_ok,
        authoritative_sha=auth_sha,
        authoritative_passed=auth_ok,
        authoritative_flaky=auth_flaky,
    )


# --- Prompts ------------------------------------------------------------------

DEVELOPER_PROMPT = """\
{role}

Task ID: {task}
Spec file: {spec_rel}

The spec is the source of truth. Read it fully, including any
"## Clarifications" section.
{context}
Implement or fix the task by editing files in this working tree.
The orchestrator commits for you. Do not push. Never touch main.
Finish with a one-paragraph summary of what you changed and why.
"""

FAST_FAILURE_SECTION = """
## Fast test failure to fix

The previous change failed the fast test gate. Fix the failure:

```
{tail}
```
"""

AUTHORITATIVE_FAILURE_SECTION = """
## Authoritative gate failure to fix

The clean containerized run of the FULL suite failed on the current HEAD.
Fix the failure (no test skipping, no gate weakening):

```
{tail}
```
"""

RW1_VERDICT_SECTION = """
## Reviewer verdict to address (rw1, BINDING)

Address every blocker finding below, then re-check the whole change:

```json
{verdict}
```
"""

RW2_VERDICT_SECTION = """
## Guard reviewer finding to address (rw2, BINDING defect)

An independent cross-vendor guard found a defect. You MUST address every
blocker finding; advisory findings you MAY adopt at your own judgement:

```json
{verdict}
```
"""

SENIOR_VERDICT_SECTION = """
## Senior reviewer verdict to address (BINDING, final authority)

```json
{verdict}
```
"""

RW1_PROMPT = """\
{role}

Task ID: {task}
Spec file: {spec_rel}

Review the current branch (diff against origin/{base}) against the spec.
Verify claims with evidence from the actual code, not the developer's
summary. Output ONLY the verdict JSON object required by your role file -
no prose before or after it.
"""

RW2_PROMPT = """\
{role}

Task ID: {task}
Spec file: {spec_rel}

Adversarially review the current branch (diff against origin/{base}):
actively try to find a real defect. Approve if you cannot.
The latest rw1 verdict, for context (do not defer to it):

```json
{rw1_verdict}
```

Output ONLY the verdict JSON object required by your role file.
"""

RW2_GONOGO_PROMPT = """\
{role}

Task ID: {task}
Spec file: {spec_rel}

A rework round addressed your previous binding finding:

```json
{previous_verdict}
```

Issue go/nogo ONLY: verify whether YOUR finding was addressed on the
current branch. Do NOT open a fresh full review. Output ONLY the verdict
JSON object (APPROVED = go; a still-unaddressed finding stays a blocker).
"""

EXPLORATION_SECTION = """
## Exploration findings (from the explorer role)

```
{exploration}
```
"""

EXPLORER_PROMPT = """\
{role}

Task ID: {task}
Spec file: {spec_rel}

Scope the task before any code is written: explore the relevant code,
reproduce the bug if there is one, locate the root cause, and propose a
concrete approach. Do NOT change product code. Finish with a structured
summary (findings, affected files, proposed approach, risks).
"""

INVESTIGATOR_PROMPT = """\
{role}

Task ID: {task}
Spec file: {spec_rel}

This is a REPORT-ONLY task: the deliverable is a report, not code.
Investigate per the spec. Do NOT modify any source file and do NOT commit
failing tests - reproduction evidence (output, traceback) goes in the
report text.

Output ONLY a JSON object:
{{"report": "<full markdown report>",
  "findings": [{{"severity": "blocker|advisory", "category": "correctness|invariant|security|migration|test-adequacy|quality",
                 "file": "path", "line": 0, "summary": "...", "failure_scenario": "..."}}],
  "fix_spec": "<optional: complete draft spec markdown for the fix task, empty string if not applicable>"}}
"""

VERIFY_PROMPT = """\
{role}

Task ID: {task}
Spec file: {spec_rel}

Play devil's advocate over every proposed finding below: try to REFUTE it
against the real code. The top risk is a hallucinated finding or a
symptom-not-cause diagnosis. Drop every finding you cannot confirm with
evidence; keep only confirmed ones.

Proposed findings:
```json
{findings}
```

Output ONLY the standard verdict JSON object; its "findings" array is the
CONFIRMED subset (verbatim where confirmed), with your evidence in
claims_verified.
"""

SENIOR_PROMPT = """\
{role}

Task ID: {task}
Spec file: {spec_rel}

You are the escalation authority. The loop below did not converge or the
change is high-risk. Review the diff and both reviewer verdicts, then
issue a BINDING verdict: APPROVED overrides the disputed gate,
CHANGES_REQUESTED sends concrete blockers back to the developer.

rw1 verdict:
```json
{rw1_verdict}
```

rw2 verdict:
```json
{rw2_verdict}
```

Diff against origin/{base}:
```diff
{diff}
```

Output ONLY the verdict JSON object required by your role file.
"""


class Orchestrator:
    """Sequencing per design S5: developer <-> fast tests <-> rw1 -> rw2 ->
    authoritative gate -> push, with the S6 backstop around it.

    rw2/senior/authoritative collaborators are optional; when absent the
    corresponding step is skipped (Slice-1 composition).
    """

    def __init__(
        self,
        *,
        gitops: GitOps,
        dev_runner: AgentRunner,
        rw1_runner: AgentRunner,
        fast_commands: str,
        shell: ShellRunner,
        roles_dir: Path,
        max_loops: int = 4,
        rw2_runner: AgentRunner | None = None,
        senior_runner: AgentRunner | None = None,
        docker_gate: DockerGate | None = None,
        composition: Sequence[str] | None = None,
        policy_enabled: bool = False,
        notifier: NtfyNotifier | None = None,
        now: Any = None,
        quota_policy: QuotaPolicy | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.gitops = gitops
        self.dev_runner = dev_runner
        self.rw1_runner = rw1_runner
        self.rw2_runner = rw2_runner
        self.senior_runner = senior_runner
        self.docker_gate = docker_gate
        self.composition = tuple(composition) if composition is not None else None
        self.policy_enabled = policy_enabled
        self.notifier = notifier or NtfyNotifier(topic=None)
        self.fast_commands = fast_commands
        self.shell = shell
        self.roles_dir = roles_dir
        self.max_loops = max_loops
        self._now = now
        self._quota_policy = quota_policy
        if quota_policy is not None:
            kwargs: dict[str, Any] = {"policy": quota_policy, "sleep_fn": sleep_fn}
            if clock is not None:
                kwargs["now_fn"] = clock
            self.dev_runner = QuotaAwareRunner(self.dev_runner, **kwargs)
            self.rw1_runner = QuotaAwareRunner(self.rw1_runner, **kwargs)
            if self.rw2_runner is not None:
                self.rw2_runner = QuotaAwareRunner(self.rw2_runner, **kwargs)
            if self.senior_runner is not None:
                self.senior_runner = QuotaAwareRunner(self.senior_runner, **kwargs)

    def _has_role(self, role: str) -> bool:
        return self.composition is not None and role in self.composition

    # -- helpers ---------------------------------------------------------

    def _role(self, name: str) -> str:
        return (self.roles_dir / f"{name}.md").read_text(encoding="utf-8")

    def _artifacts(self, wt: Path, task_id: str) -> TaskArtifacts:
        if self._now is not None:
            return TaskArtifacts(wt, task_id, now=self._now)
        return TaskArtifacts(wt, task_id)

    def _dev_context(self, artifacts: TaskArtifacts) -> str:
        entries = artifacts.read_log()
        last_phase = next(
            (e for e in reversed(entries) if e.get("action") in _PHASE_ACTIONS), None
        )
        if last_phase is None:
            return ""
        action, outcome = last_phase["action"], last_phase["outcome"]
        if action == "fast_tests" and outcome == "fail":
            return FAST_FAILURE_SECTION.format(tail=last_phase.get("detail", ""))
        if action == "authoritative" and outcome == "fail":
            return AUTHORITATIVE_FAILURE_SECTION.format(tail=last_phase.get("detail", ""))
        if action == "rw1" and outcome == "changes_requested":
            verdict = artifacts.read_json(RW1_VERDICT)
            return RW1_VERDICT_SECTION.format(verdict=json.dumps(verdict, indent=2))
        if action == "rw2" and outcome == "nogo":
            verdict = artifacts.read_json(RW2_VERDICT)
            return RW2_VERDICT_SECTION.format(verdict=json.dumps(verdict, indent=2))
        if action == "senior" and outcome == "changes_requested":
            verdict = artifacts.read_json(SENIOR_VERDICT)
            return SENIOR_VERDICT_SECTION.format(verdict=json.dumps(verdict, indent=2))
        return ""

    def _declared_risk_high(self, artifacts: TaskArtifacts) -> bool:
        return declared_risk_from_verdicts(artifacts) == "high"

    def _override_phase(
        self, phase: str, artifacts: TaskArtifacts, wt: Path
    ) -> str:
        """Backstop + senior triggers (design S6) on top of pure derivation."""
        entries = artifacts.read_log()
        if self.senior_runner is None:
            return phase
        if phase == "developer" and nonconvergence_detected(entries):
            if senior_ran(entries):
                return "deadlock"
            return "senior"
        if phase in ("authoritative", "push") and not senior_ran(entries):
            # post-approval senior gate: high-risk change or test/invariant edits
            if self._declared_risk_high(artifacts) or touches_invariant_tests(
                load_target_policy(wt),
                self.gitops.changed_files(wt, artifacts.task_id),
            ):
                return "senior"
        return phase

    # -- run ---------------------------------------------------------------

    def run(self, task_id: str) -> str:
        wt = self.gitops.task_worktree(task_id)
        artifacts = self._artifacts(wt, task_id)
        spec_rel = f"{TARGET_DIR_NAME}/specs/{task_id}.md"
        # Idempotent resume: a run that already reached a terminal (or a
        # completed push) must not re-execute - that would re-fire the ntfy and
        # can push an empty report-only deliverable. Return the recorded state.
        recorded = _recorded_terminal(artifacts.read_log())
        if recorded is not None:
            return recorded
        self._bind_quota(task_id, artifacts)
        try:
            return self._run_phases(task_id, wt, artifacts, spec_rel)
        except QuotaTimeout:
            return self._finalize(task_id, wt, artifacts, "QUOTA_TIMEOUT")
        except Exception as exc:  # noqa: BLE001 - detached loop must never die silently
            return self._internal_error(task_id, wt, artifacts, exc)

    def _bind_quota(self, task_id: str, artifacts: TaskArtifacts) -> None:
        runners = [
            r
            for r in (self.dev_runner, self.rw1_runner, self.rw2_runner, self.senior_runner)
            if isinstance(r, QuotaAwareRunner)
        ]
        if not runners or self._quota_policy is None:
            return
        budget = QuotaBudget(self._quota_policy.max_wait)

        def on_wait(role: str, reset_at: datetime | None, wait: timedelta, attempt: int) -> None:
            artifacts.append_log(
                action="quota_exhausted",
                outcome="wait",
                role=role,
                reset_at=reset_at.strftime("%Y-%m-%dT%H:%M:%SZ") if reset_at else None,
                wait_seconds=int(wait.total_seconds()),
                attempt=attempt,
            )
            if attempt == 0:  # one notification per waiting episode, not per backoff
                self.notifier.notify(task_id, "QUOTA_WAIT")

        def on_resume(role: str, attempts: int) -> None:
            artifacts.append_log(
                action="quota_resumed", outcome="ok", role=role, attempts=attempts
            )

        for runner in runners:
            runner.bind(budget, on_wait, on_resume)

    def _run_phases(
        self, task_id: str, wt: Path, artifacts: TaskArtifacts, spec_rel: str
    ) -> str:
        if self._has_role("investigator"):
            return self._run_report_only(task_id, wt, artifacts, spec_rel)

        if self._has_role("explorer") and not any(
            e.get("action") == "explorer" for e in artifacts.read_log()
        ):
            self._run_explorer(task_id, wt, artifacts, spec_rel)

        while True:
            rp = derive_resume_point(
                artifacts.read_log(),
                self.max_loops,
                with_rw2=self.rw2_runner is not None,
                with_authoritative=self.docker_gate is not None,
            )
            phase = self._override_phase(rp.phase, artifacts, wt)

            if phase == "developer":
                self._run_developer(task_id, wt, artifacts, spec_rel, rp)
            elif phase == "fast_tests":
                self._run_fast_tests(task_id, wt, artifacts, rp)
            elif phase == "rw1":
                self._run_rw1(task_id, wt, artifacts, spec_rel, rp)
            elif phase == "rw2":
                self._run_rw2(task_id, wt, artifacts, spec_rel, rp)
            elif phase == "authoritative":
                self._run_authoritative(task_id, wt, artifacts, rp)
            elif phase == "senior":
                self._run_senior(task_id, wt, artifacts, spec_rel, rp)
            elif phase == "push":
                terminal = "PUSHED"
                if self.policy_enabled:
                    decision = self._decide_merge(task_id, wt, artifacts)
                    artifacts.write_json(MERGE_DECISION, decision.to_json())
                    terminal = f"MERGE_DECIDED:{decision.decision}"
                return self._finalize(
                    task_id, wt, artifacts, terminal, push_round=rp.round
                )
            elif phase == "cap_reached":
                return self._finalize(task_id, wt, artifacts, "CAP_REACHED")
            elif phase == "deadlock":
                return self._finalize(task_id, wt, artifacts, "ESCALATED_DEADLOCK")
            elif phase == "done":
                # Crash-window / legacy resume: the push-ok entry exists (the
                # deliverable push completed and the ntfy fired) but the sticky
                # marker or its push never landed. Re-record and re-push so the
                # next kickoff short-circuits and the hub holds the full log.
                # No re-notify: reaching "done" implies the notify already ran.
                decision_json = artifacts.read_json(MERGE_DECISION)
                state = (
                    f"MERGE_DECIDED:{decision_json['decision']}"
                    if isinstance(decision_json, dict) and decision_json.get("decision")
                    else "PUSHED"
                )
                artifacts.append_log(action="terminal", outcome=state)
                self.gitops.commit_all(wt, f"Record terminal {state} for {task_id}")
                self.gitops.push(wt, task_id)
                return state
            else:  # pragma: no cover - defensive
                raise RuntimeError(f"unknown phase: {phase}")

    # -- terminal + policy helpers -------------------------------------------

    def _finalize(
        self,
        task_id: str,
        wt: Path,
        artifacts: TaskArtifacts,
        state: str,
        *,
        push_round: int | None = None,
    ) -> str:
        """Single terminal tail for every outcome (M2). Order is load-bearing:

        artifacts -> commit -> push -> notify -> marker -> commit -> push

        The deliverable/handback push happens BEFORE the ntfy (the message
        says "see handback" - it must already be on the hub), and the sticky
        marker is recorded LAST, after the notify: a crash anywhere in the
        tail leaves a retryable run whose re-kickoff redoes the tail
        idempotently instead of stranding it. The notify is therefore
        at-least-once - a crash inside the marker window repeats it on
        resume; a duplicate notification is acceptable, a lost one is not.
        The final commit+push lands the marker on the hub so a REBUILT VPS
        short-circuits instead of re-running (and re-notifying) a finished
        task. What each state pushes/writes comes from terminals.py -
        notably PATH_GUARD_VIOLATION never pushes its violating tree.
        """
        spec = terminal_spec(state)
        write_human_summary(artifacts, state, base_branch=self.gitops.default_branch)
        if spec.handback:
            write_handback(artifacts, state, base_branch=self.gitops.default_branch)
        self.gitops.commit_all(wt, f"{state} for {task_id}")
        if spec.push:
            self.gitops.push(wt, task_id)
        self.notifier.notify(task_id, state)
        if spec.kind == "success":
            extra: dict[str, Any] = {"round": push_round} if push_round is not None else {}
            artifacts.append_log(action="push", outcome="ok", **extra)
        artifacts.append_log(action="terminal", outcome=state)
        self.gitops.commit_all(wt, f"Record terminal {state} for {task_id}")
        if spec.push:
            self.gitops.push(wt, task_id)
        return state

    def _internal_error(
        self, task_id: str, wt: Path, artifacts: TaskArtifacts, exc: BaseException
    ) -> str:
        """Catch-all terminal for an unexpected exception in a DETACHED run.

        Without this, any non-quota exception (most realistically a ``GitError``
        from ``gitops.push`` hitting branch protection / a network blip, which
        runs AFTER the work and BEFORE the ntfy) would propagate out of the
        detached process to a traceback in a log nobody watches: the converged
        overnight task vanishes with zero signal. So record a terminal
        INTERNAL_ERROR, best-effort write the handback, and fire exactly ONE
        ntfy. Every artifact/git side effect is guarded independently so a
        broken tree still lets the notify fire (the load-bearing guarantee).
        """
        state = "INTERNAL_ERROR"
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        steps = (
            lambda: artifacts.append_log(action="terminal", outcome=state, detail=detail),
            lambda: write_human_summary(
                artifacts, state, base_branch=self.gitops.default_branch
            ),
            lambda: write_handback(artifacts, state, base_branch=self.gitops.default_branch),
            lambda: self.gitops.commit_all(
                wt, f"{state} for {task_id}; handback written"
            ),
        )
        for step in steps:
            try:
                step()
            except Exception:  # noqa: BLE001 - never let cleanup block the notify
                pass
        self.notifier.notify(task_id, state)
        return state

    def _gate_states(self, artifacts: TaskArtifacts, head_sha: str) -> GateStates:
        return gate_states_from_log(
            artifacts.read_log(),
            head_sha,
            require_rw2=self.rw2_runner is not None,
            require_authoritative=self.docker_gate is not None,
        )

    def _decide_merge(
        self, task_id: str, wt: Path, artifacts: TaskArtifacts
    ) -> MergeDecision:
        head_sha = self.gitops.code_sha(wt, task_id)
        changed = self.gitops.changed_files(wt, task_id)
        # The DECISION always requires all three gates (S8 auto-merge
        # precondition) - a composition that skipped rw2 or the docker gate
        # can push, but never auto-merge.
        gates = gate_states_from_log(artifacts.read_log(), head_sha)
        artifacts.write_json(
            STATE,
            {
                "head_sha": head_sha,
                "phase": "merge_decision",
                "rw1": {"sha": gates.rw1_sha, "approved": gates.rw1_approved},
                "rw2": {"sha": gates.rw2_sha, "go": gates.rw2_go},
                "authoritative": {
                    "sha": gates.authoritative_sha,
                    "passed": gates.authoritative_passed,
                    "flaky": gates.authoritative_flaky,
                },
            },
        )

        def _read_wt(path: str) -> str:
            return (wt / path).read_text(encoding="utf-8")

        entries = artifacts.read_log()
        deadlock = any(
            e.get("action") == "senior" and e.get("outcome") == "deadlock" for e in entries
        )
        return merge_decision(
            policy=load_target_policy(wt),
            changed_files=changed,
            diff_lines=self.gitops.diff_line_count(wt, task_id),
            declared_risk=declared_risk_from_verdicts(artifacts),
            gates=gates,
            changed_statuses=self.gitops.changed_statuses(wt, task_id),
            migration_texts=_read_wt,
            senior_deadlock=deadlock,
        )

    # -- phase handlers ------------------------------------------------------

    def _run_developer(
        self,
        task_id: str,
        wt: Path,
        artifacts: TaskArtifacts,
        spec_rel: str,
        rp: ResumePoint,
    ) -> None:
        entries = artifacts.read_log()
        last_phase = next(
            (e for e in reversed(entries) if e.get("action") in _PHASE_ACTIONS), None
        )
        # Debugger lens (design S3): after a test failure the fix round is
        # driven by the debugger prompt over the SAME dev session - a lens,
        # not a new role session.
        fixing_test_failure = last_phase is not None and (
            last_phase["action"] in ("fast_tests", "authoritative")
            and last_phase["outcome"] == "fail"
        )
        role_name = "debugger" if self._has_role("debugger") and fixing_test_failure else "developer"

        context = self._dev_context(artifacts)
        exploration = artifacts.read_text(EXPLORATION)
        if exploration and rp.round == 1:
            context = EXPLORATION_SECTION.format(exploration=exploration) + context

        prompt = DEVELOPER_PROMPT.format(
            role=self._role(role_name),
            task=task_id,
            spec_rel=spec_rel,
            context=context,
        )
        result = self.dev_runner.run(prompt, wt, resume=rp.dev_session)
        artifacts.append_log(
            action="developer",
            outcome=result.exit_reason,
            round=rp.round,
            session_id=result.session_id,
            role=role_name,
            detail=result.text[:2000],
        )
        self.gitops.commit_all(wt, f"Round {rp.round}: developer changes for {task_id}")

    def run_explorer(self, task_id: str) -> str:
        """Run the explorer once (foreground design gate) and return the
        exploration text. Idempotent: if it already ran, returns the stored
        EXPLORATION artifact without re-running."""
        wt = self.gitops.task_worktree(task_id)
        artifacts = self._artifacts(wt, task_id)
        spec_rel = f"{TARGET_DIR_NAME}/specs/{task_id}.md"
        if not any(e.get("action") == "explorer" for e in artifacts.read_log()):
            self._run_explorer(task_id, wt, artifacts, spec_rel)
        return artifacts.read_text(EXPLORATION) or ""

    def _run_explorer(
        self, task_id: str, wt: Path, artifacts: TaskArtifacts, spec_rel: str
    ) -> None:
        prompt = EXPLORER_PROMPT.format(
            role=self._role("explorer"), task=task_id, spec_rel=spec_rel
        )
        try:
            # identity parse: only the fail-closed exit_reason check + bounded
            # retry apply - the text of a crashed run must never become the
            # exploration artifact (request_payload never parses non-ok output)
            text, result = request_payload(self.dev_runner, prompt, wt, lambda t: t)
        except VerdictError as exc:
            # exploration is advisory context, not a gate: record the failure
            # and let the loop proceed to the developer without it
            artifacts.append_log(
                action="explorer", outcome="error", detail=str(exc)[:2000]
            )
            self.gitops.commit_all(wt, f"Exploration failed for {task_id}")
            return
        artifacts.write_text(EXPLORATION, text)
        artifacts.append_log(
            action="explorer",
            outcome=result.exit_reason,
            session_id=result.session_id,
            detail=result.text[:2000],
        )
        self.gitops.commit_all(wt, f"Exploration for {task_id}")

    # -- report-only flow (design S3/S5: audit / investigate) ----------------

    def _run_report_only(
        self, task_id: str, wt: Path, artifacts: TaskArtifacts, spec_rel: str
    ) -> str:
        entries = artifacts.read_log()

        if not any(e.get("action") == "investigator" for e in entries):
            prompt = INVESTIGATOR_PROMPT.format(
                role=self._role("investigator"), task=task_id, spec_rel=spec_rel
            )
            try:
                # same fail-closed retry engine as request_verdict: a crashed
                # run's text is never parsed, a malformed payload is fed back
                # for a bounded retry - one transient CLI failure must not
                # become a sticky INVESTIGATOR_MALFORMED terminal
                (report, proposed, fix_spec), result = request_payload(
                    self.dev_runner, prompt, wt, _parse_investigator_payload
                )
            except VerdictError as exc:
                artifacts.append_log(
                    action="investigator", outcome="malformed", detail=str(exc)[:2000]
                )
                return self._finalize(task_id, wt, artifacts, "INVESTIGATOR_MALFORMED")
            artifacts.write_text(REPORT, report)
            artifacts.write_json(FINDINGS_PROPOSED, proposed)
            if isinstance(fix_spec, str) and fix_spec.strip():
                self._write_fix_spec(wt, task_id, fix_spec)
            artifacts.append_log(
                action="investigator",
                outcome=result.exit_reason,
                session_id=result.session_id,
                findings=len(proposed),
            )
            self.gitops.commit_all(wt, f"Investigator report for {task_id}")
            entries = artifacts.read_log()

        if not any(e.get("action") == "verify" for e in entries):
            proposed = artifacts.read_json(FINDINGS_PROPOSED) or []
            prompt = VERIFY_PROMPT.format(
                role=self._role("verify"),
                task=task_id,
                spec_rel=spec_rel,
                findings=json.dumps(proposed, indent=2),
            )
            try:
                # validate=None: confirmed findings (blockers included) live
                # inside an APPROVED report verdict here (see validate_review)
                verdict, result = request_verdict(self.rw1_runner, prompt, wt, validate=None)
            except VerdictError as exc:
                artifacts.append_log(
                    action="verify", outcome="malformed", detail=str(exc)[:2000]
                )
                return self._finalize(task_id, wt, artifacts, "VERIFY_MALFORMED")
            confirmed = [dataclasses.asdict(f) for f in verdict.findings]
            artifacts.write_json(FINDINGS, confirmed)
            artifacts.append_log(
                action="verify",
                outcome="ok",
                session_id=result.session_id,
                confirmed=len(confirmed),
                proposed=len(artifacts.read_json(FINDINGS_PROPOSED) or []),
                # bind the verify to the exact report/findings content it blessed
                content_sha=report_content_sha(artifacts),
            )
            self.gitops.commit_all(wt, f"Verified findings for {task_id}")

        ok, offending = path_guard(task_id, self.gitops.changed_files(wt, task_id))
        if not ok:
            artifacts.append_log(
                action="path_guard", outcome="violation", detail="; ".join(offending)[:2000]
            )
            return self._finalize(task_id, wt, artifacts, "PATH_GUARD_VIOLATION")

        terminal = "PUSHED"
        if self.policy_enabled:
            changed_now = self.gitops.changed_files(wt, task_id)
            decision = report_only_decision(
                task_id=task_id,
                changed_files=changed_now,
                verify_confirmed=report_verify_confirmed(artifacts),
                nondraft_specs=nondraft_report_specs(
                    task_id, changed_now, lambda p: (wt / p).read_text(encoding="utf-8")
                ),
            )
            artifacts.write_json(MERGE_DECISION, decision.to_json())
            terminal = f"MERGE_DECIDED:{decision.decision}"
        return self._finalize(task_id, wt, artifacts, terminal)

    def _write_fix_spec(self, wt: Path, task_id: str, fix_spec: str) -> None:
        """Fix proposal is a draft, never a runnable spec (design S3).

        The investigator's status (if any) is force-overwritten to
        draft-proposal - an emitted ``status: ready`` must NOT slip a
        runnable spec past the kickoff draft gate.
        """
        path = wt / TARGET_DIR_NAME / "specs" / f"{task_id}-fix.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_force_draft_status(fix_spec), encoding="utf-8", newline="\n")

    def _run_fast_tests(
        self, task_id: str, wt: Path, artifacts: TaskArtifacts, rp: ResumePoint
    ) -> None:
        result = run_fast(self.fast_commands, wt, self.shell)
        artifacts.append_log(
            action="fast_tests",
            outcome="pass" if result.passed else "fail",
            round=rp.round,
            detail=result.output_tail[-4000:],
        )
        self.gitops.commit_all(
            wt,
            f"Round {rp.round}: fast tests "
            f"{'passed' if result.passed else 'failed'} for {task_id}",
        )

    def _run_rw1(
        self,
        task_id: str,
        wt: Path,
        artifacts: TaskArtifacts,
        spec_rel: str,
        rp: ResumePoint,
    ) -> None:
        prompt = RW1_PROMPT.format(
            role=self._role("rw1"),
            task=task_id,
            spec_rel=spec_rel,
            base=self.gitops.default_branch,
        )
        try:
            verdict, result = request_verdict(
                self.rw1_runner, prompt, wt, resume=rp.rw1_session
            )
        except VerdictError as exc:
            artifacts.append_log(
                action="rw1", outcome="malformed", round=rp.round, detail=str(exc)[:2000]
            )
            self.gitops.commit_all(
                wt, f"Round {rp.round}: rw1 verdict malformed for {task_id}"
            )
            return
        artifacts.write_json(RW1_VERDICT, dataclasses.asdict(verdict))
        artifacts.append_log(
            action="rw1",
            outcome="approved" if verdict.approved else "changes_requested",
            round=rp.round,
            session_id=result.session_id,
            sha=self.gitops.code_sha(wt, task_id),
            detail="; ".join(f.summary for f in verdict.blockers)[:2000],
        )
        self.gitops.commit_all(wt, f"Round {rp.round}: rw1 review for {task_id}")

    def _run_rw2(
        self,
        task_id: str,
        wt: Path,
        artifacts: TaskArtifacts,
        spec_rel: str,
        rp: ResumePoint,
    ) -> None:
        assert self.rw2_runner is not None
        entries = artifacts.read_log()
        # Narrow go/nogo re-review is correct ONLY when the immediately prior
        # rw2 verdict was a nogo with a stored blocker verdict to re-verify.
        # After a "go" (then an authoritative failure + rework) or a malformed
        # rw2 (no verdict written), the code is materially different / never
        # reviewed, so a fresh adversarial review is required - not a
        # rubber-stamp of a stale or null "previous finding".
        last_rw2 = next(
            (e for e in reversed(entries) if e.get("action") == "rw2"), None
        )
        stored_verdict = artifacts.read_json(RW2_VERDICT)
        gonogo = (
            last_rw2 is not None
            and last_rw2.get("outcome") == "nogo"
            and isinstance(stored_verdict, dict)
        )
        if gonogo:
            prompt = RW2_GONOGO_PROMPT.format(
                role=self._role("rw2"),
                task=task_id,
                spec_rel=spec_rel,
                previous_verdict=json.dumps(stored_verdict, indent=2),
            )
        else:
            prompt = RW2_PROMPT.format(
                role=self._role("rw2"),
                task=task_id,
                spec_rel=spec_rel,
                base=self.gitops.default_branch,
                rw1_verdict=json.dumps(artifacts.read_json(RW1_VERDICT), indent=2),
            )
        try:
            verdict, result = request_verdict(
                self.rw2_runner, prompt, wt, resume=rp.rw2_session, validate=validate_rw2
            )
        except VerdictError as exc:
            artifacts.append_log(
                action="rw2", outcome="malformed", round=rp.round, detail=str(exc)[:2000]
            )
            self.gitops.commit_all(
                wt, f"Round {rp.round}: rw2 verdict malformed for {task_id}"
            )
            return
        artifacts.write_json(RW2_VERDICT, dataclasses.asdict(verdict))
        # guard semantics: only binding defects block; advisory never does
        nogo = bool(verdict.blockers)
        artifacts.append_log(
            action="rw2",
            outcome="nogo" if nogo else "go",
            round=rp.round,
            session_id=result.session_id,
            sha=self.gitops.code_sha(wt, task_id),
            fingerprint=verdict_fingerprint(verdict) if nogo else None,
            detail="; ".join(f.summary for f in verdict.blockers)[:2000],
        )
        self.gitops.commit_all(wt, f"Round {rp.round}: rw2 guard review for {task_id}")

    def _run_authoritative(
        self, task_id: str, wt: Path, artifacts: TaskArtifacts, rp: ResumePoint
    ) -> None:
        assert self.docker_gate is not None
        sha = self.gitops.code_sha(wt, task_id)
        include_frontend = frontend_touched(
            self.gitops.changed_files(wt, task_id),
            load_target_policy(wt).frontend_prefixes,
        )
        result = self.docker_gate.run(wt, sha, include_frontend)
        # flaky = this exact SHA failed the gate before and passes now (S7)
        prior_fail = any(
            e.get("action") == "authoritative"
            and e.get("sha") == sha
            and e.get("outcome") == "fail"
            for e in artifacts.read_log()
        )
        flaky = result.passed and prior_fail
        payload = result.to_json()
        payload["flaky"] = flaky
        artifacts.write_json(AUTHORITATIVE, payload)
        artifacts.append_log(
            action="authoritative",
            outcome="pass" if result.passed else "fail",
            round=rp.round,
            sha=sha,
            flaky=flaky,
            fingerprint=failure_fp(result.output_tail) if not result.passed else None,
            detail=result.output_tail[-4000:] if not result.passed else "",
        )
        self.gitops.commit_all(
            wt,
            f"Round {rp.round}: authoritative gate "
            f"{'passed' if result.passed else 'failed'} for {task_id}",
        )

    def _run_senior(
        self,
        task_id: str,
        wt: Path,
        artifacts: TaskArtifacts,
        spec_rel: str,
        rp: ResumePoint,
    ) -> None:
        assert self.senior_runner is not None
        prompt = SENIOR_PROMPT.format(
            role=self._role("senior-reviewer"),
            task=task_id,
            spec_rel=spec_rel,
            base=self.gitops.default_branch,
            rw1_verdict=json.dumps(artifacts.read_json(RW1_VERDICT), indent=2),
            rw2_verdict=json.dumps(artifacts.read_json(RW2_VERDICT), indent=2),
            diff=self.gitops.diff_text(wt)[:20000],
        )
        try:
            verdict, result = request_verdict(self.senior_runner, prompt, wt)
        except VerdictError as exc:
            artifacts.append_log(
                action="senior", outcome="deadlock", round=rp.round, detail=str(exc)[:2000]
            )
            self.gitops.commit_all(wt, f"Senior escalation deadlock for {task_id}")
            return
        artifacts.write_json(SENIOR_VERDICT, dataclasses.asdict(verdict))
        artifacts.append_log(
            action="senior",
            outcome="approved" if verdict.approved else "changes_requested",
            round=rp.round,
            session_id=result.session_id,
            sha=self.gitops.code_sha(wt, task_id),
            detail="; ".join(f.summary for f in verdict.blockers)[:2000],
        )
        self.gitops.commit_all(wt, f"Senior escalation verdict for {task_id}")
