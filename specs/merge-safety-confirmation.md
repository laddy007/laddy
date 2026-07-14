---
type: feature
---

# merge-safety-confirmation - explicit task confirmation before local merge side effects

## Authoritative context

Local merge authority lives in `.laddy/orchestrator/local_merge.py` and is
launched by `.laddy/scripts/merge-verified.sh`. Legacy `.agent` merge-safety
specs are design references only.

Relevant current files:

- `.laddy/orchestrator/local_merge.py`
- `.laddy/scripts/merge-verified.sh`
- `tests/agent_orchestrator/test_local_merge.py`

## Goal

Before mutating local `main`, require an explicit task-id confirmation for each
merge side effect unless the command is running in documented dry-run mode.
This protects against accidental non-interactive merges and wrong-task merges.

## Non-goals

- No remote push automation changes.
- No change to the deterministic gate criteria.
- No merge from the VPS.
- No legacy `.agent/scripts/agent-flow.sh` behavior.

## Scope

1. Keep `--no-input` as a true dry run: it must never merge or push.
2. For interactive local merges, show the task id and verified sha before
   merging.
3. Require the user to type the exact task id for merge side effects.
4. Preserve the existing L3 risk prompt; task-id confirmation is separate and
   applies before any merge mutation.
5. Tests must use injected confirmation callbacks, not real stdin.

## Acceptance criteria

- A green L1/L2 branch does not merge unless exact task id confirmation passes.
- A wrong confirmation holds the task and leaves local `main` unchanged.
- `--no-input` remains dry-run and does not prompt.
- L3 still requires its existing risk decision and then task-id confirmation.
- The merge uses the already verified sha, not the mutable branch ref.
