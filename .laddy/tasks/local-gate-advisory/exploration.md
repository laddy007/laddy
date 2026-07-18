I now have a complete picture. Let me confirm one detail about the digest so the advisory record renders honestly, then write up the scoping.

The exploration is complete. Here is my structured summary.

---

# Exploration: `local-gate-advisory` — `merge-verified --advisory`

## Findings

**This is a feature, not a bug.** Today `decide()` treats the two judgment gates (security panel, rw2 re-run) identically to the deterministic gates: any blocker lands in one flat `broken` list → `BROKEN` hold, no merge offer. There is no code path that records-and-proceeds. The task is to add an opt-in `--advisory` mode that waives *only* the judgment gates, records their findings durably in local main, and merges.

**Key structural facts confirmed:**

- `decide()` (`orchestrator/local_merge.py:193-241`) is pure and returns a frozen `MergeVerdict`. The deterministic block is `:198-216` (policy, tests, coverage, scan_findings, infra_overridden); the judgment block is `:218-223` (security panel, rw2) — exactly the split the spec cites.
- `MergeVerdict` (`:104-114`) is a frozen dataclass; adding `advisory: tuple[str, ...] = ()` is backward-compatible (all positional call sites keep working).
- **The AC5 trap is real and lives at `local_merge.py:346-348`:** on a confirmed RISK_DECISION the engine rebuilds `MergeVerdict(task_id, "merge", RISK_DECISION)` from scratch, dropping every field. If the durable write keys on `verdict.advisory`, this line silently discards it → the L3 confirm path would never record. Must preserve `advisory` here (cleanest: `dataclasses.replace(verdict, decision="merge")`).
- The durable write must NOT go in `decide()` (it stays pure) and must NOT go only in the auto-merge branch. The right seam is the engine's post-merge step in `run()` (`:359-369`) or its `on_verdict`/`_report` (`:950-964`), keyed on `merged and verdict.advisory` — this fires on both the auto-merge and confirm paths with one call site.
- **AC3 requires a real commit into local main**, not just a working-tree artifact. `_report` at `:955-956` writes `merge-hold.md` via `TaskArtifacts.write_text` but never commits it. The advisory record needs `write_text` + `git add -- <path>` + `git commit`. After `merge_branch` the repo is already checked out on `main` (`:765`), so a follow-up commit lands on main and survives task-branch deletion.
- **The change is judgeable by its own gate:** `RESTORED_INFRA_PATHS` (`testgate.py:188-191`) is only `.laddy/docker` + `.laddy/security`; `orchestrator/*.py` is not restored, so `infra_overridden` stays empty and the gate can actually run on this diff (as the spec's "Notes" predicts). It will almost certainly classify **L3** and the security panel may object — that objection is expected; this is the intended last bootstrap hand-merge.
- **`merge-verified.sh` needs no change:** line 68 is `exec "$PY" -m orchestrator.local_merge --repo "$REPO_DIR" "$@"` — `--advisory` passes straight through.

## Affected files

- `orchestrator/local_merge.py` — the whole change:
  - `MergeVerdict` (`:104`): add `advisory: tuple[str, ...] = ()`.
  - `decide()` (`:193`): signature `decide(task_id, gates, *, advisory_mode: bool = False)`; split into `deterministic` and `judgment` reason lists; `blocking = deterministic + ([] if advisory_mode else judgment)`; if `blocking` → BROKEN; else set `advisory = tuple(judgment) if advisory_mode else ()` and attach to the RISK_DECISION hold and the AUTO_MERGE verdict.
  - `LocalMergeEngine` (`:315`): add `advisory_mode: bool = False` and an injected `record_advisory: Callable[[MergeVerdict], None] = field(default=lambda v: None)`. In `run()`: pass `advisory_mode` into `decide()`; fix `:347` to preserve `advisory` via `replace(verdict, decision="merge")`; after a successful `merge_one`, call `self.record_advisory(verdict)` when `verdict.advisory`.
  - New pure `render_advisory(task_id, advisory)` → markdown, and a real `record_advisory` closure in `main()` that writes `.laddy/tasks/<task>/merge-advisory.md`, `git add`s just that path, and commits on main.
  - `main()` argparse (`:870`): add `--advisory`; wire `advisory_mode` + `record_advisory` into the engine; print a loud banner when on; make `_report`'s merged line visibly distinct for advisory merges (honest labeling, AC5/constraint 5).
  - Add `from dataclasses import replace` (currently only `dataclass, field` at `:34`).
- `tests/test_local_merge.py` — new tests (below).
- `scripts/merge-verified.sh` — **no change** (verified).

## Proposed approach (decision matrix for `decide`)

| advisory_mode | deterministic red? | judgment red? | blast | result |
|---|---|---|---|---|
| any | yes | any | any | **BROKEN** (fail closed) |
| False | no | yes | any | **BROKEN** (today's behavior, byte-identical) |
| True | no | yes | ≠L3 | **merge**, `advisory=(judgment…)` |
| True | no | yes | L3 | **RISK_DECISION** hold, `advisory=(…)` → recorded on confirm |
| any | no | no | ≠L3 | **merge**, `advisory=()` |
| any | no | no | L3 | **RISK_DECISION**, `advisory=()` |

Reuse the exact reason strings decide already builds for the judgment gates, so the advisory tuple is symmetric with the BROKEN reasons (matches the spec sketch: "the judgment reasons go into the verdict's `advisory` field").

## Acceptance-criterion tests to write first

1. `decide(_gates(security_blockers=[…]), advisory_mode=True)` → `decision=="merge"`, waived summary in `.advisory`; same gates `advisory_mode=False` → `BROKEN`. (AC1)
2. For each deterministic gate (`tests_passed=False`, `coverage_ok=False`, `scan_findings=(…)`, `policy_ok=False`, `infra_overridden=(…)`): `advisory_mode=True` still → `BROKEN`, `.advisory==()`. (AC2 — one test per gate)
3. Engine-level: advisory L2 merge → `record_advisory` invoked with the verdict; regression: `advisory_mode=False` security blocker → held, `record_advisory` **never** called. (AC4)
4. Integration (real git, like `test_cli_l3_confirmed_merges…`): `--advisory` merge commits `.laddy/tasks/t1/merge-advisory.md` into main, asserted via `git cat-file -e main:.laddy/tasks/t1/merge-advisory.md` from a checkout that never had the branch. (AC3)
5. **AC5 (the trap):** L3 branch, only a security-panel finding, `--advisory`, `confirm=lambda v: True` → merges **and** writes `merge-advisory.md`; assert the file is committed on main. Also add a focused engine test that a confirmed RISK_DECISION verdict keeps a non-empty `.advisory` (guards `:347`).
6. Regression guard: no `--advisory` → existing suite unchanged (all current `decide` tests must stay green with the new keyword-only default).

## Risks

- **AC5 preservation bug (highest):** if the developer wires the write into the auto-merge branch or forgets to preserve `advisory` at `:347`, the L3 confirm path silently drops the record. Covered by test 5 above — write it first.
- **Purity of `decide()`:** the durable write must not creep into `decide()`. Keep it in the executor.
- **Commit hygiene:** the advisory commit must `git add` only the one file (`-- <path>`), never `add -A`, or an unrelated dirty working tree gets swept into main. Repo is on `main` post-merge; don't assume otherwise.
- **Deterministic-gate integrity (the invariant reviewers must check):** `advisory_mode` may only remove `judgment` from `blocking`. Any refactor that lets it touch `deterministic` violates constraint 1 — the security-sensitive line of the whole change. Tests in AC2 pin this per-gate.
- **`replace(verdict, decision="merge")`** keeps `kind=RISK_DECISION`, matching the old explicit constant, so `test_engine_risk_decision_confirmed_merges` stays green — verify it does.
- **Honest labeling:** the merged-line output and the markdown itself must state "not fully verified / judgment gates waived" (constraint 5). Easy to omit; assert on the output/file text.
- No push/origin impact — `--advisory` only reaches the local-main merge; push stays the separate Tier-3 `_ask`/`push_and_cleanup` decision (unchanged).