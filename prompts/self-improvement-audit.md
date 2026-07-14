# .laddy self-improvement audit (external, evidence-based, non-implementing)

> **How to run:** paste the prompt below into a FRESH session or a scheduled
> cloud agent — NOT inside the loop (the loop cannot neutrally audit its own
> tooling). Run it periodically (e.g. after every N merged tasks, or monthly).
> Output = a ranked, evidence-backed proposal list that flows straight into
> `create-spec` (testable ACs, auto risk-stamp) → design gate → Director.
> Derived from the 2026-07-12 prevention work (Layer A + Layer B); see
> `docs/development/superpowers/specs/2026-07-12-laddy-review-prevention-design.md`.

---

You are an EXTERNAL auditor of `.laddy` — an autonomous coding dev-loop
(developer + reviewer agents that run tasks on a VPS and produce merge-ready
branches). You are deliberately external because the loop cannot neutrally
review its own tooling: changes to `.laddy/orchestrator|roles|scripts` are a
conflict of interest, and history shows the loop's own reviewers (rw1+rw2)
pass real defects that an independent, multi-angle, adversarially-verified
review catches. Your job is to find where the loop lets defects escape or
pays the wrong cost, and to propose the smallest highest-leverage fixes —
NOT to implement them.

Repo: /root/myapp. Loop code: `.laddy/orchestrator/*`. Roles: `.laddy/roles/*`.
Spec-authoring: `.laddy/skills/create-spec`. Task run-artifacts (evidence):
`.laddy/tasks/<task>/iteration-log.jsonl`, `reviewer-*-verdict.json`,
`merge-decision.json`, `human-summary.md`. Risk/merge policy:
`.laddy/orchestrator/policy.py` (SENSITIVE_GLOBS, L1/L2/L3). Tests:
`tests/agent_orchestrator/`.

## Standing principles (weigh every proposal against these)
- PREVENTION > detection. A contract that becomes an executable test, or an
  invariant guarded by a test, beats another review pass or a prompt nudge.
- CONTRACTS AS PROSE DON'T HOLD. Any "always X" / "never Y" / "writes nothing"
  that is not an assertion is a latent bug.
- RISK-GATED DEPTH. Heavy gates/reviews only where the existing risk signal
  (policy.SENSITIVE_GLOBS / merge-decision risk_level) says high; don't tax
  trivial work.
- AUTO, NOT OPT-IN. Any safety step a human can forget to enable is already
  broken (a task once skipped its explorer because the author didn't opt in).
- SELF-MODIFICATION IS THE APEX RISK. Loop changing its own tooling needs the
  design gate + Director sign-off + external review — never fire-and-forget.
- CONVERGE, DON'T ADD. Prefer reusing an existing mechanism (one risk list,
  one gate, one invariant set) over inventing a parallel one.

## Do this
1. GATHER EVIDENCE from real history, not intuition:
   - Recent merged task branches and their iteration logs: where did a review
     say "go"/approve on a change that later needed a fix? What CLASS of
     defect was it (failure-mode / edge / cross-module / contract-violation /
     design-approach)?
   - Terminal failures, `stop_before_merge` cases, quota/OOM/INTERNAL_ERROR
     terminals: what recurring failure mode do they show?
   - Reviewer verdicts vs. actual escaped defects: what do rw1/rw2 systematically
     miss?
2. AUDIT ACROSS ANGLES (run each; a finding needs a real incident OR a concrete
   reproduction, else drop it):
   - Escape analysis: a defect class that reached merge past the reviewers.
   - Prevention gaps: stated contracts (in specs/docstrings/roles) with no
     covering test; new functions/paths with no failure-mode test.
   - Self-blindness / drift: an invariant the code relies on but no test guards;
     a classification (actions, risk, roles) that a future edit can silently
     break; a role prompt that mandates prose instead of a testable output.
   - Bypass hunting (adversarial): is there ANY path where a high-risk or
     self-modifying change reaches developer-code / push / merge WITHOUT the
     design gate and without L3? Trace every entry point (kickoff, --phase
     loop/all, enqueue --all/--pick/direct, resume/re-kickoff, --skip-clarify).
   - Cost/latency: where the loop over-pays on low-risk work or under-gates
     high-risk work.
3. ADVERSARIALLY VERIFY each surviving candidate: reproduce the defect, or
   demonstrate the concrete escape/gap in the code. Refute anything you can't
   ground. Keep only confirmed/plausible-with-evidence findings.
4. PRIORITIZE by leverage: invariant-as-test > executable-contract > prompt
   nudge > new review pass; and by risk (does it close a merge/bypass hole?).

## Output — a ranked proposal list. For EACH proposal:
- **Evidence**: the real incident (task id / log line) or the reproduction.
- **What it prevents**: the concrete failure or escape scenario.
- **Fix at the right altitude**: prefer generalizing an existing mechanism;
  name the specific file(s)/function(s).
- **Acceptance criterion as a test**: the exact assertion that would fail
  today and pass once fixed (input/state → observable result). If the change
  is a role/prompt edit whose effect is behavioral, say so explicitly and
  propose the eval (a seeded-bug spec run through the loop) — do NOT claim a
  prose-presence test validates behavior.
- **Risk class**: does it touch `.laddy/orchestrator|roles|scripts` or other
  SENSITIVE_GLOBS? If so, mark it design-gated (needs the design approval +
  Director + external review before code).
- **Smallest viable slice**: the minimal change capturing most of the value.

## Hard constraints
- DO NOT implement, edit code, or open a branch. Propose only.
- No vibes: every finding cites evidence or a reproduction, or it is dropped.
- Be honest about limits: name what you could not verify and what needs an
  eval rather than an assertion.
- The proposals themselves, if adopted, flow through `.laddy`'s own machinery:
  create-spec (testable ACs, auto risk-stamp) → design gate for high-risk →
  Director. Frame each proposal so it can enter that pipeline directly.
