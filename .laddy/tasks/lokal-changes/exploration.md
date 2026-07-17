I have everything needed. Here is the scoping.

---

# Explorer summary — `lokal-changes`

## Findings

The spec's root-cause claims all check out against the code:

- **`main()`** (`orchestrator/local_merge.py:773`) wires `verify_one → gather_gates` (`:840`) and `merge_one → merge_branch` (`:844`). Both resolve the commit from the **remote**: `gather_gates` → `_branch_worktree` → `_git(repo,"fetch",branch_remote,task_id)` then worktree add at `{branch_remote}/{task_id}` (`:497,:502`); `merge_branch` also fetches (`:680`). So a local edit can never enter the gate — confirmed.
- **`build_digest`** BROKEN branch (`:164-169`) offers only "Re-run the task on the VPS … or … push a new revision of the branch." The VPS-rerun path is unbuilt; the push path implies the untrusted node. Confirmed dead end.
- **Fix-free invariant** is real and load-bearing: docstring `:6-7`, engine comments `:321`. Nothing to preserve here except *not adding* fix logic.
- **Judged==merged TOCTOU pin**: `gather_gates` sets `head_sha=verified_sha` (the worktree tip, `:637`); `engine.run` merges `gates.head_sha` (`:341`); `merge_branch` merges the sha, not the ref (`:678-693`, tested at `test_merge_pins_verified_sha_not_a_moving_branch`, `:707`). `--local` must keep this: worktree sha == merge sha, no re-resolution between.
- **Tripwire**: `hub_main_ancestor_of_local` (`:414`) short-circuits to `True` when the remote main ref is absent (`:430-431`), so a fresh/never-seeded hub already isn't a tripwire. The hard part is the *fetch* at `main():805` which uses `check=True` and would **raise** when the remote is absent/unreachable — that's what `--local` must soften.
- **Launcher** (`scripts/merge-verified.sh`): `"$@"` already forwards `--local` verbatim to argparse (`:54`). The only launcher blocker is the `die` on missing `AGENT_BRANCH_REMOTE` (`:45-46`), which must become a warning **only** when `--local` is present.

## One real ambiguity (needs a developer decision)

The spec's Behaviour recipe passes a worktree **path** (`merge-verified.sh <task> --local ../fix`), but AC#1 says `<ref>` "resolves in the target repo." **These conflict:** I verified that `git -C <repo> rev-parse ../fix^{commit}` fatals on a worktree path — a path is not a rev. A plain `rev-parse` handles a sha/branch/tag but **not** the spec's own `../fix` example.

**Recommended resolution (robust, small):** resolve `<ref>` by trying `git -C <repo> rev-parse --verify "<ref>^{commit}"` first (sha/branch/tag); on failure, if `Path(ref)` is a directory, fall back to `git -C <ref> rev-parse HEAD` and verify that sha exists in the target repo's object store (shared via the worktree). This satisfies both the recipe and AC#1. If the developer instead restricts `<ref>` to revs only, the spec's example must change to pass a branch name (`fix`) — flag that to the Director rather than shipping an example that errors.

## Affected files

- `orchestrator/local_merge.py` — the mode, worktree-from-local-sha, no-fetch merge, tripwire softening, digest.
- `scripts/merge-verified.sh` — soften the remote-missing `die` under `--local`.
- `tests/test_local_merge.py` — pure/seam + CLI-stub tests (existing style).
- **New** `tests/test_merge_verified_launcher.py` (or similar) — the AC#10 `PYTHON_BIN` recording-stub test (no such test file exists today; launcher tests here are text-grep style, but AC#10 requires argv capture + warn-vs-die, so a real subprocess invocation is needed).

## Proposed approach

1. **Worktree seam.** Extract the "add detached worktree at a sha + `_neutralize_agent_config`" tail of `_branch_worktree` into a helper `_worktree_at_sha(repo, task_id, work_root, sha)`. `_branch_worktree` = `fetch` + resolve `{remote}/{task}` → sha → helper. Add `_local_worktree(repo, task_id, work_root, ref)` = resolve `<ref>` (per the resolution above) → sha → helper, **no fetch**. Keeps `_binding_on_merged_tree` / trial-merge / gate set byte-identical.

2. **`gather_gates`.** Add `local_ref: str | None = None`. When set, source the worktree via `_local_worktree` instead of `_branch_worktree`; everything downstream (trial-merge into current main, `merge_check`, policy from trusted `base_branch`, binding gate, panel, rw2, `head_sha=verified_sha`) is unchanged. This is the key to "same gate set, no weakening."

3. **`merge_branch`.** Add `fetch: bool = True`; `--local` passes `fetch=False` (skip `:680`). Sha is already local. TOCTOU pin unchanged.

4. **`main()`.**
   - Add `--local <ref>` (a `store` string arg).
   - **Arg rules (AC#6):** with `--local`, require exactly one `task` and a non-empty `<ref>`; else `parser.error(...)` (non-zero, nothing merged).
   - **Dirty-tree guard (AC#5):** if `--local` and `git -C repo status --porcelain` is non-empty → print "commit or stash first", return non-zero, before any gate.
   - **Tripwire (AC#7):** branch on mode. Default: keep `fetch(check=True)` + abort-2. `--local`: `fetch(check=False)`; if it succeeded → run `hub_main_ancestor_of_local`, abort-2 if not ancestor; if it failed (absent/unreachable) → warn and proceed.
   - **Wiring:** `list_ready = lambda: [task]` (bypass `discover_ready`, AC#3); `verify_one = gather_gates(..., local_ref=ref)`; `merge_one = merge_branch(..., fetch=False)`.
   - Leave `confirm`/`_ask`/push offer and dry-run path untouched (L3 y/N and Tier-3 push unchanged).

5. **`build_digest`.** In the BROKEN `else` block (`:164`), add the `--local` local-fix guidance alongside the existing VPS-rerun line, with the trust framing ("trusts the route, not the code; same gate; stopgap until bounce-to-VPS exists"). Note the `infra_overridden` branch (`:150`) deliberately omits the rerun advice — add the local route to the **generic** branch only.

6. **Launcher.** Scan `"$@"` for `--local`; when present, replace the missing-remote `die` (`:45-46`) with a `WARN` and continue. `"$@"` already forwards `--local <ref>`.

## Acceptance-criterion tests (contracts to write first)

- **AC#1/#3:** `main(["--local", ref, task], ...)` with `gather_gates` monkeypatched to capture its `local_ref`/sha and a `discover_ready` fake that raises if called → asserts local sha judged, discovery never consulted. (Follow the existing `_fake_gather` monkeypatch pattern, `:583`.)
- **AC#2:** green `--local` verdict → `merge_one` receives exactly `gates.head_sha`; task lands in local `main`.
- **AC#4:** reuse `_gates(...)` fixtures — a security/rw2 blocker or red suite → BROKEN, no merge; fully green → merge. Same gate set as remote path.
- **AC#5:** dirty target tree → non-zero, nothing merged, message names commit/stash.
- **AC#6:** `--local` with 0 or ≥2 task ids, or no `<ref>` → non-zero `parser.error`.
- **AC#7:** three separate cases — reachable+diverged→rc 2; absent/unreachable→warns+judges; default path still aborts.
- **AC#8:** existing `local_merge` tests unmodified and green (default path).
- **AC#9:** a BROKEN digest string contains the `--local` guidance **and** the VPS-rerun line.
- **AC#10:** invoke `scripts/merge-verified.sh <task> --local <ref>` via subprocess with `PYTHON_BIN` → recording stub capturing argv, in a repo with no `AGENT_BRANCH_REMOTE`: asserts `--local <ref>` forwarded and a warning (not a die); without `--local`, still dies.
- **AC#11:** grep/behavioural guard — `--local` path never pushes origin/GitHub, never stubs/skips a gate, merges only the judged sha, never writes the hub.

## Risks

1. **`<ref>` resolution (highest).** The path-vs-rev ambiguity above. Pick the robust resolution or get the spec example corrected — otherwise the documented recipe errors on first use.
2. **Trial-merge base drift.** `_binding_on_merged_tree` merges `verified_sha` into **current local main** and baselines coverage/scans to it (`:562-565`). Because the fix commit sits *on top of the VPS branch*, the diff/coverage baseline still spans the whole change (main→fix) — correct per spec, but verify the local fix commit shares history with main (it will, being fetched from the same hub), else the trial-merge conflicts and reports BROKEN. Worth an explicit test note.
3. **No-fetch object availability.** `--local` skips the fetch, so the sha must already be in the target repo's object store. A worktree HEAD or committed branch satisfies this; a bare sha the Director never committed does not — the dirty-tree guard + "resolves in the repo" covers it, but the resolution helper must fail cleanly (non-zero) on an unresolvable ref.
4. **Blast radius = the merge authority itself (L3).** Per the spec note, don't assume the blast is the new mode only — run the **default-path** existing tests (`:427-940`) to prove no regression; this change is judged by exactly the gate it edits.
5. **Digest branch placement.** Adding the local route to the `infra_overridden` branch would resurrect the "rerun that cannot help" bug guarded by `test_infra_override_digest_does_not_advise_a_rerun_that_cannot_help` (`:142`). Add to the generic `else` only.

No product code was changed; reads/analysis only.