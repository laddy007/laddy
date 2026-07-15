---
type: feature
roles: [developer, rw1, rw2]
risk: medium
---
# fullrun-s0 — config-driven role→{vendor, model, thinking} binding

## Goal
Make WHICH agent runs each loop role — and with what model and reasoning
(thinking) level — fully config-driven from `env.vps` / `env.local`, instead of
the vendor being hardcoded in `orchestrator/run.py`. This is slice S0 of the
`fullrun` design (see `.laddy/specs/fullrun.md`); it is a standalone,
self-contained change and the prerequisite for later slices (rw3 needs to be
codex-on-local without editing run.py).

## Root-cause context
Today the review CLI *commands* are already env config
(`CLAUDE_CMD`/`RW2_CMD`/`SENIOR_CMD`/`CODEX_CMD`) and the model is baked into
them (e.g. `DEFAULT_RW2_CMD = claude --model sonnet`). But WHICH vendor/runner
serves each role is fixed in `run.py`:

    make_runner        = lambda c: ClaudeRunner(...)   # developer, rw1, clarify
    make_rw2_runner    = lambda c: ClaudeRunner(...)   # rw2
    make_senior_runner = lambda c: ClaudeRunner(...)   # senior

So rw2 cannot be pointed at codex without editing code, and a per-role
reasoning/thinking level is not wired anywhere.

## Scope
In: `orchestrator/run.py`, `orchestrator/agents.py`, `env.vps.example`,
`env.local.example`, and their tests under `tests/`.
Out: the `fullrun` driver, rw3, semgrep rules, any cross-machine behaviour
(all later slices); no new external dependency; no change to the loop's
control flow, verdict schema, or merge/trust behaviour.

## Behaviour
- Each loop role — at least `developer`, `rw1`, `rw2`, `senior`, `clarify` —
  resolves a `{vendor, model, thinking}` triple from the environment. Proposed
  env schema (adjust names to fit existing conventions, keep it minimal):
  `ROLE_<NAME>_VENDOR` (`claude`|`codex`), `ROLE_<NAME>_MODEL`,
  `ROLE_<NAME>_THINKING`.
- `vendor` selects the runner class (`ClaudeRunner`/`CodexRunner`) — the
  hardcoded `make_*_runner` lambdas are replaced by one resolver that maps a
  role's config to the right runner. Both runners already share the
  `AgentRunner` protocol, so nothing downstream changes.
- `model` maps to the vendor's model flag (as the CMD strings do today).
- `thinking` is a new explicit knob mapped to the vendor's reasoning/thinking
  flag where one exists; where a vendor exposes none, it is a documented no-op
  (never a hard error).

## Acceptance criteria
1. With no role-specific env set, behaviour is **unchanged** from today
   (developer/rw1/clarify/senior → Claude; rw2 → Claude/sonnet): existing
   defaults and the current `CLAUDE_CMD`/`RW2_CMD`/`SENIOR_CMD` knobs still
   work, so no live deployment breaks. Asserted by tests that assert the
   default runner/model per role.
2. Setting rw2's vendor to `codex` in env results in a `CodexRunner` serving
   rw2 — **no code change** — asserted by a test that sets the config and
   checks the runner class the loop wiring selects for rw2 (via the existing
   `Deps`/fake injection, not a live model).
3. `model` and `thinking` for a role are threaded into the runner's command
   (model → the vendor model flag; thinking → the vendor reasoning flag where
   present, no-op otherwise), asserted by inspecting the constructed command.
4. The resolver is uniform: the same role→config→runner path is used for every
   role (no per-role special-casing left in `run.py`).
5. Suite green: `ruff`, `basedpyright`, `pytest`.

## Notes
- Keep the change additive and backward-compatible: prefer resolving the new
  `ROLE_*` knobs with the existing `*_CMD` values as fallbacks, so the migration
  is opt-in and nothing already deployed regresses.
- Do not touch the loop state machine, verdict schema, or any merge/trust code —
  this slice is purely the role→runner resolution.
