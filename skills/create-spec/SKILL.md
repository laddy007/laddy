---
name: create-spec
description: Co-author a task spec for the myapp agent dev-loop and save it
  to .laddy/specs/<task>.md, ready for kickoff. Use when the Director wants
  to prepare a dev-loop task ("napiš spec", "připrav task pro agenta",
  "create a spec for X"). Replaces the retired create-spec.sh; the same
  flow also exists on the VPS as `kickoff.sh <task> --new`.
---

# create-spec — dev-loop spec authoring

You are co-authoring a task spec named `<task>` (ask for the name if not
given; it must match `^[A-Za-z0-9._-]+$` and `.laddy/specs/<task>.md` must
not already exist).

First run `/superpowers:brainstorming` and use it to explore intent, scope
and requirements with the Director before writing anything.

Context:

- The spec is executed later by the autonomous dev-loop (developer + two
  reviewer agents) on a VPS, so it must be fully self-contained. Read
  `.laddy/roles/developer.md` and an existing spec under `.laddy/specs/`
  for the expected shape and level of detail.
- The spec must cover: goal, scope (in and out), constraints, the files or
  areas involved, and a `## Acceptance criteria` section. Leave no open
  question for the agent to guess.
- In `## Acceptance criteria`, every criterion is a single testable statement
  — input / state -> expected observable result — not prose. Each becomes
  a test the developer must write and the reviewers check. Phrase prose
  guarantees AS tests: not "the reporter always exits 0" but "running
  `--phase flags` with an unreachable origin exits 0 and lists local flags";
  not "resolve writes nothing on a bad id" but "resolving an unknown id
  returns failure and creates no file". If a behavior cannot be phrased as a
  test, it is not yet an acceptance criterion — clarify it until it can.
- Optional front matter selects the task type:
  `type: feature | bug | spike | audit | investigate` (audit/investigate
  are report-only tasks).
- Auto-classify risk: if the agreed scope touches loop-tooling or
  policy-sensitive paths (`.laddy/orchestrator/`, `.laddy/roles/`,
  `.laddy/scripts/`, deploy / secret / CI files), stamp `risk: high` in the
  front matter. A high-risk task runs a mandatory design-approval gate at
  kickoff (`--phase design`), so this must not be left to chance — set it
  whenever the scope qualifies, never ask the Director to opt in.
- Never mark a spec `status: ready` on the Director's behalf, and never
  write `status: draft-proposal` unless the Director asks — kickoff refuses
  drafts by design.

When you agree on the spec, WRITE it to exactly `.laddy/specs/<task>.md`.

Then tell the Director it is ready to dispatch:

1. commit + push the spec to `main` (worker clones from GitHub), then
2. on the VPS: `./.laddy/scripts/kickoff.sh <task>`
   (or locally: `./.laddy/scripts/local-task.sh <task>`).

Do not implement anything — only author the spec.
