I now have a complete, concrete picture. Here is my exploration summary.

---

# Exploration: `director-resume`

## Verdict

This is a **well-specified, low-ambiguity task**. The spec is precise about the seam, the rule's single home, and the failure classes. My job here is mostly to confirm the seams exist as described, pin exact line numbers, and surface the **three concrete gaps the ACs do not force but a correct implementation must handle**. There is no bug to reproduce — this is a feature. I did not change product code.

## The mechanism, confirmed against the code

The un-stick is a two-part fact of the log-replay engine:

1. `run()` (`loop.py:661-663`) calls `_recorded_terminal` **first** and short-circuits before `derive_resume_point` is ever reached. So un-sticking **must** happen inside `_recorded_terminal` — nowhere else can re-open the loop.
2. `_recorded_terminal` (`loop.py:98-105`) reverse-walks and returns at the first `terminal` (sticky per `terminals.py:33`) or `push:ok`. `director_resume` is an unknown action → the reverse walk skips it and still finds the terminal underneath. This is why "append a later event" alone does nothing, exactly as the spec says.

`derive_resume_point` (`loop.py:122-173`) reacts **only** to `_PHASE_ACTIONS` (`loop.py:131-132`) and the three session-carrying actions. An unknown `director_resume` touches neither `last`, `rounds_used`, nor sessions — so **AC6 (transition derivation untouched) holds for free**; no code is needed to protect it, only a test.

## Affected files

| File | Change |
|---|---|
| `orchestrator/terminals.py` | Add `RESUMES: dict[str, frozenset[str]]`, a `MERGE_DECIDED_ANY` sentinel, and pure `clears_terminal(action, terminal)`. Reuse existing `_MERGE_DECIDED_PREFIX` (`terminals.py:23`). |
| `orchestrator/loop.py` | (a) `_recorded_terminal` (81-105): after finding a sticky state, scan entries **after** it and return `None` if any `clears_terminal`. (b) Add `DIRECTOR_NOTE_SECTION` constant (near line 450). (c) `_run_developer` (896-937): prepend the note via the existing seam at 917-920. |
| `orchestrator/run.py` | Add `--phase resume` + `--reason` arg; `_phase_resume`; wire into `main` dispatch (811-823) and `single_task_phases` (749). |
| `orchestrator/gitops.py` | Add `blob_sha(path)` (a `git hash-object` wrapper) for `spec_sha` — no existing helper. |
| `orchestrator/handoff.py` | Add the resume receipt (count + latest reason) to `build_summary`/`build_handback`, **and exclude `director_resume` from the round-trace loops**. |
| `scripts/kickoff.sh` | `--resume` branch: skip new/clarify/design, forward `--reason`, detach via `nohup` (mirror lines 66-70). |
| `tests/` | `test_loop_resume.py` (AC1-7,12), `test_terminals.py` (AC4-5), `test_run_cli.py` (AC8-9,11), `test_handoff.py` (AC10). |
| Docs | `USAGE.md`, `SECURITY.md`. |

## Proposed approach (write tests first per AC)

**`clears_terminal` / `RESUMES`** — table + prefix, mirroring `terminal_spec`:
```python
MERGE_DECIDED_ANY = _MERGE_DECIDED_PREFIX  # sentinel: "clears any MERGE_DECIDED:*"
RESUMES = {"director_resume": frozenset({"CAP_REACHED","ESCALATED_DEADLOCK","PUSHED", MERGE_DECIDED_ANY})}

def clears_terminal(action, terminal):
    states = RESUMES.get(action, frozenset())
    if terminal in states:
        return True
    return terminal.startswith(_MERGE_DECIDED_PREFIX) and MERGE_DECIDED_ANY in states
```

**`_recorded_terminal`** — single positional scan wrapping **both** return sites (the `terminal` branch *and* the `push:ok`→`"PUSHED"` branch, since AC1 asserts a bare `push:ok` is also un-stuck):
```python
for idx in range(len(entries) - 1, -1, -1):
    action = entries[idx].get("action")
    if action == "terminal":
        outcome = str(entries[idx].get("outcome"))
        if not terminal_spec(outcome).sticky: return None
        state = outcome
    elif action == "push" and entries[idx].get("outcome") == "ok":
        state = "PUSHED"
    else:
        continue
    if any(clears_terminal(str(e.get("action")), state) for e in entries[idx+1:]):
        return None
    return state
return None
```
No per-event `if` — the AC4 lock. It contains no hardcoded event name; that's grep-assertable.

**Director note** — positional, "newer than the last phase action" (identical shape to `_dev_context`'s `last_phase` computation at `loop.py:609-611`), so it renders on the first developer round after the resume and self-clears once a `developer` entry is appended:
```python
def _director_note(entries):
    for e in reversed(entries):
        if e.get("action") in _PHASE_ACTIONS: return None      # a phase ran after the resume
        if e.get("action") == "director_resume": return e.get("reason")
    return None
```
In `_run_developer`, prepend `DIRECTOR_NOTE_SECTION.format(reason=...)` to `context` (keep it **additive** — AC7 — so the rw1/rw2 verdict section survives).

**CLI `_phase_resume`** — validate in this order, **writing nothing until all pass**: `--reason` non-empty; task worktree/log exists with a recorded terminal (else "never started"); the last recorded sticky terminal satisfies `clears_terminal("director_resume", state)` (so `PATH_GUARD_VIOLATION` and unknown states refuse). Only then compute `spec_sha` from the worktree spec, `append_log(action="director_resume", outcome="ok", reason=…, spec_sha=…)`, `commit_all`, then delegate to `_phase_loop(..., skip_clarify=True)` (task is underway → `has_clarify` already true, design already approved).

## Risks & failure classes (the parts the ACs do *not* force)

**RISK 1 — the flagship `mcp` example does not actually re-develop; it routes to `done`. (highest value)**
`mcp` carries `terminal MERGE_DECIDED:stop_before_merge`, whose `_finalize` tail is `[…, push:ok, terminal MERGE_DECIDED:…]` (`loop.py:801-804`). After `director_resume` un-sticks `_recorded_terminal`, `derive_resume_point` replays and finds the **last phase action = `push:ok`** → transition `("push","ok"): "done"` (`loop.py:157`) → the `done` handler (`loop.py:747-762`) **re-records the same terminal and re-pushes** — no developer round, no note delivered. The un-stick primitive works (AC1 passes, it only tests `_recorded_terminal is None`), but re-development only happens for terminals whose tail routes to `developer` (`CAP_REACHED`, `ESCALATED_DEADLOCK`, a trailing `rw1 changes_requested` — which is exactly what AC6/AC7 fixtures use). **This is consistent with the spec's own words** ("next_phase derives from the last real phase action exactly as before", and re-opening-after-push is deferred to the future `rw3` consumer), but it contradicts the *motivating narrative* that resuming `mcp` continues the loop. The developer should **flag this to the Director explicitly** and confirm the intent: this spec ships the seam (un-stick + note), and a `PUSHED`/`MERGE_DECIDED` resume is inert-by-design until `fullrun-s1` adds the transition. Do **not** silently add a `push→developer` transition to "fix" it — that would violate the Out-list and AC6.

**RISK 2 — `director_resume` will leak into the human-summary/handback round trace.** The round-trace loops filter on `"outcome" not in entry` (`handoff.py:69`, `handoff.py:138`); flag events survive because `raise_flag` writes **no** `outcome` (`flags.py:152-157`). But the spec mandates `director_resume` carry `outcome="ok"`, so it will render as a garbled `` `director_resume` -> ok `` line. The receipt work must **positively exclude** `action=="director_resume"` from both loops and render the count+latest-reason in a dedicated section (AC10).

**RISK 3 — un-stick must wrap the `push:ok` branch, not just `terminal`.** AC1's "and for a completed `push:ok`" case only clears if the scan-after logic covers `loop.py:103-104` too. A naive implementation that only guards the `terminal` branch passes 4 of 5 AC1 sub-cases and silently fails the fifth. The single-loop rewrite above handles both; a two-return-site patch is the trap.

**Trust (per the spec's "think this through" note):** I traced every write on the resume path. `_phase_resume` appends one log line + `commit_all` (local) and calls `_phase_loop`; it never calls `gitops.push` to origin, never touches `merge_decision`/`MERGE_DECISION`/`code_sha`, and a resumed task re-traverses `derive_resume_point`'s rw1/rw2/authoritative gates identically to a fresh run (they re-derive from the log). A forged `director_resume` from a compromised VPS buys only more of its own compute — it cannot merge, push, or skip a reviewer. **Conclusion matches the spec: bounded and acceptable, no attestation needed.** The one place to keep honest is `spec_sha` — it is a *receipt only* (recorded, shown in the handback); nothing must *branch* on it, or it becomes a trust input.

## Acceptance criteria as tests the developer must write
- **AC1/AC2/AC3** — `_recorded_terminal` over fake logs: newer `director_resume` → `None` for each of `{CAP_REACHED, ESCALATED_DEADLOCK, PUSHED, MERGE_DECIDED:stop_before_merge, push:ok}`; older event → returns the state; `PATH_GUARD_VIOLATION` stays sticky even with a newer resume.
- **AC4** — table-driven test over `RESUMES` + a grep test asserting no event name is hardcoded in `loop.py`.
- **AC5** — `clears_terminal("director_resume","MERGE_DECIDED:<novel-suffix>")` is `True`.
- **AC6** — `[…, rw1 changes_requested, director_resume]` → `derive_resume_point(...).phase == "developer"` and identical `round` to the same log without the event.
- **AC7** — developer context contains the reason **and** the rw1 verdict JSON (additive); a following developer round does not re-render it.
- **AC8/AC9** — CLI: empty/missing `--reason`, non-resumable last terminal, unknown task each → non-zero with nothing appended; happy path appends exactly one `director_resume` with `reason` and `spec_sha`, then enters the loop (stubbed).
- **AC10** — after a later terminal, summary/handback names the resume count + latest reason.
- **AC11** — resume path asserted to not push/merge/skip-a-reviewer and add no `terminals.py` state.
- **AC12** — `director_resume` with no later terminal → plain re-derivation continues (no second event).
- **AC13** — `pytest -n auto -q`, `ruff check .`, `basedpyright` clean for touched scope.