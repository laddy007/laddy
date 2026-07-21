---
type: feature
roles: [developer, rw1, rw2]
risk: high
---
# fable-findings-fix -- implement the audit C2+C3 trust/merge hardening findings

## Goal
In ONE unattended task run, land every **confirmed** finding from the C2+C3
report-only audit of the trust/merge/policy core (`orchestrator/{local_merge,
merge_check,merge_subject,gitops,policy,verdict,target_policy,flags}.py`) -- HIGH
(H1-H8), MEDIUM (M1-M8), and the LOW quality/convention bucket -- each shipping
with its own test. The developer works the findings as an ordered set of stages
(S0..S10 below) on a single branch; it may implement them sequentially or fan out
to per-stage subagents, but it is one kickoff, one branch, one review pipeline.
Do **not** re-open the audit's "Rejected -- do NOT chase" items (end of file).

## Why one run (unattended, no Director present)
This task is deliberately run as one loop because the Director is away and cannot
perform the between-slice steps a sliced plan needs. In laddy, chaining separate
slices requires `push-hub` (advancing hub `main`) between each -- a trusted-machine
action only the Director can take -- so "all findings, unattended, through laddy"
means one task branch. Safety is preserved by the trust model, not by slicing:
- rw1 (Claude) + cross-vendor rw2 (Codex) + the security panel review the whole
  diff on the VPS, unattended.
- **Nothing merges into `main` without the Director.** The loop pushes the verified
  branch to the hub; the L3 merge (type the exact task id in `merge-verified.sh`)
  waits for the Director's return. A `cap_reached` (loop hit `MAX_LOOPS`) is a safe
  stop -- it holds, never a forced merge.

> **Scale note.** This is a large L3 change over the trust boundary carried through
> one loop; the default `MAX_LOOPS=4` is too few developer<->review bounces. Kick
> off with a raised cap (below). If it still hits the cap, it stops safely and the
> Director resumes / splits the remainder -- do not force a merge.

## Run configuration (kickoff -- unattended)
```
ROLE_DEVELOPER_MODEL=claude-fable-5 MAX_LOOPS=16 ./scripts/kickoff.sh fable-findings-fix
```
- **Developer on Fable.** The engine resolves a role's runner from
  `ROLE_<NAME>_{VENDOR,MODEL,THINKING}` (`orchestrator/config.py`
  `_parse_role_bindings`; `orchestrator/run.py` `_resolve_runner`); role key for the
  developer is `developer` (`orchestrator/loop.py:78`). Setting only
  `ROLE_DEVELOPER_MODEL` keeps vendor = claude and swaps just `--model`. No per-spec
  model field exists, so this is an env knob at kickoff (set it in `env.vps`).
- **Reviewers stay as-is.** rw1 (Claude) and rw2 (cross-vendor, Codex) gate the run;
  do not move rw2 off its cross-vendor default -- that independent review is the
  whole point over this surface.
- **`MAX_LOOPS`.** Default 4 (`config.py`) is too few here; `MAX_LOOPS=16` is a
  starting point. `cap_reached` is a real signal the task was under-sliced, not a
  silent fail.

## Source and confidence
Findings, file:line anchors, repro sketches, and suggested tests live in
`audit-c2c3-handoff.md` (repo root). `[VERIFIED]` items were hand-checked end to
end; `[CONFIRMED]` items were adjudicated but not independently reproduced --
**reproduce a CONFIRMED item before committing its fix** (its repro sketch in the
handoff is the starting point). This spec does not restate the full sketches.

## Global constraints (apply to EVERY stage -- non-negotiable)
- **L3 / human-gated.** Every touched file is engine trust-boundary surface
  (`.laddy/policy.toml` `security_globs` / `invariant_tests`); the whole change is a
  stop-before-merge and the merge is the Director's explicit decision.
- **Behavior change ships with a test.** Untested behavior is undefined. Pure
  ASCII/doc edits are TDD-light; anything that changes a decision is not.
- **Preserve the invariants.** Fail-closed engine guards; derive-don't-store (replay
  from `iteration-log.jsonl`); injected clock (no bare `datetime.now` /
  `time.sleep`); typed models over freeform dicts; LF + ASCII-safe source; files
  split rather than grow.
- **Engine vs target.** Do not change what a *target* can weaken; engine-generic
  guards stay engine-side and fail closed. A target may only *add* sensitive
  surface, never widen the safe/L1 lane to cover code.
- **Do not chase the Rejected list** (end of file). Those were refuted.
- **Gate stays green throughout:** `ruff check .` clean, `basedpyright` 0 errors,
  `pytest -n auto -q` green.

## Execution order and method
Implement the stages **in order** on the one task branch. Each stage is
self-contained and ships its own test(s); keep commits small (per stage, ideally
per finding) so the accumulated diff stays reviewable. The developer may delegate a
stage to a subagent, but all work lands on this branch and is reviewed together.
Stages are ordered so earlier ones do not depend on later ones; the couplings that
exist are called out (e.g. M4 compounds H1, so they share S0).

## Stages

### S0 -- Honor the honest stop; detect test deletion anywhere (H1, M4)
**Findings:** H1 `[VERIFIED]`, M4 `[CONFIRMED]` (M4 compounds H1).
**Where:** `merge_check.py:106-111`; `local_merge.py:660` (`policy_ok = code == 0`),
`decide()` `local_merge.py:193-241`; `policy.py:212-218` (`deleted_test_files`).
**Direction:** the local authority must honor the recomputed decision *value*, not
just its consistency. Either `merge_check.check()` returns the recomputed
`MergeDecision` and `gather_gates`/`decide()` treat `stop_before_merge` as a hold,
or `gather_gates` independently recomputes `merge_decision` on the trusted tree and
feeds its stop reasons into `decide()`. `policy_ok` must never be True for a
`stop_before_merge`. Separately, derive the test location(s) from target policy so a
deletion under a target's configured test dir (`src/tests/`, `myapp/tests/`,
`frontend/__tests__/`, ...) raises `test_files_deleted`, not only literal `tests/`.
**Acceptance:**
1. A branch deleting a non-invariant test under `tests/` plus a benign non-sensitive
   change, whose VPS-committed decision is `stop_before_merge`, is **held, not
   merged**, even with L2 blast and green gates.
2. A branch carrying an honest `stop_before_merge` for a declared-high-risk verdict
   on non-sensitive paths is held, not merged.
3. `policy_ok` is False whenever the recomputed decision is `stop_before_merge`; a
   fabricated `auto_merge` over a real `stop` still trips the consistency mismatch.
4. A test deleted under a target's **configured** test dir (not literal `tests/`)
   raises `test_files_deleted`.

### S1 -- Executable specs get an agent eye and cannot self-promote (H2, H3, LOW)
**Findings:** H2 `[VERIFIED]`, H3 `[CONFIRMED]`, LOW "report-only path breadth".
**Where:** `target_policy.py:95` (`ENGINE_SAFE_GLOBS`), `policy.py:186`
(`_is_safe_by_construction`), `policy.py:207` (`classify_blast_radius`),
`policy.py:57-83` (`nondraft_report_specs`, `if f == own: continue`),
`policy.py:44,74` (`REPORT_ALLOWED_PREFIXES`).
**Direction:** specs are not inert. Exclude `.laddy/specs/**` from the L1 safe set so
a spec falls through to L2 (agents gate it) **and/or** wire draft/status checking
into the non-report path. Narrow the own-spec exemption so it cannot cover a diff
that flips `report_only` off, sets `status: ready`, or injects executable
instructions. Tighten `REPORT_ALLOWED_PREFIXES` to genuinely inert files (drop the
whole `docs/` subtree incl. `docs/conftest.py`, and non-`.md` files under
`.laddy/specs/`). Keep every fix engine-side.
**Acceptance:**
1. An all-markdown diff adding `.laddy/specs/next.md` with `status: ready` is **not
   L1** (routes to L2/L3 -> reviewed or held), never a zero-agent merge.
2. A report-only task whose diff flips its own spec's `report_only` off (or sets
   `status: ready`) yields `stop_before_merge`.
3. `REPORT_ALLOWED_PREFIXES` no longer admits `docs/conftest.py` or a non-`.md` file
   under `.laddy/specs/`.

### S2 -- Merging local main requires typing the exact task id (H4)
**Findings:** H4 `[VERIFIED]` -- POLICY DECISION resolved: **implement the documented
control** (Director decision, 2026-07-18). Also LOW "`--no-input` has no CLI-level
test".
**Where:** `local_merge.py:847` (`_interactive_confirm`, currently y/N), consulted
only for L3 at `run():346`; L1/L2 AUTO_MERGE merge with no prompt;
`scripts/merge-verified.sh`; `local_merge.py:988` (`--no-input`).
**Direction:** any merge side-effect into **local main** requires the operator to
type the **exact task id** (L1, L2, and L3 alike, not a y/N). A wrong or blank id
declines with a clear message and merges nothing. `--no-input` stays a **true dry
run**. Align CLAUDE.md's "Core invariants" and `CLAUDE/trust-and-merge.md` step 4
with the now-true behavior.
**Acceptance:**
1. A merge side-effect on local main requires the **exact** task id; a wrong or blank
   id declines and merges nothing.
2. The requirement holds for an L1/L2 AUTO_MERGE decision, not only L3.
3. `--no-input` merges nothing (a true dry run) -- asserted at the CLI level.
4. CLAUDE.md and `CLAUDE/trust-and-merge.md` no longer describe a control that does
   not exist; they match the implemented task-id confirmation.

### S3 -- Untrusted branch content cannot spoof verdict / neutralization / classification (H6, H7, H8)
**Findings:** H6 `[CONFIRMED]`, H7 `[VERIFIED]`, H8 `[CONFIRMED]`.
**Where:** `verdict.py:108` (`extract_json`); `local_merge.py:480-504`
(`_UNTRUSTED_AGENT_CONFIG` / `_neutralize_agent_config`) vs `target_policy.py:52-58`
(`**/CLAUDE.md`, `**/AGENTS.md` sensitive); `policy.py:124-126` (`sensitive_paths`),
`policy.py:186` (`_is_safe_by_construction`) -- case-sensitive `fnmatch` on
case-insensitive DrvFs.
**Direction:**
- H6: take the **last** balanced top-level JSON object (or require the verdict be the
  sole/final/delimited object) so a planted `APPROVED` quoted before the real verdict
  cannot masquerade as it.
- H7: `_neutralize_agent_config` must **recurse** -- remove nested `**/CLAUDE.md`,
  `**/AGENTS.md`, `**/GEMINI.md`, `**/.mcp.json`, and nested `.claude/` / `.codex/`
  dirs -- matching the sensitive-glob set. Working tree only (never a commit), so
  classification is unaffected and the diff still shows the files (routing L3).
- H8: match the engine-generic classification globs **case-insensitively** (casefold
  both path and glob) without loosening any intended case distinction.
**Acceptance:**
1. Reviewer output with a planted `APPROVED` block before the real
   `CHANGES_REQUESTED` -> the **real** verdict wins.
2. A branch shipping `pkg/CLAUDE.md` and `pkg/.mcp.json` -> both **absent** from the
   review worktree, while the diff still shows them (routes L3).
3. `Claude.md` / `.Claude/hooks` classify as **sensitive (L3)**, not L1, on a
   case-insensitive filesystem.

### S4 -- The sanctioned `--local` fix path can actually pass `merge_check` (H5)
**Findings:** H5 `[CONFIRMED]` -- reproduce first (touches trust logic).
**Where:** `merge_check.py:43` (`missing_merge_decision`), `:75-79`
(`state_sha_mismatch`).
**Direction:** under `--local`, the state/decision equality checks must accommodate
the trusted Director fix commit: a fix on top of the branch advances `code_sha` while
the VPS-written `state.json head_sha` cannot, and a fix authored off local main has no
`merge-decision.json`. Re-derive gate state from the fixed tree (route trusted, sha
judged==merged, dirty-tree guarded) rather than requiring the VPS `state.json` to
match -- **without** opening a non-local hole.
**Acceptance:**
1. `--local <sha>` with a one-line fix commit on top of a held branch -> `check()`
   **passes** given green gates (today it cannot).
2. The non-`--local` (VPS-authored) path is unchanged: a genuine `state_sha_mismatch`
   / `missing_merge_decision` still holds.
3. A dirty tree under `--local` is still refused (no silent bypass).

### S5 -- Classify against the right baseline; do not drop other tasks' files (M1, M2)
**Findings:** M1 `[VERIFIED, partial]`, M2 `[CONFIRMED]`.
**Where:** `local_merge.py:659` (`merge_check(base="origin/main")`), `:664`
(`GitOps(default_branch="main")`), `config.default_branch`/`base_branch`;
`gitops.py:49` (`policy_pathspec ':(exclude).laddy/tasks'`); `gitops.py:181`
(`_range()`).
**Direction:** thread `base_branch` / `config.default_branch` through instead of the
literals `"origin/main"` and `"main"`, and align the baselines a run uses so
classification is not computed off a possibly-stale `origin/main`. **Do not**
implement M1's "changes escape classification" framing -- the audit marks it an
**overclaim**; the `origin/main...HEAD` range is over-inclusive (conservative), not
fail-open. For M2, stop blanket-excluding `.laddy/tasks` from classification:
classify anything a branch plants outside its **own** task dir.
**Acceptance:**
1. A target whose default branch is **not** literally `main` classifies against the
   correct ref (no hardcoded `"main"` / `"origin/main"`).
2. A branch touching `.laddy/tasks/<other-task>/x.py` is **classified/held**, not
   silently integrated unclassified.
3. No behavioral widening of the diff range beyond today's conservative
   over-inclusion.

### S6 -- Merge-verified tripwire catches hub-main deletion and rewind (M3)
**Findings:** M3 `[VERIFIED for delete]`.
**Where:** `local_merge.py:451` (`fetch --prune`, missing-ref-returns-True), `:453`
(ancestor check).
**Direction:** distinguish "never had a main" from "main disappeared / was rewound."
Remember the last-seen hub-main sha (or fetch without pruning main and compare), so a
compromised VPS deleting `refs/heads/main` is not indistinguishable from a benign
fresh hub, and a force-push that rewinds hub main to an older commit does not still
satisfy the ancestor check. Defense-in-depth; the per-branch gate still holds.
**Acceptance:**
1. A hub-main **deletion** trips the tripwire (not treated as a fresh hub).
2. A hub-main **rewind** (force-push to an older commit) trips the tripwire.
3. A genuinely fresh hub (never had main) is still handled without a false trip.

### S7 -- Target classification completeness (M5, M6)
**Findings:** M5 `[CONFIRMED]`, M6 `[CONFIRMED]`.
**Where:** `target_policy.py:133-135` (`all_safe_globs`); `target_policy.py:64`
(supply-chain manifests only at repo root).
**Direction:**
- M5: validate engine-side that a target's `safe_globs` resolve to **inert**
  extensions -- a target can only add inert catalogues, never widen L1 to cover a
  `.py` (or other code). Reject / refuse-to-L1 an entry matching code.
- M6: add nested + lockfile variants so `svc/requirements.txt`, `poetry.lock`,
  `uv.lock`, `package-lock.json`, and nested `package.json` are engine-sensitive.
**Acceptance:**
1. A target `safe_globs` entry matching a `.py` is **rejected / not routed L1**.
2. `svc/requirements.txt` and `poetry.lock` (and the other nested/lockfile variants)
   classify **sensitive**.

### S8 -- Per-task exception isolation (M7)
**Findings:** M7 `[CONFIRMED]` -- contradicts "a hold never blocks the others".
**Where:** `local_merge.py:343-372` (`run()` has no per-task try/except);
`merge_check.py:41` (`json.loads` raises; only SpecError/OSError caught).
**Direction:** wrap each task in `run()` so a failure (e.g. a malformed / truncated
`merge-decision.json`) becomes a **BROKEN hold for that task** and the batch
continues, instead of one bad branch aborting the whole run.
**Acceptance:**
1. A truncated `merge-decision.json` on one of three ready tasks -> that task holds
   **BROKEN**; the other two still process to completion.
2. The BROKEN hold records why (the parse failure), per derive-don't-store.

### S9 -- Fail-safe validation at the library boundaries (M8, LOW, LOW)
**Findings:** M8 `[CONFIRMED]`, LOW "raise_flag wrong kind set", LOW
"spec_is_high_risk regex".
**Where:** `policy.py:145-146` (`effective_risk` returns a raw out-of-enum string;
consumer `risk == "high"` at `:309`); `flags.py:145` (validates against `FLAG_KINDS`
incl. `ORACLE_ESCAPE` instead of `LOOP_FLAG_KINDS`); `policy.py:99` (`_PATH_TOKEN_RE`
requires a `/`).
**Direction:**
- M8: normalize / validate the declared risk to the enum, fail-safe to `high`, so
  `HIGH` / `critical` actually raise the `high_risk` stop.
- `raise_flag`: validate against `LOOP_FLAG_KINDS` at the **library** boundary so a
  forged `ORACLE_ESCAPE` cannot pass there (not only guarded by argparse).
- `spec_is_high_risk`: also match **bare** sensitive filenames (`pyproject.toml`,
  `.env`, `CLAUDE.md`) so a slashless sensitive path in a spec flags high-risk.
**Acceptance:**
1. A declared `risk_level: "HIGH"` / `"critical"` produces the `high_risk` stop.
2. `raise_flag` refuses an `ORACLE_ESCAPE` at the library boundary.
3. A spec naming a slashless sensitive path (`pyproject.toml`, `.env`, `CLAUDE.md`)
   flags high-risk.

### S10 -- Quality and cohesion cleanup (remaining LOW)
**Findings:** LOW bucket (non-correctness / convention).
**Where / items:**
- **ASCII-safe violations:** leftover non-ASCII markers -- a Czech word at
  `local_merge.py:88,685` and `policy.py:184`, an em-dash at `policy.py:165`, and a
  section-sign glyph at `merge_check.py:6`. Replace with ASCII.
- **`merge_branch` dirty-tree misreport** (`local_merge.py:765`): the normal path has
  no dirty guard, so a dirty index is misreported as "branch no longer applies
  cleanly". Add a dirty-tree guard / distinct message on the normal path too.
- **Module cohesion / typed models:** `GitOps` mixes provisioning + stateless diff
  helpers (`gitops.py:52`) -- split; `verdict.py:259` `request_payload` /
  `RETRY_TEMPLATE` is a generic retry core mislocated in the verdict-schema module --
  relocate; type `destructive_migrations` / `migration_texts: Any` (`policy.py:222`)
  and `committed` / `state: Any` (`merge_check.py:41`).
**Acceptance:**
1. `rg -n '[^\x00-\x7f]'` over the touched engine sources is clean (LF + ASCII).
2. A dirty index on the `merge_branch` normal path yields a **distinct** message (not
   the false "no longer applies cleanly").
3. Relocated / split modules keep behavior identical (existing tests green); the newly
   typed boundaries pass `basedpyright` at 0 errors.

## Acceptance criteria (whole task -- Definition of Done)
1. Every stage S0..S10 is implemented and **all** its listed acceptance criteria
   hold, each backed by its own test.
2. All eight HIGH (H1-H8), all eight MEDIUM (M1-M8), and the LOW bucket are closed;
   none of the "Rejected -- do NOT chase" items were touched.
3. Full gate green on the final branch: `ruff check .` clean, `basedpyright` 0
   errors, `pytest -n auto -q` green.
4. Invariants preserved: fail-closed guards, derive-don't-store, injected clock,
   typed boundaries, LF + ASCII-safe, no engine guard a target can weaken.
5. H4 is implemented (task-id confirmation) and CLAUDE.md /
   `CLAUDE/trust-and-merge.md` match the code.

## Rejected -- do NOT chase (carried from the audit)
These were adjudicated and **refuted**; "fixing" one is wrong.
- `local_merge.py:665` "blast classification escapes entirely" / critical --
  overclaim; the `origin/main...HEAD` range is over-inclusive, not fail-open (the
  real, narrower issue is M1 -> S5).
- `merge_check.py:41` typed-`Any` "as a bug" -- style-only; handled under S10.
- `target_policy.py:138` `myapp()` sample in a prod module -- intentional test
  fixture; the docstring covers it.
- `merge_check.py:83` "only catches inconsistent fabrication" -- misframes
  `check()`'s purpose (it is the consistency recompute; gate authority is elsewhere).
- `local_merge.py:699` rw2 L2-only -- by design: L3 goes to a human (more scrutiny),
  not less. (The digest "all gates passed" wording is a separate optional doc nit.)

## Notes
- **Dogfooding.** The code this task changes *is* laddy's own merge/trust engine, so
  the run exercises the very gates being fixed. The whole change is L3 and stops
  before merge -- on return the Director types the task id (post-S2 that is the
  enforced control) and reads the diff.
- **Fable developer.** Set `ROLE_DEVELOPER_MODEL=claude-fable-5` at kickoff (see Run
  configuration). rw1/rw2 stay as-is.
- **Reproduce CONFIRMED items.** H3, H5, H6, H8 and every M* are `[CONFIRMED]`, not
  `[VERIFIED]` -- reproduce against the code before committing each fix.
- **If the loop hits the cap.** A `cap_reached` means the task was too big for the
  configured `MAX_LOOPS`; on return, raise the cap and resume, or split the remaining
  stages into a follow-up task -- never force a merge to finish it.
