# Agent guide: engine vs. target, and env footguns

Read this when writing engine code, editing target policy, or hitting a
python/env footgun. Core rule (CLAUDE.md, Core invariants -- Engine is not the
target): the engine is target-agnostic; product policy belongs to the target,
never to engine Python.

## The boundary

- The engine holds **no product code**. Product policy lives in
  `<target>/.laddy/policy.toml` -- sensitive/security/invariant paths, coverage
  package, frontend gate. `orchestrator/target_policy.py` loads it with **no
  built-in fallback**: a missing or malformed file **fails the merge closed**.
- Engine-generic sensitive globs (`ENGINE_SENSITIVE_GLOBS`) always apply and a
  target **cannot weaken** them; a target only *adds* its own product surface.
  The off-VPS merge check reads `policy.toml` from trusted `main`, so a task
  branch cannot downgrade its own classification.
- **Roles and prompts load from the engine**, never from the untrusted target
  -- a target cannot inject a persona into the loop.
- `.laddy/` in this repo is laddy **dogfooding itself** as its own target
  (`policy.toml`, `specs/`, `tasks/`, `docker/`, `security/`).

## Python & env

- **Python 3.11+ required** -- `target_policy.py` imports `tomllib`.
  `pyproject.toml` targets py311. This machine's default `python3` is 3.10, so
  `env.local` sets `PYTHON_BIN=python3.11`; a bare `python3` crashes on import
  before argparse even runs.
- `env.local` (local node) / `env.vps` (VPS) are copied from their `.example`
  files and git-ignored. They carry the per-node knobs (`PYTHON_BIN`,
  `SETUP_COMMANDS`, `TEST_COMMANDS`, `CLAUDE_CMD`, `CODEX_CMD`, remotes). Bash
  only exports these; all interpretation happens in `orchestrator/config.py`.

## Engine-wide coding conventions

- **LF + ASCII-safe** source and specs (`.gitattributes` pins `eol=lf`).
- **Injected clock** -- no bare `datetime.now` / `time.sleep` in logic paths;
  time and sleep are injected so tests never really wait.
- **Typed models over freeform dicts** at boundaries.
- **Files split rather than grow** -- extract a module when a change adds a
  second responsibility.
- **Prefer the standard library** -- new third-party deps are scrutinized (see
  `CONTRIBUTING.md`).
