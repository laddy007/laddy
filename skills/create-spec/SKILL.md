---
name: create-spec
description: Co-author a task spec for the laddy dev-loop and save it to
  .laddy/specs/<task>.md in the current target repo, ready for kickoff.
  Use when the Director wants to prepare a dev-loop task ("napis spec",
  "priprav task pro agenta", "create a spec for X"). One-command local
  authoring: run `claude "/create-spec <task>"` from the target repo —
  the local counterpart of `kickoff.sh <task> --new` on the VPS.
---

# create-spec — dev-loop spec authoring (local)

You are co-authoring a task spec named `<task>` in the **target** repo the
session was started in. The task id comes from the skill argument
(`/create-spec <task>`); ask for it if missing. It must match
`^[A-Za-z0-9._-]+$`, must not be `main` (reserved), and
`.laddy/specs/<task>.md` must not already exist.

First explore intent, scope and requirements with the Director before
writing anything (use `/superpowers:brainstorming` if that skill is
available; otherwise interview directly). Do not implement anything —
only author the spec.

Context:

- The spec is executed later by the autonomous dev-loop (developer + two
  reviewer agents) on a VPS worktree cloned from the hub — the agent sees
  only the spec and the code, never this conversation, so the spec must be
  fully self-contained. Read an existing spec under `.laddy/specs/` for the
  expected shape and level of detail; the engine `roles/developer.md`
  (wherever the engine is checked out) shows what the developer is told.
- The spec must cover: goal, scope (in and out), constraints, the files or
  areas involved, and a `## Acceptance criteria` section. Leave no open
  question for the agent to guess — anything unresolved here resurfaces as
  a clarify-gate question at kickoff, or worse, as a wrong guess.
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
  are report-only tasks: the deliverable is a findings report, merged under
  a stricter path guard).
- Auto-classify risk from the TARGET's own policy: read
  `.laddy/policy.toml` (sensitive / security / invariant paths) and stamp
  `risk: high` in the front matter whenever the agreed scope touches any of
  them or deploy / secret / CI files. A high-risk task runs a mandatory
  design-approval gate at kickoff — set the flag yourself whenever the
  scope qualifies, never ask the Director to opt in.
- Never mark a spec `status: ready` on the Director's behalf, and never
  write `status: draft-proposal` unless the Director asks — kickoff refuses
  drafts by design.

When you agree on the spec, WRITE it to exactly `.laddy/specs/<task>.md`
(LF line endings, ASCII-safe).

Then tell the Director it is ready to dispatch, with the real commands:

1. Ship the spec to the hub (the VPS clones from the hub, not GitHub):
   `git add .laddy/specs/<task>.md && git commit -m "spec: <task>"`,
   then `<engine>/scripts/push-hub.sh <user>` from this repo.
2. Kick off on the VPS (inside tmux — the clarify/design gates run
   foreground): `ssh <alias> '~/laddy/scripts/kickoff.sh <task>'`.
   For a batch, enqueue instead: `python -m orchestrator.run --phase
   enqueue <task> ...` (`--chain` to run them in order, each building on
   the previous one's result).
3. Or rehearse the whole loop locally, no VPS: `<engine>/scripts/local-task.sh <task>`.

If several specs were authored in one session and they depend on each
other, say so explicitly and suggest `enqueue --chain` in that order.
