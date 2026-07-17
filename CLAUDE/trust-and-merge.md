# Agent guide: trust model & merge authority

Read this before any merge decision, or whenever reasoning about where work is
allowed to flow. Core rule (CLAUDE.md, Core invariants -- Trust direction): the
VPS does the work, the trusted machine grants trust, and the two are wired so
you never have to trust the VPS. Full risk model: `SECURITY.md`. Operator
how-to: `README.md`, `USAGE.md`.

## Two nodes, one direction of trust

- **VPS worker** -- unprivileged, disposable. Holds **no** GitHub credential,
  no deploy key, no `gh` CLI: only the agent-CLI auth and an ntfy topic. Runs
  the loop unattended with `--dangerously-skip-permissions`. It has an engine
  checkout and a **bare hub** (`repo_<project>/hub.git`).
- **Local trusted machine** (Director) -- the ONLY node with a GitHub
  credential. `origin` = GitHub; a `laddy` remote points at the VPS bare hub.
  Runs `merge-verified.sh`, does the GitHub push, keeps normal permissions.

The binding gate (full test re-run + reviews + security panel) runs on the
trusted machine, where the agent that wrote the code cannot reach it.

## The hub is a closed namespace

Every branch on the hub except `main` **is** a task id. A task branch is the
bare task id (e.g. `mytask`), never `agent/<task>`. Locally these appear as
`laddy/<task>` remote-tracking refs; the merge flow never checks out or
switches your own branch.

## Merge flow

1. The VPS pushes `<task>` to the hub.
2. The Director runs `scripts/merge-verified.sh` **from inside the target
   repo** (its `origin`/`laddy` remote wiring, not the engine's location,
   decides which repo is integrated).
3. Per ready branch, `orchestrator.local_merge` re-derives every gate on
   trusted infra in a sibling worktree and either merges the **verified sha**
   (not the mutable branch ref) into local `main`, or holds with a digest.
4. Merging into local `main` requires typing the **exact task id**
   (merge-safety confirmation). `--no-input` stays a true dry run: it never
   merges and never prompts.

## Blast-radius tiers (L1 / L2 / L3)

`orchestrator/policy.py` routes a diff by blast radius, not by "can a human
read it". The decision is recomputed locally from the actual diff -- the
VPS-written `merge-decision.json` is never trusted.

- **L1 safe-by-construction** -> auto-merge after mechanical gates, no review.
  L1 is an allowlist of **inert file classes** (docs / data), never a
  directory allowlist: any non-inert file falls through to L2. Deliberately
  conservative -- code is never safe-by-construction, so even an added test is
  L2, not L1 (closing a fail-open hole under the "branch is
  attacker-controlled" threat model).
- **L2 ordinary logic** -> the agents ARE the gate (tests + cross-vendor rw2 +
  security panel); auto-merge if green.
- **L3 sensitive / security surface** -> security panel -> digested risk ->
  human `Y/N`. Never auto-merge.

The sensitive surface is the target's `policy.toml` plus the engine-wide
constants (`target_policy.ENGINE_SENSITIVE_GLOBS`): secrets, `.claude`/`.mcp`
agent config, and the **whole engine surface** (`orchestrator/`, `scripts/`,
`roles/`, `prompts/`, `oracle/`, `monitoring/`, `docker/`, `security/`,
`skills/`). **When laddy dogfoods itself, almost every code change is L3** --
only markdown/docs ride L1. Editing `policy.toml` is itself L3, and the merge
check reads it from trusted `main`, so a task branch cannot weaken its own
classification.

## The tripwire

Every `merge-verified.sh` run fetches the hub and **aborts the whole run** if
hub `main` is not an ancestor of local `main`. The VPS must never have written
`main`; if it did, the flow refuses to proceed rather than integrate a tampered
base.

## The GitHub push

Pushing local `main` to GitHub (and deleting merged hub branches) is a
**separate, explicit `y/N`** at the end of the flow -- Tier 3. Never automatic,
even right after a clean local merge. Run it in the foreground so the prompt
reaches a real user; never pre-answer it (`echo y | ...`) and never background
or pipe the script in a way that detaches stdin.
