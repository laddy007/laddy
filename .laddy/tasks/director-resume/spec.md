---
type: feature
roles: [developer, rw1, rw2]
risk: high
---
# director-resume — one trusted resume channel: un-stick a terminal, amend the ask, continue

## Goal

Give the Director one explicit, logged, append-only way to **put a finished task
back to work**: un-stick its terminal, hand the developer a corrected spec and a
written note saying what changed, and let the loop continue — without editing the
append-only log, without a new store, and without touching the trust model.

And do it **once**. Three separate specs currently want the same mechanism —
"an event newer than the recorded terminal makes the loop resume" — each
inventing its own shape:

| Spec | Event | Un-sticks |
|---|---|---|
| `cap-override-resume` (draft) | `cap_override` | `CAP_REACHED` |
| `fullrun-s1` (promoted) | `rw3` `changes_requested`/`nogo` | `MERGE_DECIDED:*` / `PUSHED` |
| **this spec** | `director_resume` | see the resume table below |

Three bespoke implementations of one rule inside `_recorded_terminal` is the
outcome to avoid. This spec builds the **shared mechanism** plus its first
consumer (the Director channel); the other two become table rows.

## Root-cause context

**1. Every terminal that matters is sticky, and stickiness is all-or-nothing.**
`run()` calls `_recorded_terminal` as its first action and short-circuits
(`loop.py:661-663`) before `derive_resume_point` is ever reached.
`_recorded_terminal` (`loop.py:81-105`) reverse-walks the log and returns at the
**first** `terminal` entry or completed `push`:

```python
for entry in reversed(entries):
    if action == "terminal":
        return outcome if terminal_spec(outcome).sticky else None
    if action == "push" and entry.get("outcome") == "ok":
        return "PUSHED"
```

Stickiness is derived from one coarse rule in `terminals.py`:
`sticky = kind != "retryable"`. So `CAP_REACHED` (failure),
`ESCALATED_DEADLOCK` (failure), `PUSHED` (success) and every `MERGE_DECIDED:*`
(which `terminal_spec` maps to the `PUSHED` spec) are **permanently** sticky.
A re-kickoff no-ops. Verified live: the `mcp` branch carries
`terminal MERGE_DECIDED:stop_before_merge` and `kickoff mcp` today does nothing.

Appending a later event does **not** help by itself: the reverse walk skips
non-terminal actions and still finds the terminal underneath.

**2. There is no way to correct the ask.** The spec is the developer's source of
truth and it lives at `.laddy/specs/<task>.md` **on the task branch** —
`_load_spec` reads it from the worktree (`run.py:161`), which `task_worktree`
checks out from the branch (`gitops.py:98-121`). Nothing in the engine lets the
Director say "the spec was wrong, here is the corrected one, carry on". The only
channels that reach a developer round are reviewer verdicts and test tails
(`_dev_context`, `loop.py:607-628`) — all agent-produced, none Director-authored.

`mcp` is exactly this failure: the code was not wrong, the **spec** was (it never
required throttling or replay protection). No reviewer can fix that, because
every reviewer reviews *against* the spec. Even `fullrun`'s rw3 would bounce the
finding to a developer whose spec still does not ask for throttling.

**3. The feedback channel already has the right shape to extend.**
`_dev_context` maps the last phase action to a rendered section, and
`_run_developer` (`loop.py:917-920`) already **prepends** a section to that
context:

```python
context = self._dev_context(artifacts)
exploration = artifacts.read_text(EXPLORATION)
if exploration and rp.round == 1:
    context = EXPLORATION_SECTION.format(exploration=exploration) + context
```

A Director note takes the same prepend seam: it augments the normal context
rather than replacing it, so the developer sees both "the Director changed the
ask" *and* the reviewer verdict that stopped it.

## Scope

**In:**

- **A resume table, one home for the rule** — new in `orchestrator/terminals.py`,
  next to `TERMINALS` (that module already exists to be "one home for what a
  terminal MEANS"):

  ```
  RESUMES: dict[str, frozenset[str]]   # log action -> terminal states it clears
  ```

  plus a pure helper `clears_terminal(action: str, terminal: str) -> bool`.
- **`_recorded_terminal` (`loop.py`)**: when a sticky terminal is found, scan the
  entries **after** it; if any carries an action that `clears_terminal` admits for
  that terminal state, return `None` instead. This is the **only** place the rule
  lives; the `sticky` rule itself (`kind != "retryable"`) is unchanged.
- **`director_resume` log event** (append-only): `action="director_resume"`,
  `outcome="ok"`, mandatory non-empty `reason`, `ts`, and `spec_sha` (the spec
  blob's sha at resume time, recorded so the receipt shows whether the ask
  changed). Committed to `agent/<task>` like every other artifact.
- **CLI**: `orchestrator.run <task> --phase resume --reason "<text>"` — validate,
  append `director_resume`, commit, and start the loop detached. Skips
  clarify/design (the task is already under way).
- **`scripts/kickoff.sh <task> --resume`**: thin passthrough that forwards the
  reason and detaches (survives an SSH drop, matching the existing kickoff
  pattern).
- **`DIRECTOR_NOTE_SECTION`** rendered into the developer prompt via the existing
  prepend seam in `_run_developer`, carrying the Director's `reason` verbatim,
  for the **first developer round after** a `director_resume` only.
- **Visibility**: the count of `director_resume` events and the latest reason
  appear in `human-summary.md` / handback (e.g. "resumed 2×"), so a subsequent
  terminal shows the Director's own interventions.
- Docs: `USAGE.md` (how to correct a task mid-flight), `SECURITY.md` (why this is
  not a trust weakening — see Notes).
- Tests under `tests/` — pure functions (`clears_terminal`,
  `_recorded_terminal`, `_dev_context`) plus a CLI passthrough stub.

**Out:**

- **No new terminal state** in `terminals.py`. After a resumed run exhausts
  itself, whatever terminal it reaches is recorded normally.
- **No cap on resumes.** Unlimited, exactly as `cap-override-resume` argued: the
  guard is that it is a manual, logged act with a mandatory reason and a visible
  count. A hard wall can be added later as one CLI condition.
- **No automatic spec authoring.** The Director edits
  `.laddy/specs/<task>.md` on the branch with their own editor and pushes it, as
  they would any file. This spec **records** that the spec changed (`spec_sha`);
  it does not co-author it. (`--phase new` remains the authoring path.)
- **No trust-model change.** `director_resume` re-arms *iteration*, nothing else.
  A resumed task passes rw1/rw2/authoritative exactly as a fresh one; the local
  machine remains the sole merge authority and the VPS still never writes main.
  No push to origin, no review bypass, no merge-policy or gate-SHA change.
- **`PATH_GUARD_VIOLATION` is NOT resumable** — deliberate, see Behaviour.
- **The other two consumers' semantics.** This spec adds their table rows only if
  their specs have landed; it does **not** implement per-gate budget resetting
  (`cap-override-resume`) or the rw3 verdict/transitions (`fullrun-s1`). It is the
  seam they plug into.
- No change to `_PHASE_ACTIONS`, the transition table, `round` labelling,
  `nonconvergence_detected`/senior slicing, or quota logic.

## Behaviour

### The resume table

```
RESUMES = {
    "director_resume": frozenset({
        "CAP_REACHED", "ESCALATED_DEADLOCK", "PUSHED", MERGE_DECIDED_ANY,
    }),
    # rows added by their own specs as they land:
    # "cap_override": frozenset({"CAP_REACHED"}),
    # "rw3":          frozenset({"PUSHED", MERGE_DECIDED_ANY}),
}
```

`MERGE_DECIDED:*` is matched by prefix, mirroring `terminal_spec`'s existing
`_MERGE_DECIDED_PREFIX` handling. `clears_terminal` is the single place that
knows this.

**`PATH_GUARD_VIOLATION` is absent on purpose.** It means the branch carries
source edits a report-only task was forbidden to make — `terminals.py` singles it
out as "the one failure that must NOT push: never publish the violating tree".
Resuming would continue working on a poisoned tree. The remedy is to discard the
branch and restart, not to continue. `QUOTA_TIMEOUT` / `INTERNAL_ERROR` need no
row: they are already `retryable` and resume today.

### The Director's round trip

1. Director reads the handback / `merge-hold.md`, and — if the ask itself was
   wrong — edits `.laddy/specs/<task>.md` **on the task branch** and pushes it.
2. `kickoff <task> --resume --reason "spec was missing throttling; added it, plus
   replay protection and 0600 notes"`.
3. Validation (else refuse, non-zero, **nothing written**): the task exists; its
   last terminal is one `director_resume` clears; `--reason` is non-empty.
4. The event is appended and committed; the loop starts detached.
5. `_recorded_terminal` finds the sticky terminal, sees the newer
   `director_resume`, and returns `None` → the loop runs.
6. `derive_resume_point` replays as today. `director_resume` is **not** in
   `_PHASE_ACTIONS`, so it does not become `last`: `next_phase` derives from the
   last real phase action exactly as before (e.g. a trailing
   `rw1 changes_requested` still routes to `developer`).
7. The next developer round gets `DIRECTOR_NOTE_SECTION` **prepended** to its
   normal context, so it reads the Director's note *and* the verdict that stopped
   it. Subsequent rounds do not repeat the note.

### Repetition and idempotence

One `director_resume` buys exactly one run to the next terminal — the same
property `cap-override-resume` defines:

- `[…, terminal CAP_REACHED, director_resume]` → newer than the terminal →
  resumes.
- The resumed run hits a terminal again: `[…, director_resume, …, terminal X]` →
  no newer resume event → sticky again → stopped.
- A second `--resume` appends another event after that terminal → resumes again.
  Unbounded by design.
- Crash mid-resumed-run (no new terminal): the last terminal is still older than
  the `director_resume`, so a plain re-kickoff resumes normally — no second event
  needed. Resume stays crash-safe.

"Newer" means **later position in the append-only log**, never a parsed `ts`.

## Acceptance criteria

Tests are over pure functions (`clears_terminal`, `_recorded_terminal`,
`_dev_context`) with fake log entries as in `tests/test_loop_resume.py`; the CLI
is tested with a passthrough stub. No real LLM, git push, or VPS.

1. **A newer `director_resume` un-sticks.** `[…, terminal CAP_REACHED,
   director_resume]` → `_recorded_terminal(...) is None`. Without the event →
   `"CAP_REACHED"` (today's behaviour). Asserted for each of `CAP_REACHED`,
   `ESCALATED_DEADLOCK`, `PUSHED`, `MERGE_DECIDED:stop_before_merge`, and for a
   completed `push:ok`.
2. **Order is load-bearing.** `[…, director_resume, …, terminal CAP_REACHED]`
   (event older than the terminal) → returns `"CAP_REACHED"`. One event, one run.
3. **`PATH_GUARD_VIOLATION` stays sticky.** `[…, terminal PATH_GUARD_VIOLATION,
   director_resume]` → still `"PATH_GUARD_VIOLATION"`. Asserted explicitly, with
   the reason in the test name.
4. **The rule lives in exactly one place.** `clears_terminal` is a pure function
   over `(action, terminal)`; `_recorded_terminal` consults it and contains no
   per-event `if`. Asserted by a table-driven test over `RESUMES` **and** by grep:
   no event name is hardcoded in `loop.py`.
5. **`MERGE_DECIDED:*` matches by prefix.** `clears_terminal("director_resume",
   "MERGE_DECIDED:anything")` is true, sharing `terminals.py`'s existing prefix
   constant — asserted with an unknown decision suffix.
6. **Transition derivation is untouched.** `director_resume` is not in
   `_PHASE_ACTIONS`: with a trailing `rw1 changes_requested` then
   `director_resume`, `derive_resume_point(...).phase == "developer"` and the
   `round` label is unchanged from the same log without the event.
7. **The note reaches the developer, once.** With a `director_resume` newer than
   the last phase, the developer context **contains** the reason verbatim **and**
   still contains the normal section (e.g. the rw1 verdict JSON) — the note is
   prepended, not substituted. After the next developer round, a subsequent round
   does not re-render it.
8. **CLI validation.** `--phase resume`: no/empty `--reason` → non-zero, nothing
   written; last terminal not resumable (`PATH_GUARD_VIOLATION`) → non-zero, clear
   message, nothing written; unknown/never-started task → non-zero, nothing
   written. Each asserted separately.
9. **CLI happy path.** `--phase resume --reason "x"` on a `CAP_REACHED` task
   appends exactly one `director_resume` with `reason == "x"` and a `spec_sha`,
   then starts the loop — asserted with a stub capturing the append and the phase.
10. **Receipt.** After a later terminal, `human-summary.md`/handback names the
    number of `director_resume` events and the latest reason — asserted over the
    summary/handback path.
11. **Trust intact.** Grep/test: the `--phase resume` path does not push to
    origin, does not bypass rw1/rw2/authoritative (a resumed task traverses them
    normally), adds no state to `terminals.py`, and does not touch
    merge-decision or gate-SHA logic. Its only effects are un-sticking and the
    prepended note.
12. **Crash-safe resume.** After a `director_resume` and no subsequent terminal, a
    plain re-derivation continues the loop rather than stopping — no second event
    required.
13. Suite green: `pytest -n auto -q`, `ruff check .` clean, `basedpyright` clean
    for the touched scope.

## Notes for the reviewer

- **The table is the point; reject a bespoke `if`.** If the implementation adds
  `if action == "director_resume"` to `_recorded_terminal` instead of a table +
  `clears_terminal`, this spec has failed at its actual goal — the next two
  consumers (`cap_override`, `rw3`) then add two more `if`s and we are back to
  three shapes of one rule. AC4 is the lock.
- **Verify the one-event-one-run property (AC2).** Keying the un-stick on "a
  `director_resume` exists anywhere in the log" rather than "after the terminal"
  makes the terminal permanently non-sticky and destroys the replay-idempotence
  that `_recorded_terminal`'s docstring exists to protect (a re-kickoff must not
  re-fire the single ntfy, nor re-push an empty deliverable for a malformed
  report-only task). That docstring is the specification of what must not break.
- **Trust: think this through, do not wave it past.** The VPS writes the task log,
  so a compromised VPS could forge a `director_resume` and un-stick itself. The
  reasoned conclusion is that this is **bounded and acceptable**: a forged resume
  buys the VPS more of its own compute, and nothing else — it cannot merge (the
  local machine is the sole authority and re-derives every gate), cannot push to
  origin, and cannot skip a reviewer. No attestation is therefore required for the
  mechanism to be safe. **But** if you find any path where an un-stick *does*
  influence a merge decision, a gate SHA, or the hub-main-ancestor tripwire, that
  is a CHANGES_REQUESTED — the reasoning above depends on it.
- **`PATH_GUARD_VIOLATION` (AC3) is the one exclusion; hold the line.** It is
  tempting to argue "the Director is the trust anchor, they may resume anything".
  The tree is the problem, not the permission: resuming builds on edits the task
  was forbidden to make. Discard and restart.
- **Check the note is additive (AC7).** If `DIRECTOR_NOTE_SECTION` replaces
  `_dev_context` instead of prepending, the developer loses the verdict that
  stopped it and gets only prose — a strictly worse round than today's.
- **This spec must land before `cap-override-resume` and `fullrun-s1`.** Both
  reference this mechanism; both shrink to a table row plus their own semantics
  (budget reset / rw3 verdict+transitions) once it exists. If either has somehow
  landed first, reconcile onto the table rather than leaving two mechanisms.
- Reject if: the rule is not table-driven; the un-stick is not positional; any
  terminal outside the table becomes resumable; a new terminal state appears; the
  resume path pushes, merges, or skips a reviewer; or `round`/`_PHASE_ACTIONS`/
  transition semantics shift.
