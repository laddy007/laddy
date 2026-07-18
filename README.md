# laddy

**Disclaimer:** This is an independent, community project. It is **not affiliated with, endorsed by, or sponsored by Anthropic**. "Claude" and "Claude Code" are trademarks of Anthropic PBC. This tool requires you to bring your own Claude subscription/API credentials and shells out to the official `claude` CLI — it does not read, store, or transmit your credentials.

**Before you run this: read [SECURITY.md](SECURITY.md).** The agent runs unattended with `--dangerously-skip-permissions`, but only inside a disposable VPS sandbox with no access to anything outside itself (no GitHub credential, no other secrets). Your local machine — where you review and merge — keeps normal permissions throughout; merging is a human decision by default (an auto-merge path exists, gated by a full local re-verification).

Autonomous dev loop: you write a small spec, a VPS worker (developer +
two reviewer agents) implements and converges on it, pushes a task
branch to its own **local bare hub on the VPS**, and you merge it
locally with a re-verified, trusted gate. `laddy` is a standalone
engine repo — it holds no product code of its own; it is installed
per VPS user and pointed at a **target** project repo (e.g. `myapp`).

Full narrative + troubleshooting: `USAGE.md`. This file is the
practical "how do I run it" reference.

---

## Topology (GitHub-free VPS)

Two nodes, one direction of trust. The VPS never holds a GitHub
credential of any kind — no deploy key, no `gh`, no clone of a
`github.com` remote. It reaches the outside world only through the
Director's own machine.

```
 LOCAL (your machine) ─────────────────────────────────────────────────
   origin = GitHub                    the ONLY node with a GitHub credential
   laddy  = ssh://<alias>/home/<user>/repo_<project>/hub.git   (per target)

   scripts/upgrade_laddy.sh <user>   engine (this repo) -> ~/laddy on the VPS
   scripts/push-hub.sh <user>        target main -> hub (seed / keep current)
   ssh <alias> '~/laddy/scripts/kickoff.sh <task>'   kick off a task
   scripts/merge-verified.sh         re-verify + merge laddy/<task> -> local main
   git push origin main              publish the merge to GitHub
                        │                                       ▲
                        │ ssh (kickoff / watch)                 │ git push <task>
                        ▼                                       │
 VPS (unprivileged user, e.g. "laddy") ─────────────────────────────────
   ~/laddy                    engine checkout (promoted by upgrade_laddy.sh)
   ~/repo_<project>/hub.git   bare hub — the ONLY place task branches live
                              before you merge them
   AGENT_WORK_ROOT/wt/<task>  per-task worktree: developer -> rw1 -> rw2 loop
   NO GitHub credential, NO deploy key, NO gh CLI — only the agent-CLI
   auth (claude/codex) and an ntfy topic are permitted on this box.
```

A task branch is the bare task id (e.g. `mytask`), never `agent/<task>`
— the hub is a **closed namespace**: every branch except `main` is a
task. Locally, once the `laddy` remote exists (`push-hub.sh` adds it),
those branches show up as `laddy/<task>` (a remote-tracking namespace;
the flow never checks out or switches your own branch).

---

## Quick start

**First use — once per VPS user (Director):**

```bash
scripts/vps-onboard.sh                # interactive: root bootstrap (user, docker,
                                       # cgroup slice) + per-user bare hub + empty
                                       # engine checkout + env.vps, over SSH
scripts/upgrade_laddy.sh laddy        # promote this engine repo's local main
                                       # into that user's ~/laddy (all-or-nothing
                                       # preflight across every LADDY_USERS entry)

# from the TARGET project's own local checkout (e.g. ~/myapp):
scripts/push-hub.sh <user>            # add the `laddy` remote + seed the hub
                                       # with the target's main (idempotent)
```

**Every task after that:**

```bash
ssh vps-laddy '~/laddy/scripts/kickoff.sh <task>'   # clarify gate, then detaches

# on your machine, from the TARGET repo:
scripts/merge-verified.sh                           # re-verify + merge, on YOUR machine
git push origin main                                # publish to GitHub
scripts/push-hub.sh <user>                          # keep the hub's main current
                                                      # (next kickoff clones from it)
```

`scripts/merge-verified.sh` lives in this engine repo but is **run from
inside the target repo** (it operates on your current working directory,
`--repo .`) — the engine's own checkout location is irrelevant to which
repo gets merged into.

---

## What lives where

```
orchestrator/   the engine (Python; deterministic policy, gates, state)
roles/          agent prompts: developer, rw1, rw2, security,
                senior-reviewer, explorer, debugger, verify, investigator
scripts/        thin launchers only — all decisions live in Python:
                  kickoff.sh          VPS entrypoint (clarify -> design -> detached loop)
                  merge-verified.sh   LOCAL merge authority (trusted machine)
                  local-task.sh       whole loop locally (no VPS), rehearsal
                  smoke-review-cli.sh preflight of the least-privilege review CLIs
                  watch-vps.sh / colorize-log.sh   tail + colorize a running task's log
                  vps-onboard.sh      one-shot, per-user VPS bootstrap (bare-hub model)
                  upgrade_laddy.sh    promote this repo's main into each user's ~/laddy
                  push-hub.sh         seed / keep-current the target's hub (main -> hub)
                  lib/laddy_users.sh  shared LADDY_USERS parsing (onboard/upgrade/push-hub)
skills/         interactive helpers (Claude Code skills):
                  create-spec/        co-author a task spec -> <target>/.laddy/specs/<task>.md
                  investigate/        diagnosis-only session -> investigations/
docker/         containerized gate config (compose + image) — the template a
                new target's `.laddy/docker/` is seeded from
security/       semgrep ruleset the gate runs — template for `.laddy/security/`
monitoring/     lightweight VPS host/Docker/process monitor + systemd install
oracle/         escape-class registry (`classes.md`) for the self-improvement oracle
specs/          legacy task specs from before the engine/target split (history
                only — a live target's specs live at `<target>/.laddy/specs/`)
setup.md        VPS self-setup prompt (paste into claude on a fresh box)
vps.conf.example   onboarding + promotion config template (LADDY_USERS schema)
env.*.example   per-node config templates (env.local / env.vps, git-ignored)
```

Every **target** project (e.g. `myapp`) carries its own
`<target>/.laddy/{specs,tasks,docker,security,policy.toml}` — specs you
author, per-task artifacts the loop commits, a copy of the containerized
gate config it runs under, and `policy.toml` (the per-target merge policy:
sensitive/security/invariant paths, coverage package, frontend gate — the
engine holds no product-specific policy of its own). Onboarding a new
project seeds that directory once; see `setup.md`.

**Skills activation (in the target repo):** Claude Code discovers
skills in `.claude/skills/`, so symlink or copy them once from wherever
this engine is checked out locally:
`ln -s <engine>/skills/create-spec .claude/skills/create-spec`
(same for `investigate`).

---

## How to run it

### 0. One-time setup

Onboarding a VPS user or adding a new project = the same operation
(spec: "switching / adding a project"). Full detail: `setup.md`.

- `scripts/vps-onboard.sh` drives a fresh VPS over SSH: root bootstrap
  (unix user, docker, cgroup slice) plus, per `LADDY_USERS` entry, a
  bare hub, an empty engine checkout, `env.vps`, and your push pubkey
  in that user's `authorized_keys`. It never touches GitHub and never
  seeds engine or target content.
- `scripts/upgrade_laddy.sh [user...]` then promotes this repo's local
  `main` into `~/laddy` for each user — all-or-nothing: if any named
  user's loop is running or its checkout is dirty, **nothing** is
  upgraded.
- You seed the target's `main` onto the hub with `scripts/push-hub.sh
  <user>` (run from inside the target repo — it adds the `laddy` remote
  from `vps.conf` and pushes `main`, idempotently). `upgrade_laddy.sh`
  never does this; it only ever promotes the engine.

On your local trusted machine you only need Docker running plus `git`,
`python3`, and the `claude`/`codex` CLIs — the merge gate (lint, types,
tests, diff-coverage, `semgrep`, `gitleaks`) runs fully containerized
against its own throwaway Postgres. Copy `env.local.example` →
`env.local` if you want non-default knobs.

### 1. Write a spec and kick off the VPS

Author the spec either with the **`create-spec` skill** (interactive
Claude Code session on your machine, committed + pushed to the hub
before kickoff) or directly on the VPS during kickoff (`--new` co-writes
it in the terminal, inside the fresh task worktree — no local push
needed first):

```bash
ssh vps-laddy '~/laddy/scripts/kickoff.sh <task>'          # spec already on the hub's main
ssh vps-laddy '~/laddy/scripts/kickoff.sh <task> --new'    # co-write the spec interactively
```

A **clarify gate** runs interactively right after kickoff (answer any
blocking questions), then a **design gate** (foreground, high-risk
tasks only), then the loop **detaches** — it survives an SSH drop.
Watch it (colorized via `colorize-log.sh`):

```bash
scripts/watch-vps.sh <task>                 # from your machine, or:
ssh vps-laddy 'tail -f ~/agent-logs/<task>.log'
```

Loop: developer → fast tests → reviewer 1 (Claude) → reviewer 2 (Codex),
bouncing back on failure/change-request until it converges (max 4
rounds, escalates to a senior reviewer on repeated deadlock). One ntfy
push fires on the terminal state. It ends either pushed to the hub as
`<task>` (ready to merge) or `CAP_REACHED` / `ESCALATED_DEADLOCK`
(nothing pushed — read `handback.md` on the branch, refine the spec,
retry).

### 2. Merge it — on your machine, not the VPS

Run from inside the **target repo**, not the engine checkout:

```bash
scripts/merge-verified.sh              # every ready branch on the hub
scripts/merge-verified.sh mytask       # or specific tasks
```

"Ready" = pushed to the hub AND carries a committed
`.laddy/tasks/<task>/merge-decision.json` (the loop reached a terminal
push). For each ready branch this recomputes everything on **your
trusted machine**: full test suite + coverage + semgrep + gitleaks,
plus a cross-vendor reviewer re-run and a security panel. Then, by
blast radius:

- safe / ordinary change, all gates green → **merges** into local `main`
  after you confirm by typing the exact task id (anything else declines).
- touches a sensitive surface (auth, migrations, `models.py`, deploy, …),
  gates green → prints what's sensitive + a summary; type the exact task id
  to merge (anything else declines).
- anything red (test/coverage/scanner/reviewer) → **holds**, prints what
  failed and why, never offers to merge (fix on the VPS and re-run).

Before any of this runs, it fetches the hub and checks a **tripwire**:
if the hub's `main` is not an ancestor of your local `main`, the VPS
wrote (or something wrote) where only you may write — the whole run
aborts, nothing is merged. See `USAGE.md` for what to do when that
fires.

When something merged, it asks `push main to origin and delete N merged
branch(es)? (y/N)`. `y` pushes your `main` to **GitHub** and deletes the
now-merged branches from the hub; `N` leaves it local. Either way, also
push your `main` to the hub yourself afterward (`scripts/push-hub.sh
<user>`) — the next `kickoff.sh` clones its base from the hub, so a
stale hub `main` means the next task starts from old code. Add
`--no-input` for a dry run (holds every sensitive change, never pushes).

---

## Status / troubleshooting / Codex vs Claude

See `USAGE.md` for the full walkthrough, the trust-model rationale, and
a troubleshooting table (missing spec, deadlocked task, tripwire fired,
"no longer applies cleanly" branch, etc).

## Reference

- **Practical guide:** `USAGE.md`.
- **VPS onboarding / new project:** `setup.md` (paste as a task on a
  fresh VPS), or `scripts/vps-onboard.sh` to run it yourself over SSH.
- **Roles (agent prompts):** `roles/`.
- **Skills (interactive helpers):** `skills/create-spec`, `skills/investigate`.
- **The engine:** `orchestrator/` (Python).
- **VPS monitoring:** `monitoring/README.md` (install, analysis, overhead,
  retirement of the temporary loop, rollback).
- **Pre-flight check:** `scripts/smoke-review-cli.sh` runs the real
  least-privilege review commands against a trivial read-only prompt on
  whichever of `claude`/`codex` is on PATH — confirms the local review
  panel actually runs to completion before you trust it on a real merge
  decision.
