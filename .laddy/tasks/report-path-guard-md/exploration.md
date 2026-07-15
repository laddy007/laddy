I now have a complete picture. Here is my scoping summary.

---

# Scope: report-path-guard-md

## Findings

**What exists today.** `loop-monitor report` (`monitoring/loop_monitor/cli.py:92-101`) computes `output` from either `json_report` or `build_report` and unconditionally `print()`s it to stdout. There is no `--out` flag, no file writer, and no path guard. The `report` subparser currently declares only `--at`, `--window-minutes`, `--json` (`cli.py:64-67`).

**The report body.** `build_report` (`report.py:48-131`) returns a string of lines joined with single `"\n"`, including blank-line section separators (`""`) and 2-space-indented sub-lines (`  pid=…`). Rendered as `.md` this collapses into one run-on paragraph — the "Markdown-safe" problem the spec calls out. `build_report`'s **signature and return value must not change** (AC1/AC6 require stdout byte-for-byte identical); the Markdown transform must wrap its output, not modify it.

**The mechanism to reuse (`b82394a`).** `orchestrator/queue.py` already implements the exact pattern the spec wants copied:
- `_read_lock_pid`/`_write_lock_pid` (`queue.py:145-184`) open with `O_NOFOLLOW`; a swapped-in symlink makes the open fail with `ELOOP` instead of writing through.
- `_refuse_symlink` (`queue.py:136`) converts `errno.ELOOP` into a clear refusal.
- The create pattern `O_CREAT | O_EXCL | O_WRONLY` is at `queue.py:104`.
- Test `test_run_lock_refuses_a_planted_symlink_instead_of_following_it` (`tests/test_queue.py:185`) is the template AC3 references: plant a symlink → known-content decoy, assert the write refuses and the decoy is **unchanged**.

**Test harness.** `tests/loop_monitor/conftest.py` puts `monitoring/` on `sys.path`, so `from loop_monitor.report_path import …` will resolve. Tests seed a `data_dir` via `storage.JsonlStore(tmp_path, retention_days=…).append("samples", record, ts)`; `pyproject.toml` sets `testpaths=["tests"]`, py311/ruff `F,I`. There is **no existing `test_cli.py`** — CLI wiring is currently untested, so new tests establish that path.

## Affected files

| File | Change |
|---|---|
| `monitoring/loop_monitor/report_path.py` | **New.** Standalone guard: validate suffix + confinement, open with `O_NOFOLLOW`/`O_EXCL`, write. Unit-testable in isolation. |
| `monitoring/loop_monitor/cli.py` | Add `--out`, `--out-root`, `--force` to the `report` subparser; reject `--out`+`--json` (A1=a); route to guard when `--out` given, else unchanged `print()`. |
| `monitoring/loop_monitor/report.py` (or the new module) | Small `render_markdown(body)` helper for the Markdown-safe transform. |
| `tests/loop_monitor/test_report_path.py` | **New.** Unit tests for the guard (each rejection branch). |
| `tests/loop_monitor/test_cli_report_out.py` | **New.** Wiring: happy path, `--json`+`--out` conflict, stdout-empty. |

## Proposed approach

**Guard helper** — single entry point, e.g. `write_report(text, out, out_root, force) -> None`, raising a dedicated `ReportPathError` on any refusal. Order (all checks precede any byte written — side-effect-free on rejection):

1. **Suffix.** Reject if `out.name` doesn't end in `.md` (and reject a bare `".md"`). String-only, cheapest first.
2. **Confinement.** `root = Path(os.path.realpath(out_root))`; `real_parent = Path(os.path.realpath(out.parent))`; refuse unless `real_parent == root or real_parent.is_relative_to(root)` (py3.9+, target is 3.11 — fine). Resolving the **parent** (not the final component) normalizes `../` and catches a parent-symlink escape, while leaving the final component for `O_NOFOLLOW`. Reconstruct the open target as `real_parent / out.name`.
3. **Open, TOCTOU-free:**
   - Try `os.open(target, O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW, 0o600)`.
   - Success → new file → write.
   - `FileExistsError` → target pre-exists:
     - not `--force` → refuse (`lstat` to say "regular file exists, use --force" vs "not a regular file").
     - `--force` → `os.open(target, O_WRONLY|O_NOFOLLOW|O_NONBLOCK)` (no `O_CREAT`); `ELOOP`→refuse symlink; then **`os.fstat(fd)`** and require `stat.S_ISREG` — reject non-regular *on the fd itself* (no TOCTOU); only then `os.ftruncate(fd, 0)` and write.

   `fstat`-on-fd + deferred truncate is what guarantees a dir/fifo/symlink is refused **regardless of `--force`** without ever truncating it. `O_NONBLOCK` avoids a fifo `open(O_WRONLY)` blocking on a missing reader.

**Markdown-safe render** (`render_markdown`): prepend a `# ` heading, then make single-`\n` line breaks survive by appending a hard break (two trailing spaces) to each non-blank line, leaving blank lines as paragraph separators. Smallest change matching the spec's first listed example; stdout path never calls this. (Alternative: heading + fenced code block preserves the body verbatim and still "starts with `#`" — noted as a fallback if hard-breaks prove fragile with the 2-space-indented sub-lines.)

**CLI wiring:** in the `report` branch — if `args.out and args.as_json`: `report.error("--out cannot be combined with --json")` (exit 2). If `args.out`: `out_root = args.out_root or config.data_dir`; `write_report(render_markdown(build_report(...)), …)`; print nothing; `except ReportPathError as e: print(e, file=sys.stderr); return 1`. Else: unchanged `print(output)`.

## Acceptance-criterion tests (behavioural contracts)

- **AC1 happy path** — `--out <data_dir>/x.md` writes file, stdout empty (`capsys`), exit 0; no-`--out` stdout unchanged.
- **AC2 `.md` enforced** — `report.txt`, `config.toml` refused, exit≠0, **no file created** (assert `not exists`).
- **AC3 symlink refused** — plant `out` as symlink → decoy with known content, even with `--force`; refused, decoy content **unchanged**, symlink not unlinked.
- **AC4 confinement** — `../escape.md`, absolute path elsewhere, and a parent-dir-symlink escape all refused, nothing written; path inside default `data_dir` and inside an explicit `--out-root` accepted.
- **AC5 no-clobber** — existing regular file without `--force` refused; with `--force` overwritten; existing **dir/fifo** refused *with and without* `--force`.
- **AC6 Markdown-safe** — file starts with `#`; two report lines stay on separate rendered lines (assert hard-break/blank-line separation present, not one paragraph).
- **AC7 no regression** — full suite green; every branch (each rejection + happy + `--force`) covered for diff-coverage.

## Risks / watch-items

- **Residual fifo TOCTOU** in the `--force` branch (target swapped between the first `EXCL` open and the second open) is closed by `fstat`-on-fd + `O_NONBLOCK`; call this out in a comment mirroring `queue.py`'s.
- **Relative `--out`** resolves against **cwd**, so a bare `x.md` is confined against cwd and normally rejected — the loop must pass an absolute path under `data_dir`. Worth a docstring note; not a defect.
- **`out_root` must exist** as a real directory (default `data_dir` does in real use — the report reads from it; `JsonlStore` creates it). A missing root surfaces as a `FileNotFoundError` at open → map to a clean `ReportPathError`, don't let it escape raw.
- **Markdown hard-breaks vs 2-space indentation**: the `  pid=…` sub-lines are 2-space indented (safe; 4+ would become a code block). Verify the chosen transform doesn't accidentally push any line to ≥4 leading spaces. The fenced-code fallback sidesteps this entirely.
- **Exit-code consistency**: `--json`+`--out` conflict via `parser.error` exits 2; guard refusals exit 1. Both non-zero (spec-compliant) but differ — intentional, keep it deliberate.
- **`O_NOFOLLOW` is POSIX**; the monitor is Linux/VPS-only, so no portability concern, but note it as the queue module already does.

No product code was changed. I reproduced the mechanism to reuse (`queue.py` `O_NOFOLLOW` path + its symlink test) and confirmed the current stdout-only report path.