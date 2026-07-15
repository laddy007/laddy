---
type: feature
roles: [developer, rw1, rw2]
risk: medium
---
# report-path-guard-md — write loop-monitor reports to a guarded Markdown file

## Goal
Give `loop-monitor report` a way to write its output to a file on disk, behind a
path guard, so the autonomous dev-loop can persist monitoring reports as
Markdown artifacts (archive per round, attach to a merge, feed a notification)
**without the output path becoming a new security hole**. Today the report can
only be `print()`ed to stdout (`monitoring/loop_monitor/cli.py:100`); persisting
it means shell redirection, which puts the whole destination — extension,
location, symlink/overwrite behaviour — outside the tool's control and outside
any test.

## Root-cause context (why a guard, not just a flag)
Once the *loop* (or onboarding), not a human at a keyboard, chooses where a
report lands, that path is untrusted input. This repo has already hardened two
sibling cases along exactly this line:

- `b82394a fix(security): stale-lock reclaim must not follow a symlink` — a
  planted symlink in a writable dir let a sibling worktree redirect a
  write-through; fixed by opening with `O_NOFOLLOW` so the open fails (ELOOP)
  instead of following. This task should reuse that mechanism, not reinvent it.
- `d8ad518 fix(security): validate onboarding answers before they reach a
  sourced config` — untrusted values validated before they can do harm.

A raw `--out PATH` with no guard would regress that posture: a caller (or a
sibling task under the same `AGENT_WORK_ROOT`) could get the loop to overwrite
`config.toml`, an `.env`, a lockfile, or a file outside the data dir by writing
through a symlink or a `../` path. The guard closes that.

## Design (agreed with Director)
Add `--out PATH` to the `report` subcommand. When given, the report is written to
that file **instead of** stdout (stdout stays the default when `--out` is
omitted, so existing behaviour and tests are unchanged). A path guard validates
the destination *before* any bytes are written, enforcing all four of:

1. **`.md` suffix required.** Reject any `--out` whose name does not end in
   `.md`. This is the "md" in the task name and the first line of defence
   against pointing the writer at `config.toml` / `.env` / `*.lock`.
2. **No symlink follow.** Neither the final target nor any parent component may
   be followed as a symlink to escape the destination. Reuse the `b82394a`
   mechanism: perform the create/write with `os.open(..., O_NOFOLLOW)` so a
   swapped-in symlink at the target makes the open fail rather than writing
   through; parent-directory symlink escape is rejected via the confinement
   check below.
3. **Confined to an output root.** The resolved parent of the target must lie
   inside an allowed output root. The root **defaults to the monitor's
   `data_dir`** (the loop already owns it and has write rights) and is
   overridable with `--out-root PATH`. A `--out` that resolves outside the root
   (`../…`, an absolute path elsewhere, or an escape via a parent symlink) is
   refused.
4. **No clobber.** Refuse to overwrite an existing file unless `--force` is
   given; refuse outright if the target exists and is not a regular file. The
   natural TOCTOU-free implementation is `O_CREAT | O_EXCL | O_NOFOLLOW`
   (drop `O_EXCL` only when `--force`).

Every rejection exits non-zero with a clear one-line reason on stderr and writes
nothing. The guard is a small standalone helper (e.g.
`monitoring/loop_monitor/report_path.py`) so it is unit-testable in isolation.

### Markdown-safe body
When writing to a `.md` file the body must be *minimally valid Markdown*, not
raw plain text: the current report joins lines with single `\n`, which Markdown
collapses into one run-on paragraph. Do the smallest change that makes the `.md`
render correctly on GitHub / in a PR — e.g. a top `#` heading and line breaks
that survive Markdown (hard breaks or blank-line separation / bullets). Do **not**
undertake a full report redesign (tables, per-section templating); that is a
separate task. Stdout output stays byte-for-byte as it is today.

## Scope
In: `monitoring/loop_monitor/cli.py` (the `--out` / `--out-root` / `--force`
flags and wiring), a new guard helper module under `monitoring/loop_monitor/`,
the minimal Markdown-safe rendering, and tests under `tests/loop_monitor/`.
Out: any change to stdout output or the `json`/`overhead` report paths; a full
Markdown redesign of `build_report`; any new third-party dependency; any change
to what the report *reads* from `data_dir`.

## Acceptance criteria
1. **Happy path.** `loop-monitor report --out <data_dir>/x.md` writes the report
   to that file, prints nothing to stdout, and exits 0. Without `--out`,
   behaviour and stdout are byte-for-byte unchanged (existing report tests still
   pass untouched).
2. **`.md` enforced.** A `--out` not ending in `.md` (e.g. `report.txt`,
   `config.toml`) is refused, exits non-zero, and creates/modifies no file.
3. **Symlink refused.** If the target path is a pre-planted symlink (à la
   `test_run_lock_refuses_a_planted_symlink_instead_of_following_it`), the write
   fails and the symlink's destination is left untouched — verified by asserting
   a decoy file's content is unchanged.
4. **Confinement.** A `--out` resolving outside the output root — via `../`, an
   absolute path elsewhere, or a parent-directory symlink escape — is refused
   and writes nothing. A path inside the root (default `data_dir`, or an explicit
   `--out-root`) is accepted.
5. **No clobber.** Writing to an existing regular file without `--force` is
   refused; with `--force` it overwrites. An existing non-regular target
   (dir/fifo/symlink) is refused regardless of `--force`.
6. **Markdown-safe output.** The written `.md` renders as distinct lines/sections
   (not one collapsed paragraph): assert the file starts with a `#` heading and
   that report lines remain separated under Markdown rules.
7. **No regression.** The full existing suite stays green; every new branch
   (each rejection reason + the happy path + `--force`) is covered so
   diff-coverage holds.

## Clarifications

**Q1:** The `report` subcommand supports `--json`, and `--out` is being added to the same subcommand. How should `report --json --out <file>` behave? Options: (a) reject `--out` together with `--json` (out-to-file is text/Markdown only) [recommended — keeps the `.md`/Markdown-safe guarantees coherent and matches Scope excluding the json path]; (b) allow it, writing raw JSON through the same path guard but keeping the `.md` suffix requirement (a `.md` file containing JSON); (c) allow it and relax the `.md` suffix to `.json` for the JSON case.
**A1:** a
