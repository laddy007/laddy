---
type: feature
---

# status-dashboard - static read-only dashboard for `.laddy` task state

## Authoritative context

`.laddy/orchestrator/run.py --phase status` is the current status source.
Legacy `.agent/scripts/status-dashboard.sh` is reference material only; do not
port shell code.

Relevant current files:

- `.laddy/orchestrator/run.py`
- `.laddy/orchestrator/artifacts.py`
- `.laddy/orchestrator/flags.py`
- `.laddy/orchestrator/handoff.py`
- `tests/agent_orchestrator/test_run_cli.py`

## Goal

Generate a static, read-only dashboard from current `.laddy` task state so the
Director can inspect specs, derived status, recent action, open flags, and
terminal summaries without running multiple commands.

## Non-goals

- No live web server.
- No write actions from the dashboard.
- No JavaScript dependency.
- No legacy `.agent` shell script port.
- No exposure of secrets, full logs, or raw stack traces.

## Scope

1. Add a `--phase dashboard` CLI path.
2. Generate one static HTML file under `.laddy/tasks/dashboard.html`.
3. Escape all task ids, status text, summaries, and flag content.
4. Dedupe tasks by spec stem.
5. Show derived status from the same helper as `--phase status`.
6. Include recent action/outcome and open flags when available.

## Acceptance criteria

- Dashboard generation is read-only with respect to specs and worktrees.
- Generated HTML escapes `<`, `>`, `&`, quotes, and task-supplied text.
- Dashboard contains every `.laddy/specs/*.md` task exactly once.
- Dashboard does not include full raw logs.
- Tests cover escaping, dedupe, and status parity with `--phase status`.
