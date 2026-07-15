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

## Scope
In: a new `scripts/create-spec.sh`; a doc line in `env.local.example` if a knob
needs mentioning; tests under `tests/`.
Out: any change to `_phase_new` behaviour; running clarify/design/loop; any
dependency on `vps.conf`; any VPS execution; touching the merge/trust path.

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

## Notes
- `env.local`'s `AGENT_REPO_URL` already points at the VPS hub, so `_phase_new`
  clones the base and pushes the authored task branch straight to the hub — the
  VPS `kickoff <task>` then picks that branch up. Verify this handoff end to end
  (author locally → branch on hub → `kickoff <task>` finds the spec) as part of
  the change; if the existing pushed branch is not cleanly re-used by a no-`--new`
  kickoff, note it and adjust the follow-up guidance rather than silently
  assuming it works.
- Do not re-implement authoring logic; the script is purely a launcher around the
  already-tested `--phase new`.
