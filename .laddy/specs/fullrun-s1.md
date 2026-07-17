---
type: feature
roles: [developer, rw1, rw2]
risk: high
status: draft-proposal
---
# fullrun-s1 — rw3: the local trusted review as a first-class reviewer in the feedback chain

> **Depends on:** `director-resume`, which builds the shared terminal-un-stick
> mechanism (the `RESUMES` table + `clears_terminal`) that this slice needs. With
> it, rw3 here is one table row plus its verdict/transitions; without it, this
> slice would invent a second bespoke un-stick inside `_recorded_terminal` —
> exactly what `director-resume` exists to prevent.
>
> Hence `status: draft-proposal`: `kickoff fullrun-s1` refuses it. The spec is
> finished and runnable; only the ordering holds it. Once `director-resume` is in
> main, drop the `status:` line and run it.

## Goal

Make the **local trusted review a real reviewer in the loop** — `rw3` — so that a
hold at `merge-verified` flows back to the developer as a normal
`changes_requested`, instead of dead-ending in a `merge-hold.md` that a human
must read, translate into a new ask, and re-dispatch by hand.

This is slice **S1** of `.laddy/specs/fullrun.md` ("rw3 verdict + loop/run wiring
over `local_merge` (feedback chain, no driver yet). Highest structural value;
unblocks the rest"). S0 (config-driven role→runner binding) has landed.

**No driver.** The Director still runs `merge-verified.sh` by hand and still
re-kicks the VPS by hand. This slice makes that round-trip *possible and
automatic in content*; S3 automates the *driving* of it. The trust model is
untouched: rw3 runs on the trusted local machine, remains the sole merge
authority's reviewer, and the VPS still never writes main.

## Root-cause context

Two independent facts, both verified against the live code, define this slice.

**1. The chain dead-ends at a human.** `local_merge._report` writes
`merge-hold.md` and prints a diagnostic; the code comment on the `BROKEN` branch
says it outright: *"no merge offer"*. Nothing is written back to the task's log
as a reviewable verdict, so the findings cannot re-enter the loop as feedback.
The `mcp` and `report-path-guard-md` holds are exactly this.

**2. `fullrun.md` §2a does not work as written — this is the load-bearing
correction.** It says:

> The next VPS kickoff/resume fetches the branch, reads the rw3 verdict as the
> latest review outcome, and transitions to `developer` with those findings.

It cannot. `run()` calls `_recorded_terminal` as its **first** action and
short-circuits on a non-None result (`loop.py:661-663`), before
`derive_resume_point` is ever reached:

```python
recorded = _recorded_terminal(artifacts.read_log())
if recorded is not None:
    return recorded
```

And `_recorded_terminal` (`loop.py:81-105`) reverse-walks the log, returning at
the **first** `terminal` entry or completed `push`:

```python
for entry in reversed(entries):
    if action == "terminal":
        return outcome if terminal_spec(outcome).sticky else None
    if action == "push" and entry.get("outcome") == "ok":
        return "PUSHED"
```

`terminal_spec("MERGE_DECIDED:stop_before_merge")` returns `TERMINALS["PUSHED"]`
→ `kind="success"` → `sticky = kind != "retryable"` → **`True`**.

So appending an `rw3` verdict to the log **does not help**: the reverse walk
skips non-terminal actions and still finds the sticky terminal underneath it. A
re-kickoff no-ops. Verified live: the `mcp` branch's log carries
`terminal MERGE_DECIDED:stop_before_merge`, and `kickoff mcp` today does nothing.

**Therefore S1 must un-stick the terminal, or rw3 feedback can never re-enter the
loop.** This is not optional polish; without it the whole slice is inert.

**Shared mechanism — now owned by `director-resume`.** Three specs want the same
rule ("an event newer than the recorded terminal makes `_recorded_terminal`
return `None`"): `cap-override-resume` (`cap_override` → `CAP_REACHED`), this
slice (`rw3` → `MERGE_DECIDED:*`/`PUSHED`), and the Director channel itself.
`director-resume` builds it **once** as a table in `terminals.py`
(`RESUMES: dict[str, frozenset[str]]` + a pure `clears_terminal`) and lands
first. rw3 therefore contributes **one row**:

    "rw3": frozenset({"PUSHED", MERGE_DECIDED_ANY})

and must add **no** per-event branch to `_recorded_terminal`. Note that
`cap-override-resume`'s AC2 requires `cap_override` NOT to clear `PUSHED` — the
rows are deliberately different, which is precisely why the rule belongs in a
table rather than in three hand-written conditions.

## Scope

**In:**

- **`orchestrator/verdict.py`**: `RW3_VERDICT` prompt/schema constant and
  `validate_rw3`, mirroring `RW2_VERDICT` / `validate_rw2` (same `Verdict`
  dataclass, same `validate_review` base rules, same malformed/retry handling via
  `request_payload`). rw3 is a **guard** like rw2, so it inherits rw2's
  restriction: quality findings are advisory, never blockers.
- **`orchestrator/loop.py`** — `derive_resume_point` transition table: add
  `("rw3", "changes_requested") → "developer"`, `("rw3", "nogo") → "developer"`,
  `("rw3", "malformed") → "developer"`, `("rw3", "go") → "done"`.
- **`orchestrator/terminals.py`** — add the `rw3` row to `director-resume`'s
  `RESUMES` table so a `MERGE_DECIDED:*`/`PUSHED` terminal is cleared by a later
  `rw3` entry. **No change to `_recorded_terminal` itself** — the mechanism is
  already there; this slice only registers its event. Every other terminal is
  unaffected.
- **`orchestrator/local_merge.py`**: on a hold whose cause is reviewable
  (security-panel blockers / gate findings), emit an **rw3 `Verdict`** in the
  rw1/rw2 schema, write it to `.laddy/tasks/<task>/reviewer-c-verdict.json` via
  `TaskArtifacts`, append the `rw3` log entry, commit, and **push the branch to
  the hub**. `merge-hold.md` stays as the human-readable face of the same facts.
- **Convergence**: reuse the existing `_repeats`/`_fingerprints` machinery so a
  repeated rw3 finding stops the bounce (senior/deadlock path) instead of
  ping-ponging.
- **`roles/rw3.md`**: the role prompt, derived from `roles/rw2.md` (adversarial
  guard) + `roles/security.md` (the local trusted lens it replaces in the chain).
- Tests under `tests/` — pure-function level, mirroring the existing rw2 verdict
  and loop-transition tests.

**Out:**

- **The `fullrun` driver** (S3) — no automation of push/kickoff/poll. The
  Director drives by hand; this slice only makes the content flow.
- **semgrep FS-safety rules** (S2), **scope/batch arg** (S4), **human-handoff
  bundle + ntfy on bounce** (S5).
- **Any change to the trust boundary**: no VPS verdict may authorize a merge; the
  hub-main-ancestor tripwire and "VPS never writes main" are untouched. rw3 is
  produced *only* on the local trusted machine.
- **No new terminal state** in `terminals.py`, and no change to the *stickiness
  rule itself* (`kind != "retryable"`) — the un-stick is an explicit override at
  the `_recorded_terminal` level, exactly as `cap-override-resume` models it.
- **No new inter-agent message format**: rw3 reuses `Verdict` + `AgentRunner` +
  the append-only artifact log verbatim.
- The L1/L2/L3 policy question of *when* rw3 should run at all
  (`fullrun.md` AC3: "rw3 runs only on sensitive (L2/L3) tasks") — the existing
  `gather_gates` policy path already decides what holds; this slice does not add
  a new risk-tiering rule.

## Behaviour

**Local side (`merge-verified`).** When a task is held for a reviewable cause,
`local_merge` now *also* speaks the loop's language: it renders its blockers as
an rw3 `Verdict` (`CHANGES_REQUESTED` + blocker findings with concrete
`failure_scenario`s), commits it to the task's artifact log, and pushes the
branch. A hold that is **not** reviewable (a pure Tier-3 risk decision on an
otherwise green change) is unchanged — that is a human call, not a defect, and
must not be sent back to the developer as if it were one.

**VPS side (re-kickoff).** The next `kickoff <task>`:

1. `_recorded_terminal` sees `terminal MERGE_DECIDED:stop_before_merge` but finds
   an `rw3 changes_requested` **after** it in the log → returns `None` → the loop
   runs instead of no-opping.
2. `derive_resume_point` replays the log; the last phase action is
   `("rw3", "changes_requested")` → `next_phase = "developer"`.
3. The developer round receives the rw3 findings through the identical path an
   rw2 `nogo` uses today. From the loop's point of view rw3 is just another
   reviewer that returned changes.

**Repetition and bounding.** One rw3 `changes_requested` un-sticks exactly one
resumption: when the loop finishes and pushes again, the new
`push:ok`/`terminal` sits **after** the rw3 entry, so the log is sticky again
until the next rw3 verdict. This mirrors `cap-override-resume`'s
one-override-one-run property and is what keeps the un-stick from making the
terminal permanently meaningless. The existing round cap and
`_repeats`/`_fingerprints` bound the bounce; a repeated identical rw3 finding
escalates to senior/deadlock rather than looping.

## Acceptance criteria

Tests are over the pure functions (`_recorded_terminal`, `derive_resume_point`,
`validate_rw3`) with fake log entries as in `tests/test_loop_resume.py`, and over
`local_merge` with its existing fake/injection seams. No real LLM, VPS, or git
push in tests.

1. **rw3 un-sticks a merge terminal.** Log `[…, terminal
   MERGE_DECIDED:stop_before_merge, rw3/changes_requested]` →
   `_recorded_terminal(...) is None`. Without the trailing rw3 entry it still
   returns `"MERGE_DECIDED:stop_before_merge"` (today's behaviour, unchanged).
   Same for a bare `PUSHED` terminal and for a completed `push:ok`.
2. **Order is load-bearing.** Log `[…, rw3/changes_requested, …, terminal
   MERGE_DECIDED:stop_before_merge]` (rw3 *older* than the terminal) →
   `_recorded_terminal` returns `"MERGE_DECIDED:stop_before_merge"`. One rw3
   verdict buys exactly one resumption. "Newer" means later position in the
   append-only log — **not** parsed `ts`.
3. **rw3 does not un-stick anything else.** Log `[…, terminal
   PATH_GUARD_VIOLATION, rw3/changes_requested]` → still
   `"PATH_GUARD_VIOLATION"`. Same for `ESCALATED_DEADLOCK` and `CAP_REACHED`
   (that one is `cap-override-resume`'s job, not rw3's).
4. **rw3 `go` does not un-stick.** Log `[…, terminal MERGE_DECIDED:*, rw3/go]` →
   the terminal still returns. Only `changes_requested`/`nogo`/`malformed` are
   feedback; `go` means the local side merged and there is nothing to resume.
5. **Transitions route rw3 like a reviewer.** `derive_resume_point` maps
   `("rw3","changes_requested")`, `("rw3","nogo")` and `("rw3","malformed")` to
   `"developer"`, and `("rw3","go")` to `"done"` — asserted as the existing rw2
   transition tests do.
6. **rw3 verdict schema is rw2's, not a new one.** `validate_rw3` enforces
   `validate_review` (APPROVED-with-blockers and CHANGES_REQUESTED-without-blocker
   both rejected) **and** rw2's quality-blocker restriction. Asserted by reusing
   the rw2 verdict/validation test bodies against `validate_rw3`.
7. **A reviewable hold emits a pushed rw3 verdict.** Given a `BROKEN` verdict with
   security-panel blockers, `local_merge` writes `reviewer-c-verdict.json` in the
   `Verdict` schema, appends the `rw3` log entry, and pushes the branch —
   asserted with a fake gitops/artifact seam. `merge-hold.md` is still written.
8. **A pure risk-decision hold does NOT emit an rw3 verdict.** An otherwise-green
   L3 change awaiting the Director's `y/N` produces no rw3 `changes_requested` —
   a human risk call is not a defect and must not bounce to the developer.
   Asserted separately.
9. **The bounce is bounded.** A fake rw3 that returns the same finding twice in a
   row escalates via the existing `_repeats`/`_fingerprints` path (senior /
   deadlock) rather than bouncing indefinitely.
10. **Trust invariants intact.** Grep/test: no rw3 path lets a VPS-produced
    verdict authorize a merge; rw3 is constructed only in the local
    (`local_merge`) path; the hub-main-ancestor tripwire is untouched; no new
    state in `terminals.py`; the `sticky` rule (`kind != "retryable"`) is
    unchanged.
11. **Round-trip, end to end against fakes.** A log that reaches
    `MERGE_DECIDED:stop_before_merge`, then receives an rw3 `changes_requested`,
    resumes to `developer`, runs a round, pushes again, and is sticky once more —
    one test that walks the whole cycle and asserts the sticky→resume→sticky
    transitions.
12. Suite green: `pytest -n auto -q`, `ruff check .` clean, `basedpyright` clean
    for the touched scope.

## Notes for the reviewer

- **The un-stick is the whole slice; verify it first.** If `_recorded_terminal`
  is left alone, every other change here is dead code: the loop no-ops on
  re-kickoff and rw3 findings never reach a developer. AC1 + AC11 are the
  regression locks. Reject an implementation that adds the rw3 verdict and
  transitions but leaves the short-circuit intact.
- **Verify the one-verdict-one-run property (AC2).** If the un-stick keyed on
  "an rw3 entry exists anywhere" rather than "newer than the last terminal", the
  terminal becomes permanently non-sticky and the replay-idempotence that
  `_recorded_terminal`'s docstring protects (a re-kickoff must not re-fire ntfy
  or re-push an empty deliverable) is lost.
- **Do not widen the un-stick.** `PATH_GUARD_VIOLATION` in particular must stay
  sticky (AC3): its whole point is that the branch carries edits a report-only
  task was forbidden to make. `CAP_REACHED` is `cap-override-resume`'s scope —
  do not absorb it here.
- **Check the AC8 boundary carefully.** The distinction between "held because
  something is wrong" (→ rw3 feedback) and "held because a human must decide" (→
  no feedback) is the one judgement call in this slice. Getting it wrong turns
  every L3 risk-confirm into a developer round that has nothing to fix and burns
  the cap — the exact failure `validate_review` already guards against for empty
  CHANGES_REQUESTED.
- **Shared mechanism with `cap-override-resume`.** If that spec has landed by the
  time this runs, reuse its override helper rather than writing a second one; if
  it has not, shape this one so it can absorb `cap_override` later. Two
  independent "newer event un-sticks a terminal" implementations in
  `_recorded_terminal` is a reject.
- **rw3 is a guard, not a second rw1.** It inherits rw2's asymmetry (binding on
  real defects, advisory on quality). An rw3 that blocks on style will burn
  developer rounds on the trusted machine's authority — the worst place for it.
- Reject if: any VPS-produced artifact can authorize a merge; a new terminal
  state appears; the `Verdict` schema is forked for rw3; the un-stick applies to
  a terminal other than `MERGE_DECIDED:*`/`PUSHED`; or a repeated rw3 finding can
  bounce without bound.
