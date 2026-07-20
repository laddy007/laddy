---
type: fix
roles: [developer, rw1, rw2]
risk: high
status: draft-proposal
---
# auto_merge-gate-input-hardening — untrusted hub input reaching the trusted merge gate

## Goal
Three findings from the `engine-rest-fix` L3 security panel (2026-07-20 local
`--no-input` re-judge of `fix/merge-flow-queue`). All three are **pre-existing**
engine defects, not defects that branch introduced: it was simply the branch
whose judgement surfaced them. They share one theme - input that originates on
the untrusted VPS side reaches trusted-machine code paths without validation -
so they are fixed together rather than one per task.

**Not yet independently verified.** These are the security panel's claims, an
LLM judgement gate. Reproduce each one before changing anything; close any that
does not survive contact with the code, and say so rather than inventing a fix.

## Findings to verify, then fix

### F1 - `check_local_fix` swallows `SpecError` alongside `OSError`
The report-only guard is disabled wholesale when a branch corrupts its own spec
file, because the handler catches `SpecError` as well as `OSError`. The inverse
of `merge_check.check`, which hard-fails the same condition. A branch that can
make its own spec unparseable therefore turns off the guard that exists to stop
it touching source. Fail closed, and pin the asymmetry with a test so the two
call sites cannot drift apart again.

### F2 - hub branch names become task ids with no charset validation
Task ids derived from hub branch names are passed to git as **bare positional
arguments with no `--` separator**, on the Director's TRUSTED machine. An
untrusted VPS agent controls those branch names, so a name beginning with `-`
is argument injection into git commands running locally. Validate the charset at
the boundary where a branch name becomes a task id (fail closed on anything
outside the expected set), AND add the `--` separator - the separator alone is
not enough where an id also reaches a non-git consumer.

### F3 - a colliding hub branch name wedges the whole merge gate
A hub branch whose name collides with the base branch's tracking ref makes the
discovery fetch fail **outside** the per-task isolation try block, so one bad
branch stops every task rather than just its own. Move the fetch inside the
per-task boundary (or pre-validate before it), so a single malformed branch is
isolated to its own task exactly like every other per-task failure.

## Acceptance criteria
- AC1: each finding is either reproduced by a failing test first, or explicitly
  closed as not-a-defect with the reasoning recorded - no silent drops.
- AC2: F1 fails closed; `check_local_fix` and `merge_check.check` treat a
  `SpecError` identically, pinned by a test.
- AC3: F2 rejects an out-of-charset task id at the boundary, and every git call
  taking a task id as a positional argument carries `--`. A leading-dash id is
  covered by a regression test.
- AC4: F3 isolates a colliding/malformed hub branch to its own task; the other
  tasks in the same run still process.
- AC5: no behaviour change ships without a test (untested behaviour is
  undefined).

## Notes
- `orchestrator/merge_check_local.py`, `orchestrator/local_merge.py` and
  `orchestrator/gitops.py` are all in laddy's own `security_globs`, so this task
  is a stop-before-merge / L3 human-gated change by construction. It cannot
  auto-merge, which is the intended outcome for a trust-boundary fix.
- Related: `.laddy/specs/agent-error-visibility.md` covers the reason this panel
  run could not say WHY its codex member failed - the engine discards the
  agent's own error text on a non-ok exit.
