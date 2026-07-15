# CLAUDE.md -- laddy engine, agent rules

This file gives AI agents (Claude Code, OpenAI Codex, others) the rules of
behavior for working on the **laddy engine**. `AGENTS.md` is a thin pointer
here. Universal agent rules live in user-level `~/.claude/CLAUDE.md`; this file
extends and, where stated, overrides them for laddy.

**Who this governs.** These rules are for an agent *developing the engine* on
the Director's trusted machine (this kind of assisting session). They do NOT
govern the loop's own internal agents (developer / rw1 / rw2 / senior /
security): those run over a **target** repo, under that target's `.laddy/`
config and the engine `roles/` personas -- not under this file.

**Design rules for this file.** It carries rules of behavior, not repository
state. Volatile facts (module lists, test counts, backlogs) live in code,
tests, and `TODO.md` -- a duplicated volatile fact here is a bug: it drifts.
It carries only what the agent must know *before* acting (invariants,
authority, footguns); situational detail lives in `CLAUDE/*.md`, read on entry
to that situation. English by design (engineering rules + cross-vendor
readability); the Director conversation stays Czech per the user-level file.

## Situational guides -- read before acting in these situations

| Situation | Read first |
|---|---|
| A merge decision, or reasoning about where work may flow | `CLAUDE/trust-and-merge.md` |
| Touching the gate, the docker test setup, or the scanners | `CLAUDE/gate.md` |
| Writing engine code, editing target policy, or an env/python footgun | `CLAUDE/engine-vs-target.md` |

## Development commands & environment

The flow runs under WSL / Linux (bash). The default `python3` on this machine
is 3.10, which crashes on `tomllib`; the engine needs **3.11+**, so `env.local`
sets `PYTHON_BIN=python3.11`.

```bash
# Per-worktree venv (PEP 668 forbids a system-wide pip install).
python3.11 -m venv .venv && .venv/bin/pip install -q -r requirements-dev.txt

# Full local gate (the deterministic correctness bar for a change).
. .venv/bin/activate && ruff check . && basedpyright && pytest -n auto -q

# Quick suite / single test.
python -m pytest -q
python -m pytest tests/test_<module>.py::test_<name> -q
```

`env.local` and `env.vps` are copied from their `.example` files and are
git-ignored. Never Read-tool a real `env.*` with secrets; the `.example`
files are fair game.

## Architecture (navigation, not authority)

The engine drives a loop of AI agents -- developer -> fast tests -> reviewer 1
(Claude) -> reviewer 2 (cross-vendor) -- bouncing on failure or change-request
until convergence, escalating a deadlock to a senior reviewer. It runs over a
**target** repo and holds no product code of its own.

- `orchestrator/` -- the engine: deterministic policy, state, and decisions.
- `roles/` -- the agent personas fed into each LLM call (engine resources).
- `orchestrator/oracle/` -- post-merge, non-blocking measurement of the gates'
  escape rate; it never blocks a merge.

## Authoritative sources (order of precedence)

The Director's explicit instructions in chat are always supreme. Otherwise:

1. **Current task / spec** -- the operational scope of present work.
2. **`specs/`** -- feature specs. Some pre-date the engine/target split and
   are history only; a live target's specs live at `<target>/.laddy/specs/`.
3. **Tests** -- `tests/` is the executable spec of the engine. Untested
   behavior is undefined.
4. **`README.md` / `USAGE.md` / `SECURITY.md` / `CONTRIBUTING.md`** -- how-to,
   runbook, risk model, and contribution rules.

There is no `docs/` tree by design: rationale lives in commit history and
inline comments. Do not add one.

## Core invariants (hold in memory always)

- **Trust direction.** Never push to GitHub (any agent). The VPS worker never
  writes `main` -- hub or local. Detail: `CLAUDE/trust-and-merge.md`.
- **Engine is not the target.** Product policy lives in
  `<target>/.laddy/policy.toml`. Engine-generic guards fail **closed** and a
  target cannot weaken them. Detail: `CLAUDE/engine-vs-target.md`.
- **Derive, don't store.** State is replayed from the append-only
  `iteration-log.jsonl`; do not add a stored status that could drift from the
  log.
- **Files don't grow, they split.** A change that adds a second responsibility
  extracts a module instead of appending.
- **Injected clock.** No bare `datetime.now` / `time.sleep` in logic paths --
  time and sleep are injected so tests never really wait.
- **LF everywhere, ASCII-safe source.** `.gitattributes` pins `eol=lf`; keep
  source and specs ASCII so they survive every terminal and agent vendor.
- **Typed models over freeform payloads.** Prefer explicit typed models to
  `dict[str, Any]` / freeform JSON at boundaries.

## Action authority -- laddy specifics

Tiers and principles are in user-level `~/.claude/CLAUDE.md`. These rules
govern an agent's **discretionary** git actions -- not the orchestrator's own
automated commits when the Director deliberately runs the loop (VPS or local
rehearsal), which are the product doing its job.

- **Never push to GitHub** (any node). `git push` to `origin` / GitHub is
  Tier 3. The final "push local `main` to GitHub" is the Director's explicit
  foreground `y/N` decision -- never run it, never pre-answer it, never
  background the script that prompts it.
- **On the VPS worker** (unattended, disposable, no GitHub credential): full
  git autonomy -- commit and push **task branches** to the local bare hub
  freely. Hard limit: never write `main`. The `merge-verified` tripwire aborts
  the whole run if hub `main` is not an ancestor of local `main`.
- **On the local trusted machine** (developing the engine):
  - **Commit -> ask the Director first.** This intentionally **overrides** the
    user-level default ("commit silently"). The engine governs everyone's
    merges; a change on the trusted machine is higher-stakes, so a human sees
    each commit.
  - **Merge into local `main` -> always confirm.** Already enforced:
    `merge-verified` requires typing the exact task id before any merge
    side-effect (`--no-input` stays a true dry run). Never merge by another
    path; never bypass that prompt.

## Definition of done -- laddy

In addition to the universal DoD (user-level file), for the touched scope:

- `ruff check .` clean and `basedpyright` at **0 errors** (its pre-existing
  warnings are non-blocking by design -- 0 errors is the gate),
- `pytest -n auto -q` green,
- LF + ASCII-safe preserved, clock injected where time is involved,
- a behavior change ships with tests (untested behavior is undefined).

Docs-only / typo / trivial-config changes fall under the user-level TDD-light
exception.
