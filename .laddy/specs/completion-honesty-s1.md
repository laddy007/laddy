---
type: feature
roles: [developer, rw1, rw2]
risk: medium
---
# completion-honesty-s1 — verified-completion guard (empty/no-op diff fails closed)

## Goal
Stop a `developer` round that reports success but produced **no net change** from
advancing into review. This is slice **S1** of the `completion-honesty` design
(see `.laddy/specs/completion-honesty.md`); it is standalone, self-contained, and
the smallest, highest-value of the three slices. The trust model is untouched —
this is one deterministic check in the existing loop, re-derived by the local
trusted gate like every other check.

## Root-cause context
`_run_developer` (`orchestrator/loop.py` ~896) records the runner's
`exit_reason` and commits the round; `derive_resume_point`'s transition table
then maps `("developer", "ok"): "fast_tests"` (`loop.py` ~142) **unconditionally**.
So a developer round returning `ok` with an empty or trivial diff sails straight
into rw1 — the loop trusts the *claim*, not the *artifact*. There is no guard
that the branch actually changed anything.

## Scope
In: `orchestrator/loop.py` (record a non-advancing outcome when a `developer:ok`
round has no net diff vs base; add the transition that re-runs the developer),
and its tests under `tests/`.
Out: the evidence receipt (S2) and the test-honesty check (S3); any change to the
verdict schema, the merge/trust behaviour, the report-only flow, or the round-cap
machinery (reuse it, don't change it); no new external dependency.

## Behaviour
- After a `developer` round whose runner `exit_reason` is `ok`, compute the
  branch's **net diff vs the task base** (merge-base with the target base branch)
  using the existing gitops helpers (`changed_files` / `diff_line_count`). An
  `ok` with an **empty net diff** is not a completion.
- Record the emptiness as a **distinct, replayable outcome** at completion time
  (suggested: `outcome="noop"`), *not* by recomputing git state during replay —
  so `derive_resume_point` stays a pure function of the append-only log (the
  invariant the whole state machine rests on). The transition table maps
  `("developer", "noop"): "developer"`, i.e. run the developer again with the
  round counter advanced.
- Because a `noop` round consumes a round, **repeated no-ops hit the existing
  round cap** (`rounds_used >= max_loops → cap_reached`) and terminate with the
  normal handback — never an infinite bounce. No new terminal is introduced.
- **Code tasks only.** The report-only flow (`_run_report_only`,
  `audit`/`investigate`) legitimately produces a report and no code diff and is
  routed separately — it must never hit this guard.

## Acceptance criteria
1. A `developer` round returning `ok` with an **empty net diff vs base** is
   recorded with the non-advancing outcome and `derive_resume_point` returns
   `developer` (not `fast_tests`) — asserted with a fake runner returning `ok`
   and a fake gitops reporting no changed files.
2. A `developer:ok` round with a **non-empty diff** transitions to `fast_tests`
   exactly as today — asserted, so the happy path does not regress.
3. Repeated empty-diff completions consume rounds and terminate at `cap_reached`
   with a handback, never looping forever — asserted with a fake that always
   reports an empty diff.
4. Report-only tasks (`audit`/`investigate`) never reach the guard — asserted by
   a report-only path test that an empty code diff there does not produce the
   `noop`/re-run behaviour.
5. **Purity preserved**: `derive_resume_point` returns `developer` for a log that
   already carries the recorded `noop` outcome **without any git access** during
   replay — asserted by feeding such a log directly to the pure function. This
   pins the invariant that loop state is derived from the log, not recomputed.
6. Suite green: `ruff`, `basedpyright`, `pytest`.

## Notes
- Keep the check deterministic and the recorded outcome self-describing: the
  `detail` on the recorded round should say the developer reported done but
  produced no change vs base, so the handback and the next developer round both
  see why the round was rejected.
- Trust boundary unchanged: this runs in the VPS in-loop copy as fast feedback
  and is re-derived by the local trusted gate as authority, like every other
  gate. No VPS result becomes authoritative.
- Borrowed idea (re-implemented natively, no dependency): loki-mode
  "verified-completion" + Dex "refuses empty diffs". See the umbrella spec.
