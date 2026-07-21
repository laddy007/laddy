I have a full picture now. Here's the scoping summary.

## Findings

**Root cause confirmed, exactly as spec describes:**
- `scripts/kickoff.sh:67-73` — the flat `for a in "$@"` loop has no case for a bare token after `--new`; it falls into `*) REST+=("$a")`.
- `scripts/kickoff.sh:92` — `REST` (now polluted with the brief) is forwarded to `--phase clarify`, which requires exactly one task id (`orchestrator/run.py:1078`) and errors on the extra positional.
- `orchestrator/run.py:179-198` (`_phase_new`) hardcodes `seed = f"# {task_id}\n"` twice (write at line 195, comparison at line 199) and calls `deps.author_spec(wt, task_id, spec_rel)` with no brief slot.
- `orchestrator/run.py:79-93` (`SPEC_AUTHOR_PROMPT` / `_default_author_spec`) has no brief placeholder.

## Affected files
- `scripts/kickoff.sh` (arg loop + the `--phase new` invocation line)
- `orchestrator/run.py`: `SPEC_AUTHOR_PROMPT`, `_default_author_spec`, `Deps.author_spec` type, `_phase_new`, `main()` argparse + dispatch (line 1160-1163)
- `tests/test_run_cli.py`: 4 existing `author_spec` fakes (lines 112, 255, 283, 296) take 3 positional args — all need a 4th `brief` param once the callable's shape changes, or the tests break even though this task isn't "about" them.
- `tests/test_kickoff_wiring.py`: add a routing test alongside the existing static-text-inspection style (`_order_ok`, line 10-14).

## Proposed approach

**`kickoff.sh`** — replace the `for a in "$@"` loop with an index-based walk so `--new` can peek the next token:
```bash
args=("$@"); n=${#args[@]}
REST=(); DO_NEW=0; DO_RESUME=0; BRIEF=""
i=1
while [ "$i" -le "$n" ]; do
  a="${args[$((i-1))]}"
  case "$a" in
    --new)
      DO_NEW=1
      if [ "$i" -lt "$n" ] && [[ "${args[$i]}" != -* ]]; then
        BRIEF="${args[$i]}"; i=$((i+1))
      fi
      ;;
    --resume) DO_RESUME=1 ;;
    *) REST+=("$a") ;;
  esac
  i=$((i+1))
done
```
Then the `--phase new` call:
```bash
"$PY" -m orchestrator.run "$TASK" --phase new ${BRIEF:+--brief "$BRIEF"}
```
Verified this bash idiom (tested standalone) — with `BRIEF=""` it expands to zero extra tokens (byte-identical call to today, satisfies AC2/AC4 automatically rather than needing an if/else duplicate branch); with `BRIEF="two words"` it expands to exactly `--brief` + one word. `REST` never contains `--new` or the brief, so it can't leak into clarify/design/loop (AC1, AC5, the bug fix).

**`orchestrator/run.py`**:
1. `parser.add_argument("--brief", help="new phase: optional brief that seeds spec authoring")` next to `--reason` (line ~1046).
2. `_phase_new(config, task_id, deps, brief: str | None = None)`; compute `seed` once (`f"# {task_id}\n\n{brief}\n"` if `brief` else the bare headline) and use that same `seed` variable for both the write (line 195→197) and the guard comparison (line 199) — satisfies AC3/AC4 (falsy check handles `""` same as `None`).
3. `deps.author_spec(wt, task_id, _spec_rel(task_id), brief)`; widen `Deps.author_spec` type to `Callable[[Path, str, str, str | None], None]`.
4. `_default_author_spec(wt, task_id, spec_rel, brief=None)`: build the base prompt exactly as today, then only when `brief` is truthy, append a separate `SPEC_AUTHOR_BRIEF_BLOCK` template (own string, concatenated — not interpolated into the base template) so the no-brief path is untouched byte-for-byte (AC2), and it composes independently of the separate `create-spec.md` restructuring proposal (out of scope, noted in spec).
5. Dispatch at line 1160-1163: `rc = _phase_new(config, task_id, deps, brief=args.brief)` for both the `--phase new` and `--phase all` + `--new` cases.

**Tests to add/update** (per AC6):
- `tests/test_run_cli.py`: update the 4 existing `author_spec` fakes to accept the 4th `brief` param; add a brief-threading test (assert prompt/seed contains the brief) and an explicit no-brief regression test comparing exact seed/prompt bytes against today's; assert the "added nothing" guard still fires when a brief-inclusive seed is left untouched.
- `tests/test_kickoff_wiring.py`: add a test in the `_order_ok`-adjacent style asserting `--brief` appears on the `--phase new` line and never on the `--phase clarify` / `--phase design` / `--phase loop` lines, mirroring how `test_kickoff_resume_forwards_reason_and_detaches` inspects a single matched line.

## Risks / things to flag to the developer and reviewer

1. **`_refresh_stub_spec` (run.py:212-236) is untouched by the spec but has a latent interaction**: it detects a failed `--new` leftover by comparing the worktree spec verbatim against the bare `stub = f"# {task_id}\n"`. If a **brief-seeded** `--new` fails the "added nothing" guard (exits 2, never commits), the leftover file is the brief-inclusive seed, not the bare stub — so this staleness-refresh no longer recognizes it as the known "TODO 2026-07" leftover case on a later plain kickoff. This is outside the spec's explicit "Locus and verification" list, so I did not change it, but the reviewer notes on the guard reuse make it worth a call-out; it's a pre-existing-shape TODO, not a new regression path this task should silently paper over.
2. **`Deps.author_spec` type widening is a signature-breaking change** for any fake defined with only 3 params (all 4 in `tests/test_run_cli.py`) — must update all four or they'll TypeError at call time, not just fail type-checking.
3. Keep the `--phase new` invocation as a single line with `${BRIEF:+...}` rather than an if/else with two near-duplicate lines — simpler to keep AC2's "byte-for-byte identical" true by construction rather than by discipline, and easier for the wiring test to assert against one line.
4. No side effects to worry about on the "does nothing" paths: `--new` with no spec present and authoring producing nothing still returns 2 without commit/push, unchanged.

Ready for the developer to implement against this scope.