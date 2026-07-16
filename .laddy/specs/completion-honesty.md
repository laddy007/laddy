---
type: feature
roles: [developer, rw1, rw2]
risk: medium
status: draft-proposal
---
# completion-honesty — borrow verified-completion, evidence receipts, and test-honesty gates

> **Umbrella design spec — not directly runnable.** Marked `draft-proposal` so
> `kickoff completion-honesty` refuses it: this is 3 independent slices (S1–S3),
> each promoted to its own runnable spec. See the Slices section.

## Goal
Close three "the agent said it was done" honesty gaps in the loop by
re-implementing — **under laddy's own control, as native gates, not as an
external dependency** — a small set of patterns proven in maintained,
subscription-compatible autonomous-SDLC tools (loki-mode's verified-completion
gate, evidence receipt, and test-mutation detection; Dex's "refuses empty diffs"
finalize step). Each is a deterministic, testable check that makes a false or
gamed completion **fail closed** instead of sailing into review. The trust model
is untouched: these run in the existing VPS loop and are re-derived by the local
trusted gate exactly like every other check.

## Motivation (why now)
A survey of subscription-compatible autonomous coders (OpenHands, Plandex,
loki-mode, Dex — all of which ride the `claude` CLI subscription the way laddy
already does) found **nothing that replaces laddy's trusted-merge layer**, but
several ship completion-honesty gates laddy lacks. Three are worth owning
outright rather than importing:

1. **No empty/no-op completion guard.** `_run_developer` (`orchestrator/loop.py`
   ~896) records the runner's `exit_reason` and commits; the transition
   `("developer", "ok"): "fast_tests"` (`loop.py` ~142) then fires
   *unconditionally*. A developer round that returns `ok` with an empty or
   trivial diff advances straight into review — the loop trusts the claim, not
   the artifact.
2. **The handoff bundle asserts, it doesn't evidence.** `build_handback` /
   `build_summary` (`orchestrator/handoff.py`) list rounds + the latest rw1/rw2
   verdict + the last failure tail, but never map the spec's numbered acceptance
   criteria to the evidence that each was met. The Director reads "APPROVED", not
   "criterion 3 ← authoritative gate green + rw2 claims_verified".
3. **Test-gaming is only partly caught.** The gate has diff-cover (≥90% patch
   coverage) and escalates edits to *policy-designated invariant* tests to senior
   (`touches_invariant_tests`). Neither catches the general gaming shapes:
   deleting or weakening an existing test, or adding a "test" with no assertion —
   a green suite that proves nothing.

None of these needs an external tool; each is a self-contained laddy gate.

## Scope
In:
- **S1**: a deterministic completion guard between `developer:ok` and
  `fast_tests` in the loop state machine (`orchestrator/loop.py`) + tests.
- **S2**: an acceptance-criteria → evidence receipt rendered into the handoff
  bundle (`orchestrator/handoff.py`, reusing the spec's numbered criteria via
  `orchestrator/spec.py` + the verdict `claims_verified`/`test_assessment`
  fields + gate outcomes from the log) + tests.
- **S3**: a deterministic test-honesty check (removed/weakened existing tests;
  assertion-free new tests) wired as a gate signal that **routes to senior
  review** rather than silently passing + tests.

Out:
- any change to the trust boundary, the merge authority, or the "VPS never
  writes main" invariant;
- any new external dependency, or runtime coupling to loki-mode/Dex — the
  patterns are **re-implemented under our control, not imported** (the surveyed
  tools are BUSL/solo-maintainer; we take the idea, not the dependency);
- LLM-judgment reworks of the review prompts — an anti-sycophancy/devil's-
  advocate lens for rw2 is a possible follow-up (see Notes), **not a slice**: it
  is not cleanly testable and rw2 is already the adversarial guard.

## Slices (promote one at a time; do NOT run this umbrella)

### S1 — verified-completion guard (empty/no-op diff fails closed)
**Root cause.** `developer:ok → fast_tests` is unconditional; the loop never
checks the developer actually changed anything.

**Behaviour.**
- On a `developer` round returning `ok`, before transitioning to `fast_tests`,
  compute the branch's **net diff vs the task base** (merge-base with
  `origin/main`) using the existing gitops helpers (`changed_files` /
  `diff_line_count`). An `ok` with an **empty net diff** is not a completion.
- A false completion routes **back to `developer`** with an explicit
  "you reported done but produced no change vs base" message, bounded by the
  **existing round cap** — so repeated no-ops hit `cap_reached` and hand off via
  `handback.md`, never loop forever. Reuse the current cap/convergence
  machinery; add no new terminal unless a distinct `NO_PROGRESS` reads cleaner.
- Applies to **code tasks only** (`feature`/`fix`/`spike`). The report-only flow
  (`_run_report_only`, `audit`/`investigate`) legitimately produces a report and
  no code diff — it is explicitly exempt.

**Acceptance criteria.**
1. `developer:ok` with an **empty net diff vs base** does **not** transition to
   `fast_tests`; it routes back to `developer` (loop-state test mirroring the
   existing transition tests, with a fake gitops reporting no changed files).
2. `developer:ok` with a **non-empty diff** transitions to `fast_tests` exactly
   as today — asserted, so the happy path does not regress.
3. Repeated no-op completions terminate at the round cap with a handback, never
   bounce indefinitely — asserted with a fake that always returns an empty diff.
4. Report-only tasks (`audit`/`investigate`) are unaffected by the guard —
   asserted by a report-only path test.
5. Suite green: `ruff`, `basedpyright`, `pytest`.

### S2 — acceptance-criteria evidence receipt in the handoff bundle
**Root cause.** The handback summarizes *rounds and verdicts*, not
*criteria and their evidence*.

**Behaviour.**
- Parse the spec's `## Acceptance criteria` numbered list (extend
  `orchestrator/spec.py`, which already parses front matter).
- Render an **Evidence** section into `build_handback` (and `build_summary`):
  one row per criterion showing (a) the deterministic gate outcomes that bear on
  it (`fast_tests`/`authoritative`/binding gate pass/fail from the log) and
  (b) any reviewer `claims_verified` / `test_assessment` entries from the latest
  rw1/rw2 verdict. A criterion with no explicit evidence is marked
  `unverified — see verdict`, never silently omitted.
- Degrade gracefully on a missing/malformed criteria section or corrupt verdict
  (match the defensive style of `_verdict_line`): the receipt must never crash
  the artifact that summarizes a failed run.
- Code tasks only; report-only tasks keep their existing `report.md`.

**Acceptance criteria.**
1. Given a spec with N numbered acceptance criteria and a run log, `build_handback`
   emits an Evidence section with **exactly N rows**, one per criterion.
2. Each row shows the relevant deterministic gate status; a criterion named in a
   verdict's `claims_verified` is marked verified **with its source** (rw1/rw2).
3. A criterion with no evidence renders `unverified — see verdict` (not dropped).
4. A spec with **no** `## Acceptance criteria` section, or a corrupt verdict
   file, produces a valid handback with a graceful placeholder (no exception) —
   asserted by malformed-input tests.
5. Suite green: `ruff`, `basedpyright`, `pytest`.

### S3 — deterministic test-honesty check
**Root cause.** diff-cover + `touches_invariant_tests` catch coverage and
invariant-test edits, but not the general gaming shapes across all tests.

**Behaviour.**
- Deterministic check over the branch diff vs base for the two gaming shapes:
  (a) an **existing** test function removed, or its assertions net-removed;
  (b) a **new/modified** test function whose body contains **no assertion**
  (`assert`, `pytest.raises`, `self.assert*`, `unittest` assertions).
- **Python/pytest first**, via the stdlib `ast` module (no new dependency); test
  paths come from the target policy. Non-Python test files are **out of scope in
  this slice** (documented no-op, not a crash) — a later slice can add languages.
- Wire the signal into the **senior escalation** path alongside
  `touches_invariant_tests`: it **routes to human/senior judgment, does not
  hard-block** (legitimate refactors delete tests) — but it is logged and can
  never be a silent pass. Surfaces as a `test-adequacy` finding.

**Acceptance criteria.**
1. A diff that **removes an existing test function** trips the check and routes
   the task to `senior` — fixture reintroduces the anti-pattern, asserts the
   escalation.
2. A diff that **adds an assertion-free test function** trips the check —
   asserted by fixture.
3. A legitimate diff (adds asserting tests, deletes nothing) does **not** trip
   the check — **no false positive** — asserted by fixture.
4. A non-Python test change neither trips nor crashes the check (documented
   scope) — asserted.
5. Suite green: `ruff`, `basedpyright`, `pytest`.

## Suggested order
- **S1** first — smallest, highest value, purely deterministic loop-state change.
- **S2** — additive artifact rendering, no control-flow risk.
- **S3** — most complex (AST + escalation wiring); may itself be sliced by
  language. Do it last.

## Notes
- **Provenance, deliberately shallow.** S1 = loki-mode "verified-completion" +
  Dex "refuses empty diffs"; S2 = loki-mode "evidence receipt"; S3 = loki-mode
  "test-mutation/mock detection". We borrow the *idea* and re-implement it in
  laddy's own idiom. We take **no code and no runtime dependency** — the sources
  are BUSL-1.1 / solo-maintainer, and the whole point of laddy is not to trust
  code that runs the loop.
- **Trust boundary unchanged.** All three gates run in the VPS in-loop copy as
  *fast feedback* and are **re-derived by the local trusted gate** as authority,
  exactly like ruff/pyright/pytest/semgrep today. No VPS gate result becomes
  authoritative.
- **Not borrowed on purpose.** Multi-agent fan-out, runtime containers per
  action, MCP, web UI, and dynamic model routing were all considered and
  rejected: laddy is deliberately sequential, deterministic, HTTP-surface-free,
  and its execution isolation is the disposable-VPS trust boundary, not a
  per-action sandbox. The anti-sycophancy rw2 lens is a plausible future prompt
  change but is not cleanly testable, so it is out of this spec.
