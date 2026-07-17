---
type: feature
roles: [developer, rw1, rw2]
risk: high
---
# lokal-changes -- judge a locally-authored fix of a held task through the full local gate

## Goal
Give the Director a way to **fix a task that the local gate held as BROKEN,
right here on the trusted machine**, and get that fix judged and merged --
without a VPS round trip. The intended long-term route for a rejected task is
to bounce it back to the VPS loop, but that path is not built yet, so today a
BROKEN hold is a dead end: the digest says "re-run the task on the VPS", the VPS
re-run does not exist, and any edit made locally is invisible because
`merge-verified.sh` re-derives every gate from the **remote** branch sha it
fetches -- it never looks at the Director's working copy.

The insight that keeps this small and trust-safe: `merge-verified.sh` is already
the **binding** gate. It re-runs the full test suite, `rw2` (cross-vendor),
the security panel, and the policy/blast/coverage recompute on trusted infra.
The VPS `rw1`/`rw2` were only ever advisory -- never binding -- so a fix judged
by the local gate is **fully judged**. Skipping the VPS reviews loses nothing
that the trust model relies on. The only missing mechanical piece is: let the
local gate judge a **locally-committed revision** of the held task instead of
re-fetching the remote branch.

## Root-cause context
The root cause is already established; this task needs no exploration.

- `main()` (`orchestrator/local_merge.py`) wires `verify_one` to `gather_gates`
  and `merge_one` to `merge_branch`; both resolve the branch by
  `git fetch <branch_remote> <task>` (`_branch_worktree` at the verify step,
  `merge_branch` at the merge step). The commit judged and merged is therefore
  always the **remote** sha, so a local edit cannot enter the gate.
- The BROKEN digest (`build_digest`) only offers "Re-run the task on the VPS to
  fix the failing gate(s), or ... push a new revision of the branch." The first
  clause points at an unbuilt path; the second implies the untrusted node.
  Neither helps the Director who is standing at the trusted machine with the
  held branch in front of them.
- `local_merge` is deliberately **fix-free** ("It NEVER edits code ... there is
  no fix path"). This task must keep that invariant: the Director authors the
  fix **by hand with ordinary git**; the engine still only *judges* -- it simply
  judges a local ref instead of a fetched one. The engine never edits code.

## Scope
**In:**
- A single-task **local mode** on `orchestrator.local_merge`, selected by a new
  `--local <ref>` argument (with exactly one task id). In this mode:
  - the gate worktree is built from the **local** sha that `<ref>` resolves to
    in the target repo -- `git worktree add --detach <wt> <sha>` with **no
    preceding `fetch <branch_remote> <task>`** -- and the **same** gate set runs
    (binding suite + `rw2` + security panel + policy/coverage recompute, agent
    config neutralised exactly as today);
  - on a green verdict, the **same judged sha** is merged into local `main`
    (the verify->merge TOCTOU pin still holds: judged sha == merged sha), with
    no fetch;
  - discovery is bypassed: only the one named task is processed.
- **Dirty-tree guard:** `--local` refuses (non-zero, nothing merged) if the
  target repo has uncommitted changes, so what is judged is exactly a real
  commit and nothing uncommitted can be smuggled into the merged tree.
- **Tripwire softening in `--local` mode only:** run
  `hub_main_ancestor_of_local` when the branch remote is reachable (cheap safety
  is kept), but an **absent/unreachable** remote **warns and proceeds** instead
  of aborting -- the whole point of the mode is "no VPS".
- `scripts/merge-verified.sh`: forward `--local`; when `--local` is present,
  soften the hard "branch remote missing" **die** to a **warning** (the mode
  must run with no hub configured).
- `build_digest` (BROKEN branch): add the local-fix route alongside the existing
  VPS-rerun line, so the digest no longer points only at an unbuilt path.
- Tests under `tests/` (pure/seam-level, plus a CLI passthrough stub, matching
  the existing `local_merge`/launcher test style).

**Out:**
- **No fix logic in `local_merge`.** The "there is no fix path" invariant stands
  -- the Director edits by hand; the engine only judges a local ref. Do not add
  any code that edits, patches, or authors the branch.
- **No hub write / no push-back.** The local fix is not pushed to the hub; no
  new remote branch, no `main` write anywhere but local. The stale hub `<task>`
  branch is left untouched and cleaned up later by the normal GitHub
  push/cleanup step.
- **No gate weakened.** `--local` runs the identical full gate; a green is a
  real green and a red is a real BROKEN hold. No skipping tests, `rw2`, the
  security panel, coverage, or the policy/blast recompute. The L3 risk-decision
  `y/N` flow is unchanged (a sensitive-but-green local fix still gets the same
  prompt).
- **No change to the default (discovery/batch) path.** `merge-verified.sh` with
  no `--local` behaves exactly as today, including the hard tripwire abort and
  the remote-required die.
- **No VPS/bounce-back work.** VPS `rw1`/`rw2` are simply not consulted (they
  were advisory, never binding); building the bounce-to-VPS route stays a
  separate future task.
- No change to `merge_branch`'s verified-sha/TOCTOU semantics on the remote
  path, to `push_and_cleanup`, or to the end-of-run GitHub push (Tier-3 `y/N`,
  unchanged).

## Behaviour
Director-side recipe (the fix authoring is plain git, not engine code):

```
git fetch <laddy> <task>                 # bring the held branch local
git worktree add ../fix <FETCH_HEAD>     # or: git switch -c fix FETCH_HEAD
# ... edit, then commit the fix ON TOP of the VPS work ...
git -C ../fix commit -am "fix: ..."
merge-verified.sh <task> --local ../fix  # judge THIS commit, merge if green
```

- `merge-verified.sh <task> --local <ref>` forwards to
  `orchestrator.local_merge --local <ref> <task>`. `<ref>` is any local revision
  (a sha, branch, or worktree HEAD) that resolves in the target repo; it is the
  fix commit, which sits on top of the VPS branch so the diff/coverage baseline
  still spans the whole change.
- The gate builds its worktree from that local sha (no fetch) and runs the same
  binding suite + `rw2` + security panel + policy/coverage recompute. The verdict
  is computed the same way:
  - **green** -> the **same sha** is merged into local `main` (`--no-ff`), and
    the end-of-run GitHub push offer behaves exactly as today (Tier-3 `y/N`);
  - **BROKEN** -> the usual diagnostic digest; nothing merged. The Director can
    edit again and re-run `--local`.
- **Trust framing (must be preserved and stated in the digest/docstring):**
  `--local` does not trust the code more, it trusts the *route*. The Director is
  the trusted author, and the identical gate still judges the diff. The judged
  sha is the merged sha, so no unverified commit can slip in.
- **Tripwire:** in `--local` mode, when the branch remote is reachable and hub
  `main` is not an ancestor of local `main`, the run still aborts (the tamper
  signal is real regardless of mode). When the remote is absent/unreachable, the
  run prints a warning and proceeds -- the mode is designed to work with no hub.
- **Dirty tree:** if the target repo working tree has uncommitted changes,
  `--local` refuses before running any gate, with a message telling the Director
  to commit or stash first. This keeps judged == merged.

## Acceptance criteria
Tests build the log/tools from fakes and drive the public `main()` with a
recording stub (as existing `local_merge` tests do); no real LLM/git-push.

1. **Judges the local sha, not a fetched one.** `main(["--local", <ref>,
   <task>], ...)` builds the gate worktree from the sha `<ref>` resolves to in
   the repo and never calls the remote-branch fetch/`discover_ready` for that
   task -- asserted via a stubbed verify path that captures the sha it was asked
   to judge.
2. **Merges exactly the judged sha.** On a green `--local` verdict, `merge_one`
   is invoked with the **same** sha that was judged (TOCTOU pin) and the task
   lands in local `main`; asserted on the sha passed to the merge seam.
3. **Discovery bypassed.** `--local` processes only the one named task;
   `discover_ready` is not consulted -- asserted with a fake that would raise if
   called.
4. **Full gate still runs.** A `--local` run whose stubbed gates carry a
   security/`rw2` blocker or a red suite yields a **BROKEN** hold (not a merge);
   a fully-green stub merges. The gate set is identical to the remote path --
   asserted by reusing the existing gate-result fixtures.
5. **Dirty-tree guard.** `--local` on a repo whose working tree has uncommitted
   changes exits non-zero, merges nothing, and names the fix (commit/stash);
   asserted per that condition.
6. **Argument rules.** `--local` with zero or more-than-one task id, or with no
   `<ref>`, is refused with a clear non-zero error; asserted per case.
7. **Tripwire softened only in `--local`.**
   - remote reachable + hub `main` not an ancestor of local `main` + `--local`
     -> still aborts with the tripwire code (2), nothing merged;
   - remote absent/unreachable + `--local` -> warns and proceeds to judge;
   - the **default** (no `--local`) path still aborts on the same tripwire.
   Each asserted separately.
8. **Default path unchanged.** `main([<task>], ...)` and the no-arg discovery run
   behave exactly as before `--local` existed (existing `local_merge` tests stay
   green, unmodified).
9. **Digest points at the local route.** A BROKEN digest contains the
   `--local` local-fix guidance in addition to the existing VPS-rerun line;
   asserted on the digest string.
10. **Launcher forwards and softens.** `merge-verified.sh <task> --local <ref>`
    forwards `--local <ref>` to `orchestrator.local_merge`, and with `--local`
    present a missing `AGENT_BRANCH_REMOTE` remote **warns** instead of dying;
    without `--local` it still dies as today -- asserted with a `PYTHON_BIN`
    recording stub capturing argv and a repo with no such remote.
11. **Trust is not weakened (grep/test).** The `--local` path does not push to
    origin/GitHub, does not skip or stub any gate, does not merge a sha other
    than the one judged, and does not write the hub. The security-panel /
    fail-closed contract is untouched.
12. Suite green for the touched scope: `ruff check .` clean, `basedpyright` at 0
    errors, `pytest -n auto -q` green; LF + ASCII-safe preserved.

## Notes
- **Keep the fix-free invariant literal.** The temptation is to have `--local`
  "help" -- stage, auto-commit, or cherry-pick the edit. Do not. The engine's
  only new capability is *sourcing the judged/merged commit from a local ref
  instead of a fetched one*. The Director's git is the fix path; `local_merge`
  stays a judge. A reviewer should reject any code that edits the branch.
- **Judged == merged is load-bearing.** The remote path merges the verified sha
  precisely to close the verify->merge TOCTOU. `--local` must preserve that: the
  worktree sha that the gates saw is the sha handed to the merge, with no
  re-resolution of a moving ref in between. The dirty-tree guard exists for the
  same reason -- so nothing uncommitted rides along.
- **Why the tripwire still fires when the hub is reachable.** A diverged hub
  `main` is a tamper signal about the whole trusted/untrusted wiring, not about
  the branch being merged; if the Director's machine can see it, honouring it
  costs nothing and refusing a real tamper is the safe default. It is softened
  to a warning only when there is no hub to consult -- the explicit "no VPS"
  case this mode serves.
- **This is a stopgap, honestly named.** Bounce-to-VPS remains the intended
  route for a rejected task; `--local` is the trusted-machine escape hatch until
  it exists (and a legitimate one after, since the Director is a trusted author
  judged by the same gate). Say so in the digest wording so the operator knows
  both routes.
- **Blast radius.** This edits the engine's merge authority
  (`orchestrator/local_merge.py`, `scripts/merge-verified.sh`) -- itself L3. The
  change is judged by exactly the gate it modifies, so verify the default path's
  existing tests still pass rather than assuming the blast radius is the new mode
  only.
