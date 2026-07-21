---
type: feature
roles: [developer, rw1, rw2]
risk: low
---
# argument-in-kickoff2 — `kickoff <task> --new "<brief>"` pre-loads the interactive authoring session

## Goal

Let a new task carry its brief on the command line so the brief PRE-LOADS the
interactive spec-authoring session, instead of the Director having to type it
into a blank chat. The Director types `kickoff <task> --new "<brief>"`; the
authoring Claude session opens already knowing the assignment, drafts the
spec from it, and the Director then refines it interactively.

This is NOT headless authoring. The session stays interactive end to end —
the brief only seeds the opening prompt; the Director still discusses,
corrects, and approves before it saves, exactly as `--new` works today.

## Today (root cause)

- `scripts/kickoff.sh` parses its own args with a flat loop:
  `for a in "$@"; do case "$a" in --new) DO_NEW=1;; --resume) DO_RESUME=1;;
  *) REST+=("$a");; esac; done`. A token typed after `--new` matches no case
  and falls into the generic `REST` bucket — it is never associated with
  `--new` at all.
- `REST` is forwarded to the `--phase clarify` / `--phase design` /
  `--phase loop` invocations, but **not** to the `--phase new` invocation.
  So today, a brief typed after `--new` skips spec authoring entirely and
  instead lands on `--phase clarify` as a second `task_ids` element, which
  `orchestrator/run.py` rejects: `parser.error("--phase clarify requires
  exactly one task id")`. The command fails outright; nothing is authored.
- `orchestrator/run.py::_phase_new` seeds the spec file with just
  `f"# {task_id}\n"`, then calls `deps.author_spec(wt, task_id, spec_rel)`,
  which launches `claude` (interactive TUI) with
  `SPEC_AUTHOR_PROMPT.format(spec_rel=spec_rel)`. The prompt template has no
  slot for a brief — there is currently no way to pass one through even if
  the CLI plumbing above were fixed.

## Change

- **`scripts/kickoff.sh`**: capture an optional token immediately following
  `--new` (only when it doesn't itself look like a flag, i.e. doesn't start
  with `-`) as the brief, keep it out of `REST`, and forward it **only** to
  the `--phase new` invocation as `--brief "<brief>"`. It must never reach
  `--phase clarify` / `design` / `loop` — routing it this way also fixes the
  stray-positional crash described above as a side effect, since the brief
  no longer rides along in `REST`.
- **`orchestrator/run.py` argparse**: add a string-valued `--brief` flag
  (same shape as the existing `--reason` flag), silently unused on phases
  other than `new`.
- **`orchestrator/run.py::_phase_new`**: accept an optional `brief`. When
  given:
  1. seed the spec as `f"# {task_id}\n\n{brief}\n"` instead of the bare
     headline,
  2. pass the brief through to `deps.author_spec(...)`,
  3. the "authoring session added nothing" guard must compare the
     post-authoring file against **this same brief-inclusive seed** (reuse
     one `seed` variable for both the write and the comparison, rather than
     hardcoding the bare headline in two places).
- **`SPEC_AUTHOR_PROMPT` / `_default_author_spec`**: thread the brief through
  and append an optional block when one is given, e.g. "The Director's brief
  for this task:\n\<brief\>\n\nDraft the spec from it; ask only about what it
  leaves open." Build the prompt so that with **no** brief it is
  byte-for-byte identical to today's — e.g. concatenate an appended block
  rather than interpolating an empty placeholder into the base template.

## Acceptance criteria

1. `kickoff <task> --new "<brief>"` opens an INTERACTIVE authoring session
   whose Claude prompt contains `<brief>`, and does not error at the
   `--phase clarify` step that follows.
2. `kickoff <task> --new` with no brief behaves exactly as today: identical
   seed content, identical authoring prompt, identical downstream phase
   calls.
3. The spec seed contains the brief when one is given; the "authoring added
   nothing" guard still correctly fires when the session leaves the
   brief-inclusive seed untouched.
4. An empty brief (`--new ""`) behaves the same as no brief, both in
   `kickoff.sh` and in `orchestrator/run.py`.
5. A token after `--new` that looks like a flag (starts with `-`) is treated
   as a separate flag, not a brief.
6. Unit tests cover: `kickoff.sh` forwards `--brief` only to `--phase new`
   and never to clarify/design/loop; `_phase_new` seeds with the brief and
   threads it into the author prompt; the no-brief path is unchanged
   (regression guard); the empty-authoring guard still fires with a brief
   present.

## Locus and verification

Touch points: `scripts/kickoff.sh`, `orchestrator/run.py`
(`SPEC_AUTHOR_PROMPT`, `_default_author_spec`, `Deps.author_spec`,
`_phase_new`, the `main()` argparse + dispatch), and their existing tests
(`tests/test_run_cli.py`, `tests/test_kickoff_wiring.py`). Neither file is in
`security_globs`; expect L1/L2.

`. .venv/bin/activate && ruff check . && basedpyright && pytest -n auto -q`.
Verify the merge with `merge-verified --local argument-in-kickoff2 <branch>`.

## Out of scope

- `scripts/local-task.sh` has its own `--new` flag; leave it untouched (this
  spec only covers `kickoff.sh`).
- `.laddy/specs/create-spec.md` proposes a larger rewrite of
  `SPEC_AUTHOR_PROMPT` into a structured house-style template (not yet
  implemented). This change should append the brief as its own block rather
  than restructuring the prompt, so it composes cleanly if that spec lands
  later.
- No change to `--code-ready` (already mutually exclusive with `--phase
  new`) or to any other phase's argument handling.

## Notes for the reviewer

- The point of this spec is the routing fix as much as the prompt change: a
  brief that leaks into `REST` and reaches `--phase clarify` is the exact bug
  being fixed — check the test asserting `--brief` never appears on the
  clarify/design/loop lines of `kickoff.sh`.
- Verify "no brief → byte-identical" is actually enforced by a test, not just
  asserted in prose — this is what protects `kickoff <task> --new` (no
  brief), which is by far the more common invocation, from any regression.
