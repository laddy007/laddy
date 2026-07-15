---
type: feature
status: done
---

# claude-md-and-guides — agent-facing engine documentation

## Goal

Give AI agents that develop the **laddy engine** on the Director's trusted
machine a small, load-bearing set of behavior rules, modelled on the proven
gps42 layout (rich `CLAUDE.md` + thin `AGENTS.md` pointer + a handful of
situational guides), scaled down to laddy's surface.

The current `CLAUDE.md` is 34 lines of three Director-side gotchas. It is not
a general orientation for an agent making engine changes: it omits the trust
model, the merge tiers, the engine-vs-target boundary, and the coding
invariants that a change here must respect.

## Non-goals

- No change to engine behavior, tests, scripts, or policy. This is
  documentation only.
- No `docs/` tree. laddy deliberately keeps design rationale in commit
  history + inline comments (`USAGE.md`); these files are agent *rules*, not
  a design-doc tree.
- No volatile repository state in the docs (module lists, test counts,
  backlogs). Those drift; they live in code, tests, and `TODO.md`.
- Not a rewrite of `README.md` / `USAGE.md` / `SECURITY.md`. The docs point
  to them; they stay the how-to and the risk model.

## Audience & the two contexts (load-bearing distinction)

These files govern **agents developing the engine** on the trusted machine
(this kind of assisting session). They do **not** govern the loop's internal
agents (developer / rw1 / rw2 / senior / security), which operate over a
**target** repo under that target's `.laddy/` config and the `roles/`
personas — not under this `CLAUDE.md`. The header states this explicitly so an
agent never mistakes engine-dev rules for loop-agent rules.

## Design rules for the docs (inherited from gps42)

- **Rules of behavior, not repository state.** A duplicated volatile fact is a
  bug in the file — it will drift.
- **Only what the agent must know _before_ acting** (invariants, authority,
  footguns) lives in `CLAUDE.md`. Situational detail lives in `CLAUDE/*.md`,
  read when entering that situation.
- **English by design** — engineering rules + cross-vendor (Claude / Codex)
  readability. (Conversation with the Director stays Czech; that is a
  user-level rule, not repeated here.)
- Stands **on top of** user-level `~/.claude/CLAUDE.md`; this file extends and,
  where stated, overrides for laddy.

## File structure

```
CLAUDE.md              rewritten — behavior rules only (~160-200 lines)
AGENTS.md              new — 3-line pointer to CLAUDE.md (Codex & other agents)
CLAUDE/
  trust-and-merge.md   new — VPS/local split, hub, L1/L2/L3, tripwire, push
  gate.md              new — docker gate, scanners, coverage, oracle pointer
  engine-vs-target.md  new — policy.toml boundary, py3.11, LF/ASCII, clock
```

The current `CLAUDE.md`'s three gotchas are **redistributed**, not dropped:

- never auto-push to GitHub → `CLAUDE.md` action-authority + `trust-and-merge.md`
- `python3.11` / `tomllib` / `PYTHON_BIN` → `CLAUDE.md` dev-commands +
  `engine-vs-target.md`
- WSL `docker-credential-desktop.exe` fix → `gate.md`

## `CLAUDE.md` — section outline

1. **Header** — what the file is; the two-contexts distinction above;
   "rules of behavior, not repository state"; sits on top of user-level
   `~/.claude/CLAUDE.md`.
2. **Situational guides** — a table: *situation → read first*. Rows: merging /
   before a merge decision → `trust-and-merge.md`; touching the gate, docker,
   or scanners → `gate.md`; writing engine code, target policy, or hitting the
   python/env footguns → `engine-vs-target.md`.
3. **Development commands & environment** — `PYTHON_BIN=python3.11`
   (default `python3` is 3.10 and crashes on `tomllib`); per-worktree venv;
   full local gate `ruff check . && basedpyright && pytest -n auto -q`;
   single-test form; `env.local` copied from example (git-ignored).
4. **Architecture (short)** — the engine drives a loop of AI agents
   (developer → fast tests → rw1 → rw2, escalation to senior) over a *target*
   repo; `orchestrator/` = deterministic policy/state/decisions, `roles/` =
   personas, the oracle = post-merge escape-rate measurement. The engine holds
   no product code of its own. (Navigation, not authority — no module list to
   drift.)
5. **Authoritative sources (precedence)** — Director's chat supreme; then the
   current task/spec; `specs/` (some legacy pre engine/target split — history
   only); **tests are the executable spec** (`tests/`); `README.md` /
   `USAGE.md` / `SECURITY.md` / `CONTRIBUTING.md` as reference. No `docs/` tree
   by design.
6. **Core invariants (hold in memory always)** —
   - Trust direction: never push to GitHub; the VPS never writes `main`.
   - Engine ≠ target: product policy lives in `<target>/.laddy/policy.toml`;
     engine-generic guards fail **closed** and cannot be weakened by a target.
   - Append-only / derive, don't store: state is replayed from the append-only
     `iteration-log.jsonl`; do not add stored status.
   - Files don't grow, they split.
   - Injected clock: no bare `datetime.now` / `time.sleep` in logic paths —
     time and sleep are injected so tests never really wait.
   - LF everywhere + ASCII-safe source (`.gitattributes` `eol=lf`).
   - Typed models over `dict[str, Any]` / freeform JSON.
7. **Action authority — laddy specifics** — see the dedicated section below;
   `CLAUDE.md` carries the summary table, `trust-and-merge.md` the detail.
8. **Definition of done — laddy** — for the touched scope: `ruff check .`
   clean, `basedpyright` **0 errors** (warnings are non-blocking by design),
   `pytest -n auto -q` green; LF/ASCII preserved; clock injected. Docs-only /
   typo / trivial-config fall under the user-level TDD-light exception.

## Action authority — the verified model (Director clarification 2026-07-15)

Verified against `scripts/merge-verified.sh`, `scripts/kickoff.sh`,
`orchestrator/local_merge.py`, and `specs/merge-safety-confirmation.md`. The
rule governs an agent's **discretionary** git actions — not the orchestrator's
own automated commits when the Director deliberately runs the loop (VPS or
local rehearsal); that is the product doing its job.

**Never push to GitHub (any agent, any node).** `git push` to `origin` /
GitHub is Tier 3. The final "push local `main` to GitHub" is the Director's
explicit foreground `y/N` decision; an agent never runs it, never pre-answers
it, never backgrounds the script that prompts it.

**On the VPS worker (unattended, disposable, no GitHub credential):** full git
autonomy — the loop commits and pushes **task branches** to the local bare hub
freely, running `--dangerously-skip-permissions`. Hard limit: it **never
writes `main`** (hub or local). The `merge-verified` tripwire aborts the whole
run if hub `main` is not an ancestor of local `main`.

**On the local trusted machine (agent developing the engine):**

- **Commit → ask the Director first.** This intentionally **overrides** the
  user-level default ("commit silently, run `git add`/`git commit` without
  asking"). Rationale: the engine governs everyone's merges; changes on the
  trusted machine are higher-stakes, so a human sees each commit. The override
  is stated as an override, not a silent contradiction.
- **Merge into local `main` → always confirm.** Already structurally enforced:
  `merge-verified` requires typing the exact task id before any merge
  side-effect (`--no-input` stays a true dry run). The agent never merges by
  any other path and never bypasses that prompt.
- **Push to GitHub → never** (Tier 3, Director-run, foreground).

## `CLAUDE/trust-and-merge.md` — outline

Read before any merge decision or when reasoning about where work may flow.

- Two nodes, one direction of trust: VPS worker (no GitHub credential, no gh
  CLI, agent-CLI auth + ntfy only) vs. local trusted machine (only node with a
  GitHub credential; `origin` = GitHub, `laddy` remote = VPS bare hub).
- Hub is a **closed namespace**: every branch except `main` is a task id;
  task branch = bare task id, appears locally as `laddy/<task>`.
- Merge flow: VPS pushes `<task>` to hub → Director runs `merge-verified.sh`
  from inside the target repo → per branch it re-derives every gate on trusted
  infra and merges the **verified sha** (not the mutable branch ref) into local
  `main`, or holds with a digest.
- Blast-radius tiers: **L1** safe (docs/i18n) → auto-merge after mechanical
  gates; **L2** ordinary logic → agents are the gate (rw2 + security panel),
  auto-merge if green; **L3** sensitive (auth, migrations, `models.py`, engine
  surface, `policy.toml`, `CLAUDE.md`, deps) → never auto-merge, digest → human
  Y/N.
- Tripwire: hub `main` must be an ancestor of local `main`, else the whole run
  aborts — the VPS must never have written `main`.
- Final GitHub push: the separate explicit `y/N` at the end of the merge flow;
  Tier 3; never automatic, never pre-answered, never backgrounded.

## `CLAUDE/gate.md` — outline

Read when touching the gate, the docker test setup, or the scanners.

- Fast inner gate vs. the authoritative containerized `DockerGate`
  (`testgate.py`). The container gate is the binding correctness gate on the
  trusted machine.
- `docker/Dockerfile.test` + `docker/compose.test.yml`: the gate command is
  **injected** by `testgate.py` (`eval "$GATE_COMMAND"`), not hardcoded in
  compose — policy stays in Python.
- Scanners: semgrep + gitleaks + diff-coverage. **Pinned versions; no autopilot
  bumps** — a bump is a deliberate, reviewed change. `pip check` fails the
  image build on dependency inconsistency.
- WSL footgun: a gate that fails instantly with
  `docker-credential-desktop.exe: exec format error` means a leftover
  `"credsStore": "desktop.exe"` in `~/.docker/config.json` — remove that key
  (empty `auths: {}` is fine for anonymous public pulls).
- Oracle (pointer): a post-merge, **non-blocking** subsystem measuring the
  gates' escape rate; it never blocks a merge. Runbook in `USAGE.md`. (Split
  into its own `oracle.md` guide only if it later needs active agent guidance.)

## `CLAUDE/engine-vs-target.md` — outline

Read when writing engine code, editing target policy, or hitting env footguns.

- The engine is **target-agnostic**. Product policy lives in
  `<target>/.laddy/policy.toml` (sensitive/security/invariant paths, coverage
  package, frontend gate). Engine-generic globs **cannot be weakened** by a
  target; a missing toml **fails closed**.
- Roles and prompts are loaded from the **engine**, never from the untrusted
  target — the target cannot inject a persona.
- `.laddy/` here is laddy **dogfooding itself** as its own target
  (`policy.toml`, `specs/`, `tasks/`, `docker/`, `security/`).
- python 3.11+ required (`tomllib`); `pyproject.toml` targets py311; the box
  default `python3` is 3.10, so `env.local` sets `PYTHON_BIN=python3.11` — a
  bare `python3` crashes on import before argparse runs.
- Engine-wide coding conventions: LF + ASCII-safe source; injected clock (no
  bare `datetime.now`/`time.sleep` in logic); typed models over freeform dicts;
  files split rather than grow.

## `AGENTS.md` — content

Three lines: read `CLAUDE.md` before any work; the binding repo-specific agent
rules live there; applies to Claude Code, OpenAI Codex, and any other agent.

## Acceptance criteria

- `CLAUDE.md` contains no volatile repository state (no module lists, no test
  counts) — only rules, invariants, authority, and pointers.
- The two-contexts distinction (engine-dev agent vs. loop agent) is stated in
  the `CLAUDE.md` header.
- The action-authority section states all three: never push to GitHub; VPS
  full autonomy but never `main`; local commit/merge require asking, with the
  user-level commit-silently default explicitly marked as overridden.
- Every situational-guide row in `CLAUDE.md` resolves to an existing file in
  `CLAUDE/`, and each guide opens by naming its trigger situation.
- The three current gotchas are all present in the new structure (none lost).
- `AGENTS.md` exists and is a thin pointer.
- All new/changed files are LF + ASCII-safe (repo `.gitattributes`).
- No engine code, test, script, or policy file is modified.
```

## Decisions (resolved)

- Spec location: `specs/` (laddy has no `docs/` tree).
- `oracle.md`: folded as a pointer inside `gate.md`; split out only on request.
