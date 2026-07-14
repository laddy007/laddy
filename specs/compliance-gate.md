---
type: feature
---

# compliance-gate - deterministic pre-review gate in the `.laddy` loop

## Authoritative context

`.laddy` is the canonical agent runtime. Legacy `.agent/scripts/agent-flow.sh`
and the old `.agent/specs/agent-compliance-gate.md` are reference material only.
Implement this in Python, using the existing loop, artifacts, and tests.

Relevant current files:

- `.laddy/orchestrator/loop.py`
- `.laddy/orchestrator/testgate.py`
- `.laddy/orchestrator/run.py`
- `.laddy/orchestrator/artifacts.py`
- `tests/agent_orchestrator/test_loop.py`
- `tests/agent_orchestrator/test_run_cli.py`

## Goal

Add a fast deterministic compliance gate after developer changes and before
`rw1`. If the gate fails, the loop must skip reviewer calls for that iteration,
send the failure back to the developer, and re-check after the developer fixes
it.

## Non-goals

- No legacy `.agent` shell port.
- No replacement for the authoritative Docker binding gate.
- No AI reviewer call from inside the compliance gate.
- No weakening of existing fast tests, rw2, senior, or local merge checks.

## Scope

1. Add a compliance phase to the loop state machine.
2. Run a configured deterministic command set in the task worktree.
3. Record compliance pass/fail in the append-only task log.
4. On fail, include the command tail in the next developer prompt.
5. On pass, continue to `rw1`.
6. Make the default command cheap and deterministic; allow environment override.

## Acceptance criteria

- A developer `ok` result transitions to `compliance` before `rw1`.
- A compliance failure transitions back to `developer`.
- `rw1` is not called in an iteration where compliance failed.
- The failure tail is visible in the developer prompt context.
- Resume derivation handles existing logs without a compliance action.
- Tests cover pass, fail, and backward-compatible resume behavior.
