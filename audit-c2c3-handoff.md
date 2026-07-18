# Handoff: trust/merge/policy core hardening (audit C2+C3)

**For:** an implementing agent working on the laddy engine.
**Source:** a report-only audit of `orchestrator/{local_merge,merge_check,merge_subject,gitops,policy,verdict,target_policy,flags}.py`. Findings below are **confirmed** (refuted/rejected ones are listed at the end so you don't chase them).
**Confidence tags:** `[VERIFIED]` = hand-checked against the code end-to-end; `[CONFIRMED]` = adjudicated but not independently repro'd — reproduce before committing a fix.

## Constraints (laddy DoD — non-negotiable)
- Engine changes are **L3** (sensitive surface). Every behavior change **ships with a test** (untested behavior is undefined).
- Preserve invariants: fail-closed engine guards, derive-don't-store, injected clock, typed models over freeform dicts, **LF + ASCII-safe** source, files split rather than grow.
- Gate: `ruff check .` clean, `basedpyright` 0 errors, `pytest -n auto -q` green.
- **Do not** change what a target can weaken; engine-generic guards stay engine-side.
- Several items are one theme (**auto-merge without a human eye on executable specs**: H1–H4). They can be fixed together but each has its own test.

---

## HIGH

### H1 — `merge_check` discards the stop DECISION, keeps only its consistency `[VERIFIED]`
- **Where:** `merge_check.py:106-111`, consumed at `local_merge.py:660` (`policy_ok = code == 0`), decided in `decide()` `local_merge.py:193-241`.
- **Defect:** `check()` returns exit 0 whenever `recomputed.decision == committed.decision` — including `stop_before_merge == stop_before_merge`. `gather_gates` reads only the exit code; the decision value (echoed in the message) is never parsed, and `decide()` never inspects `stop_before_merge`. An **honestly-computed stop whose reason has no independent local manifestation** is silently merged when the change is L2 and the gates are green. Leaking reasons: `test_files_deleted`, `senior_escalation_without_clean_verdict`, declared `high_risk` on non-sensitive paths, `destructive_migrations` on a target whose migration glob isn't also sensitive. (Fabrication — VPS writes `auto_merge`, real=`stop` — IS caught by the mismatch; the hole is the *honest* stop.)
- **Repro sketch:** branch deletes a non-invariant test + benign non-sensitive change; VPS honestly commits `merge-decision.json` `decision=stop_before_merge`; `discover_ready` lists it; `check()` recomputes stop, matches, exits 0 → `policy_ok=True`; blast=L2; suite green (the failing test is gone); rw2/security miss it → `decide()` → AUTO_MERGE.
- **Direction:** make the local authority honor the decision *value*. Either (a) `merge_check.check()` returns the recomputed `MergeDecision` and `gather_gates`/`decide()` treat `stop_before_merge` as a hold, or (b) `gather_gates` independently recomputes `merge_decision` on the trusted tree and feeds its stop reasons into `decide()`. `policy_ok` must not be True for a `stop_before_merge`.
- **Test:** branch with a deleted `tests/…` test (and separately, a declared-high-risk verdict on non-sensitive paths) → held, not merged, gates otherwise green.

### H2 — Specs are executable but ride the L1 no-review lane `[VERIFIED]`
- **Where:** `target_policy.py:95` (`ENGINE_SAFE_GLOBS`), `policy.py:186` (`_is_safe_by_construction`), `policy.py:207` (`classify_blast_radius` → L1), `local_merge.py:694/699` (panel/rw2 gated to L2/L3), `decide()` `:241`.
- **Defect:** `.laddy/specs/*.md` matches `("*.md","**/*.md")` → classified L1 → security panel and rw2 both skipped → merges with zero agent review. A `status: ready` spec is an executable task the loop autonomously runs on the next kickoff (`run.py` enqueue; see `nondraft_report_specs` docstring). The draft-status guard runs **only** on the report-only path; the normal path never checks it. Violates "code is never L1".
- **Direction:** specs are not inert — exclude `.laddy/specs/**` from the L1 safe set so a spec falls through to L2 (agents gate it), **and/or** wire spec draft/status checking into the non-report path (`classify_blast_radius` or `merge_decision`). Keep the fix engine-side (specs are an engine-known surface).
- **Test:** an all-markdown diff that adds `.laddy/specs/next.md` with `status: ready` → not L1 (→ L2/L3, reviewed or held).

### H3 — Own-spec exemption lets a report-only task escape its sandbox `[CONFIRMED]`
- **Where:** `policy.py:57-83` (`nondraft_report_specs`, `if f == own: continue`).
- **Defect:** the task's own spec is exempt from the draft requirement by filename, so a report-only (audit/investigate) task can rewrite `.laddy/specs/<task>.md` — flip `report_only` off, set `status: ready`, inject instructions — and land it. Compounds H2.
- **Direction:** the own-spec exemption must not cover content that changes `report_only`/`status`/executable instructions. Limit the exemption (e.g. own spec may only be touched while staying a draft / same report_only status), or detect the report_only→off flip as a violation.
- **Test:** report-only task whose diff flips its own spec's `report_only` off (or `status: ready`) → `stop_before_merge`.

### H4 — Documented "type the exact task id" merge confirmation does not exist `[VERIFIED]` — POLICY DECISION
- **Where:** `local_merge.py:847` (`_interactive_confirm`, y/N), consulted only for L3 at `run():346`; L1/L2 AUTO_MERGE merges with no prompt; `scripts/merge-verified.sh` is a thin `exec` with no prompt of its own.
- **Defect:** CLAUDE.md ("Core invariants" + Action authority) and `CLAUDE/trust-and-merge.md` step 4 assert *"merging into local main requires typing the exact task id."* In code, L1/L2 merge with **no human interaction at all**, and even L3 is a `y/N`, never a task-id. Both halves of the documented control are false.
- **This is a policy call, not a pure bug:** decide with the Director whether to (a) implement the documented confirmation (always-confirm before any local-main merge, typing the exact task id; `--no-input` stays a true dry run), or (b) correct the docs to match current behavior. Do **not** silently pick one.
- **Test (if fixing code):** a merge side-effect on local main requires the exact task id; a wrong/blank id declines; `--no-input` merges nothing.

### H5 — The `--local` fix path can never pass `merge_check` `[CONFIRMED]`
- **Where:** `merge_check.py:43` (`missing_merge_decision`), `:75-79` (`state_sha_mismatch`).
- **Defect:** a Director fix committed on top of the branch advances `code_sha` while the VPS-written `state.json head_sha` cannot → `state_sha_mismatch`; a fix authored off local main (the documented worktree recipe / tests' `_local_fix_commit`) → `missing_merge_decision`. So the sanctioned "fix locally, re-judge with `--local`" escape hatch always ends in a BROKEN hold that re-running can't clear.
- **Direction:** under `--local`, the state/decision equality checks must accommodate the trusted Director fix commit (the route is trusted, the sha is judged==merged, dirty-tree guarded). Re-derive gate state from the fixed tree rather than requiring the VPS `state.json` to match — without opening a non-local hole. Reproduce first; this touches trust logic.
- **Test:** `--local <sha>` with a one-line fix commit on top of a held branch → `check()` passes given green gates (currently it cannot).

### H6 — `extract_json` trusts the FIRST balanced JSON object `[CONFIRMED]`
- **Where:** `verdict.py:108`.
- **Defect:** a schema-valid `APPROVED` verdict planted in branch content and quoted by the reviewer before its real verdict is silently substituted.
- **Direction:** take the **last** balanced top-level object, or require the verdict be the sole/final object / delimited, so quoted content can't masquerade as the verdict.
- **Test:** reviewer output containing a planted `APPROVED` block before the real `CHANGES_REQUESTED` → real verdict wins.

### H7 — Nested agent-config survives neutralization `[VERIFIED]`
- **Where:** `local_merge.py:480-504` (`_UNTRUSTED_AGENT_CONFIG` / `_neutralize_agent_config`), vs `target_policy.py:52-58` (`**/CLAUDE.md`, `**/AGENTS.md` are sensitive).
- **Defect:** neutralization strips only top-level `.claude`/`CLAUDE.md`/`AGENTS.md`/`.mcp.json`/`.codex`, but the CLIs auto-ingest nested steering files and `ENGINE_SENSITIVE_GLOBS` itself flags `**/CLAUDE.md`. A branch-shipped `subdir/CLAUDE.md` (or `subdir/.mcp.json`) survives into the review worktree where the reviewer CLIs run on the trusted machine.
- **Direction:** `_neutralize_agent_config` must recurse — remove nested `**/CLAUDE.md`, `**/AGENTS.md`, `**/GEMINI.md`, `**/.mcp.json`, and nested `.claude/`/`.codex/` dirs — matching the sensitive-glob set. Working tree only (never a commit), so classification stays unaffected.
- **Test:** branch with `pkg/CLAUDE.md` and `pkg/.mcp.json` → both absent from the review worktree; the diff still shows them (routes L3).

### H8 — Case-sensitive `fnmatch` on case-insensitive DrvFs `[CONFIRMED]`
- **Where:** `policy.py:124-126` (`sensitive_paths`), `:186` (`_is_safe_by_construction`) — `fnmatch` is case-sensitive on Linux; the Director's repo is on case-insensitive `/mnt/c` DrvFs.
- **Defect:** a case-variant agent-config file (`Claude.md`, `.Claude/`) misses `ENGINE_SENSITIVE_GLOBS` and, for `.md`, matches `ENGINE_SAFE_GLOBS` → routed to L1 no-review, while the real file it shadows on disk is agent config.
- **Direction:** match classification globs case-insensitively (casefold both path and glob) on the sensitive/safe path, at least for the engine-generic globs. Confirm it doesn't loosen any intended case distinction.
- **Test:** `Claude.md` / `.Claude/hooks` classify as sensitive (L3), not L1.

---

## MEDIUM

- **M1 — `origin/main` vs local-main baseline `[VERIFIED, partial]`.** `local_merge.py:659` hardcodes `merge_check(base="origin/main")` and `:664` `GitOps(default_branch="main")`, ignoring `config.default_branch`/`base_branch`; the binding gate + policy load use `base_sha`=local main. Real problems: (a) **breaks on any target whose branch isn't literally "main"**; (b) `merge_check` recomputes policy from a possibly-stale `origin/main`; (c) three baselines in one run = fragile. **NOTE:** the "changes escape classification" framing is an **overclaim — do not implement it as a security fix.** `gitops.py:181 _range()="origin/main...HEAD"` and origin/main is always *behind* local main → the diff is over-inclusive (conservative), not fail-open. Fix = thread `base_branch`/`config.default_branch` through instead of the literals; align the baselines. Test: a non-"main" default branch classifies against the right ref.
- **M2 — `.laddy/tasks` excluded wholesale `[CONFIRMED]`.** `gitops.py:49` `policy_pathspec ':(exclude).laddy/tasks'` drops files a branch plants in *other* tasks' dirs from classification, yet `merge_branch` integrates the full verified sha → that content lands unclassified. Direction: don't blanket-exclude other tasks' dirs from classification, or classify anything outside the current task's own dir. Test: branch touching `.laddy/tasks/<other>/x.py` is classified/held.
- **M3 — Tripwire gaps `[VERIFIED for delete]`.** `local_merge.py:451`: `fetch --prune` + missing-ref-returns-True makes a compromised VPS *deleting* `refs/heads/main` indistinguishable from a benign fresh hub. Also `:453`: a force-push that *rewinds* hub main to an older commit still satisfies the ancestor check. Direction: distinguish "never had a main" from "main disappeared/rewound" (e.g. remember last-seen hub-main sha, or fetch without pruning main and compare). Defense-in-depth; per-branch gate still holds. Test: a hub-main deletion and a rewind each trip the tripwire.
- **M4 — `deleted_test_files` hardcodes `tests/` `[CONFIRMED]`.** `policy.py:212-218` → deletions under `src/tests/`, `myapp/tests/`, `frontend/__tests__/` never raise `test_files_deleted`. Compounds H1. Direction: derive the test location(s) from target policy, not a hardcoded prefix. Test: a deletion under a target's configured test dir raises the stop.
- **M5 — Target `safe_globs` merged with no inertness validation `[CONFIRMED]`.** `target_policy.py:133-135` (`all_safe_globs`) lets a target's toml route non-inert files into L1. Direction: validate that `safe_globs` resolve to inert extensions engine-side (a target can only add inert catalogues, never widen L1 to code). Test: a target `safe_globs` entry matching a `.py` is rejected / not L1.
- **M6 — Supply-chain manifests only at repo root `[CONFIRMED]`.** `target_policy.py:64` — nested `backend/requirements.txt` and lockfiles (`package-lock.json`, `poetry.lock`, `uv.lock`) aren't engine-sensitive, while JS `**/package.json` is. Direction: add nested + lockfile variants for the Python/other ecosystems. Test: `svc/requirements.txt` and `poetry.lock` classify sensitive.
- **M7 — No per-task exception isolation `[CONFIRMED]`.** `local_merge.py:343-372` `run()` has no try/except per task; `check()` parses branch JSON with a raising `json.loads` (`merge_check.py:41` catches only SpecError/OSError). One malformed branch aborts the whole batch — contradicts "a hold never blocks the others". Direction: wrap each task in `run()` so a failure becomes a BROKEN hold for that task and the batch continues. Test: a truncated `merge-decision.json` on one of three ready tasks → that one holds BROKEN, the others still process.
- **M8 — `effective_risk` out-of-enum string `[CONFIRMED]`.** `policy.py:145-146` orders an unknown declared risk as high (rank 2) but returns the raw string; the only consumer is `risk == "high"` (`:309`), so `'HIGH'`/`'critical'` outranks everything yet never adds the `high_risk` stop. Direction: normalize/validate the declared-risk value to the enum (fail-safe to "high"). Test: declared `risk_level: "HIGH"`/`"critical"` produces the `high_risk` stop.

---

## LOW (quality / convention)
- **ASCII-safe violations:** `NÁLEZ` at `local_merge.py:88,685` and `policy.py:184`; em-dash `policy.py:165`; `§` `merge_check.py:6`. Replace with ASCII (leftover annotations).
- **`merge_branch` dirty-tree misreport** `local_merge.py:765`: no dirty guard on the normal path; a dirty index is misreported as "branch no longer applies cleanly" → the Director re-runs a whole VPS task for what needed `git stash`. Add a dirty-tree guard / distinct message on the normal path too.
- **`spec_is_high_risk` regex** `policy.py:99`: `_PATH_TOKEN_RE` requires a `/`, so slashless sensitive paths (`pyproject.toml`, `.env`, `CLAUDE.md`) in a spec never flag high-risk. Also match bare sensitive filenames.
- **Report-only path breadth** `policy.py:44,74`: `REPORT_ALLOWED_PREFIXES` allows the whole `docs/` subtree (incl. `docs/conftest.py`) and non-`.md` files under `.laddy/specs/`. Tighten to inert files.
- **`raise_flag` wrong kind set** `flags.py:145`: validates against `FLAG_KINDS` (incl. `ORACLE_ESCAPE`) instead of `LOOP_FLAG_KINDS` — the library boundary permits a forged oracle-escape; only argparse guards it. Validate against the loop set at the library boundary.
- **Module cohesion / typed models:** `GitOps` mixes provisioning + stateless diff helpers (`gitops.py:52`); `verdict.py:259` `request_payload`/`RETRY_TEMPLATE` is a generic retry core mislocated in the verdict-schema module; `destructive_migrations`/`migration_texts: Any` (`policy.py:222`) and `committed`/`state: Any` (`merge_check.py:41`) should be typed. `--no-input` has no CLI-level test (`local_merge.py:988`).

---

## Rejected — do NOT chase (already refuted)
- **`local_merge.py:665` "blast classification escapes entirely" / critical** — overclaim; the `origin/main...HEAD` range is over-inclusive, not fail-open (see M1 for the real, narrower issue).
- **`merge_check.py:41` typed-`Any` as a bug** — style-only; tracked under LOW, not a correctness defect.
- **`target_policy.py:138` `myapp()` sample in prod module** — intentional test fixture; docstring covers it.
- **`merge_check.py:83` "only catches inconsistent fabrication"** — misframes `check()`'s purpose (it's the consistency recompute; gate authority lives elsewhere).
- **`local_merge.py:699` rw2 L2-only** — by design: L3 goes to a human (more scrutiny), not less. (The digest wording "all gates passed" is a separate, minor doc nit if you want it.)
