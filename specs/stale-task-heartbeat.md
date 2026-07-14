---
type: feature
---

# stale-task-heartbeat - read-only stale task check with best-effort notification

## Authoritative context

Task state is derived from `.laddy` specs, task artifacts, queue state, and the
append-only iteration log. Notifications already live in
`.laddy/orchestrator/handoff.py`.

Relevant current files:

- `.laddy/orchestrator/run.py`
- `.laddy/orchestrator/handoff.py`
- `.laddy/orchestrator/artifacts.py`
- `.laddy/orchestrator/queue.py`
- `tests/agent_orchestrator/test_run_cli.py`
- `tests/agent_orchestrator/test_handoff.py`

## Goal

Add a read-only heartbeat phase that detects tasks stuck in a non-terminal state
for longer than a configured threshold and sends a best-effort notification.

## Non-goals

- No automatic task restart.
- No mutation of task specs or task artifacts.
- No cron/systemd installation.
- No dashboard generation.
- No notification content containing diffs, secrets, stack traces, or paths.

## Scope

1. Add a `--phase heartbeat` CLI path.
2. Reuse the same status derivation rules as `--phase status`.
3. Treat `running`, `queued`, and `in-progress` as non-terminal candidates.
4. Derive age from the latest task log timestamp where possible.
5. Notify only when age exceeds `AGENT_STALE_TASK_MINUTES` or a documented
   default.
6. Keep notification best-effort and non-blocking.

## Acceptance criteria

- Heartbeat exits zero when no stale tasks exist.
- Heartbeat prints stale task summaries for human inspection.
- Heartbeat calls the existing notifier for stale candidates.
- A notifier failure does not make heartbeat fail.
- Terminal tasks (`done`, `pushed`, `failed:*`, `draft`) are ignored.
- Tests cover read-only behavior by asserting no spec or artifact file changes.
