---
type: feature
roles: [developer, rw1, rw2]
risk: low
---
# create-spec — Director-side, author a spec locally and push it to the hub

## Goal
Add a standalone local entrypoint `scripts/create-spec.sh <task>` that runs ONLY
the interactive spec-authoring phase (`orchestrator.run --phase new`) on the
Director's machine, using `env.local` (no `vps.conf`, no VPS), and stops after
the spec is authored and pushed to the hub. The Director then runs the task on
the VPS with `kickoff <task>` (no `--new`). This fills the gap that today
`--phase new` is only reachable via `kickoff.sh --new` (VPS-only, needs
`env.vps`) or `local-task.sh --new` (which then runs the whole loop locally) —
there is no "author a spec here, run it there" step.

## Root-cause context
`_phase_new` (`orchestrator/run.py`) already does the right thing on its own:
it seeds a headline, opens the interactive co-authoring session, refuses if
nothing was added beyond the headline, then `commit_all` + `push` the task
branch. It just isn't exposed as a thin, env.local-based launcher. `kickoff.sh`
is the model to mirror, but it sources `env.vps` and always continues into
clarify → design → loop.

Separately, the authoring prompt itself (`SPEC_AUTHOR_PROMPT` in `_phase_new`)
is thin: it tells the agent to "fill in the rest (Markdown; optional front
matter with type/roles)" and nothing about the house spec structure, the `risk`
field, `status: draft-proposal`, the "small / testable / self-contained, slice
big tasks" discipline, or any project context — so a well-structured spec today
is convention, not something the tool guides. It also still says "myapp agent"
(stale naming). Since create-spec exists to make local authoring good, the same
change enriches that shared prompt so authored specs come out consistently
structured — and this benefits `kickoff --new` too, since both go through
`_phase_new`.

## Scope
In: a new `scripts/create-spec.sh`; an enriched `SPEC_AUTHOR_PROMPT` in
`_phase_new` (`orchestrator/run.py`) — a structured spec-format template + brief
project context, plus fixing the stale "myapp agent" naming; a doc line in
`env.local.example` if a knob needs mentioning; tests under `tests/`.
Out: any change to `_phase_new`'s control flow (seed → author → commit/push) or
the launcher's phase set beyond the prompt *text*; running clarify/design/loop;
any dependency on `vps.conf`; any VPS execution; touching the merge/trust path.

## Behaviour
`create-spec.sh <task>`:
1. Resolves the engine dir from its own location (like `kickoff.sh`) and sources
   `<engine>/env.local`. If `env.local` is missing, it fails with a clear error
   pointing at `local-onboard.sh` / `env.local.example` — never a silent default.
2. Validates `<task>` exactly as `kickoff.sh` does: non-empty, matches
   `^[a-zA-Z0-9._-]+$`, and is not the reserved `main`.
3. Exports `PYTHONPATH` and picks `PY="${PYTHON_BIN:-python3}"` (same as kickoff),
   verifying `PY` exists.
4. Runs **only** `"$PY" -m orchestrator.run "<task>" --phase new` in the
   foreground (the co-authoring session is interactive and must reach the
   Director's terminal). It does NOT run `--phase clarify`, `--phase design`, or
   `--phase loop`.
5. On success prints the follow-up hint: author is done and pushed to the hub;
   run it on the VPS with `kickoff <task>` (no `--new`).
6. Propagates `_phase_new`'s exit code (e.g. the "authoring added nothing beyond
   the headline" and "spec already exists" refusals surface unchanged).

### Enriched authoring prompt
`SPEC_AUTHOR_PROMPT` is rewritten to guide the agent to produce a house-style
spec, at minimum:
- **front matter**: `type`, `roles`, `risk` (`low|medium|high`), and the
  optional `status: draft-proposal` (with a one-line note on what draft means —
  the loop refuses to run it);
- **sections**: `# <task> — headline`, `## Goal`, root-cause/why context,
  `## Scope` (explicit In / Out), `## Acceptance criteria` (numbered and
  **testable**), `## Notes`;
- **discipline**: keep specs small, self-contained, and testable; a large task
  is sliced (S0, S1, …) and NOT run as one loop;
- **naming**: target-generic (derive the target name from config / use a neutral
  term) — remove the hardcoded "myapp agent".
The prompt stays a co-authoring instruction (discuss, then fill in and stop); it
gains structure, not a change to the authoring flow.

## Acceptance criteria
1. `create-spec.sh <task>` sources `env.local` (NOT `env.vps`) and needs no
   `vps.conf`: it runs with only `env.local` present. Asserted by a test whose
   environment/filesystem has no `vps.conf`.
2. It invokes `orchestrator.run <task> --phase new` **exactly once and no other
   phase** (no clarify/design/loop). Asserted by a test that points `PYTHON_BIN`
   at a recording stub which captures argv, then checks the phases invoked.
3. Task-name validation mirrors `kickoff.sh`: empty, invalid characters, and the
   reserved `main` are each refused with a non-zero exit and a clear message —
   asserted per case.
4. A missing `env.local` yields a clear error and non-zero exit (no silent
   fallback to orchestrator defaults).
5. The follow-up hint naming `kickoff <task>` on the VPS is printed on success.
6. Suite green (`ruff`, `basedpyright`, `pytest`). The script is a thin launcher
   mirroring `kickoff.sh`; keep its style consistent with the existing scripts.
7. `SPEC_AUTHOR_PROMPT` names the required front-matter fields (incl. `risk` and
   the optional `status: draft-proposal`) and the section structure (Goal /
   Scope In-Out / numbered testable Acceptance criteria / Notes) and the
   small/testable/slice discipline — asserted by a test on the prompt string
   (the fields/section names are present) so both `create-spec` and
   `kickoff --new` produce structured specs.
8. The stale "myapp agent" wording is gone from the prompt; the target name is
   target-generic — asserted by a test that the literal "myapp" no longer
   appears in `SPEC_AUTHOR_PROMPT`.

## Notes
- `env.local`'s `AGENT_REPO_URL` already points at the VPS hub, so `_phase_new`
  clones the base and pushes the authored task branch straight to the hub — the
  VPS `kickoff <task>` then picks that branch up. Verify this handoff end to end
  (author locally → branch on hub → `kickoff <task>` finds the spec) as part of
  the change; if the existing pushed branch is not cleanly re-used by a no-`--new`
  kickoff, note it and adjust the follow-up guidance rather than silently
  assuming it works.
- Do not re-implement authoring logic; the script is purely a launcher around the
  already-tested `--phase new`. The only code touched in `_phase_new` is the
  prompt *text* (`SPEC_AUTHOR_PROMPT`), which is shared, so `kickoff --new` picks
  up the better structure for free — verify its existing authoring test still
  passes.
