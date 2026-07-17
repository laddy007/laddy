---
type: feature
risk: high
---

# local-gate-advisory — `merge-verified --advisory`: document judgment-gate findings, don't dead-end

## Problem

The local merge authority (`orchestrator/local_merge.py`, driven by
`scripts/merge-verified.sh`) holds **BROKEN** on any judgment-gate blocker — the
security panel or the rw2 re-run. The VPS loop that produced the branch already
ran a full test suite plus rw1 and rw2, so the local adversarial panel is
routinely the layer that dead-ends an otherwise-good branch. The only escape
today is a hand-merge that bypasses the gate entirely — losing the record of
what the panel found and defeating the point of the gate.

There is no way to say: *"record what the panel/rw2 flagged, keep it, but merge
anyway."*

## Goal

Add an **opt-in `--advisory` mode** to `merge-verified` that **waives the
judgment gates** (security panel + rw2): their findings are **documented durably
in local main** and the merge proceeds. The **deterministic gates always still
fail closed** — trusted-infra re-derivation is never waived.

## Constraints (invariants — a reviewer MUST reject any violation)

1. **Deterministic gates are NEVER waivable.** Only the security panel and the
   rw2 re-run may be waived by `--advisory`. The policy recompute, local full
   test suite, diff-coverage, and secret/FS scan (gitleaks/semgrep), plus the
   infra-override guard, still produce a BROKEN hold even under `--advisory`.
   (In `decide()` today these are the deterministic block at
   `local_merge.py:197-216`; the judgment gates are `217-223`.)
2. **Opt-in, default off.** Without `--advisory`, behavior is byte-identical to
   today. The flag is the only thing that changes the decision.
3. **Durable, branch-independent record.** When an `--advisory` merge lands, the
   waived findings are written to `.laddy/tasks/<task>/merge-advisory.md` and
   **committed into local main**, so the record survives deletion of the task
   branch and can be cleaned up later.
4. **No push, no origin change.** `--advisory` only affects the local-main
   merge. Pushing to origin stays a separate Tier-3 Director decision.
5. **Honest labeling.** An advisory merge is visibly distinct in the run output
   (and its findings live in `merge-advisory.md`), never presented as a
   fully-verified merge.

## Scope

**In:** split `decide()` into deterministic (blocking) vs judgment (waivable
under the flag); carry the waived findings on the verdict; render + commit
`merge-advisory.md`; the `--advisory` CLI arg and its passthrough in
`merge-verified.sh`; a loud notice when advisory is on.

**Out:** bounce-to-VPS rework (separate, bigger); re-classifying which gates are
deterministic vs judgment; any change to the VPS loop (developer/rw1/rw2);
changing push behavior.

## Files / areas involved

- `orchestrator/local_merge.py` — `MergeVerdict`, `decide()`, the merge executor
  in `LocalMergeEngine.run()` (~`local_merge.py:330`), the CLI `main()` argparse.
- `scripts/merge-verified.sh` — already forwards `"$@"` (`:68`), so the flag
  passes through with no shell change.
- `tests/test_local_merge.py` — new tests (the `_gates(...)` helper builds
  `GateResults`; `decide()` is exercised directly).

## Acceptance criteria (each proven by a test, public API only)

1. Given gates with a **security-panel blocker** and everything else green:
   `decide(..., advisory_mode=True)` returns a **merge** verdict whose
   `advisory` tuple lists the waived finding; the same gates with
   `advisory_mode=False` return a **BROKEN** hold.
2. Given gates with **red tests**: `decide(..., advisory_mode=True)` still
   returns **BROKEN**. Likewise for a failing diff-coverage gate, a non-empty
   `scan_findings`, a policy-recompute mismatch, and a non-empty
   `infra_overridden`. (Deterministic gates fail closed under advisory.)
3. An `--advisory` merge writes `.laddy/tasks/<task>/merge-advisory.md`
   containing the waived findings **and commits it into local main** — asserted
   by inspecting main after the merge (the file is present from a fresh checkout
   of main that never had the task branch).
4. **Regression guard:** without `--advisory`, a security-panel blocker still
   returns a BROKEN hold and no `merge-advisory.md` is written or committed.
5. **L3 + advisory:** a sensitive (L3) branch whose only findings are
   judgment-gate findings, run under `--advisory`, still goes through the human
   RISK_DECISION Y/N; on confirm-merge the waived findings are recorded in
   `merge-advisory.md`. (I.e. the record is written on BOTH merge paths, not the
   auto-merge path only.)

## Implementation sketch (non-binding — the loop decides the details)

- `MergeVerdict` (frozen dataclass) gains an `advisory: tuple[str, ...] = ()`
  field — a backward-compatible addition.
- `decide(task_id, gates, *, advisory_mode: bool = False)`: collect the
  deterministic reasons and the judgment reasons into separate lists; the
  blocking set is the deterministic reasons always, plus the judgment reasons
  only when `not advisory_mode`; when waived, the judgment reasons go into the
  verdict's `advisory` field. `decide()` stays pure — it does NOT write files.
- **The durable write hangs on the merge executor** (`LocalMergeEngine.run()` /
  its `on_verdict` reporting), keyed on a **merged verdict with a non-empty
  `advisory`** — NOT inside `decide()`. This is what makes it fire on BOTH the
  auto-merge path and the `RISK_DECISION → confirmed` path (AC5). A naive
  implementation that wires the write only into the auto-merge branch would miss
  the L3 confirm path — do not do that. Preserve `verdict.advisory` when a
  confirmed RISK_DECISION verdict is turned into a merge.
- `--advisory` flag in `orchestrator.local_merge` argparse, wired into the
  engine; `merge-verified.sh` needs no change.

## Notes / expected friction (for reviewers and the Director)

- This change **edits the merge gate itself**. It does not trip the
  infra-override guard (only `.laddy/docker` + `.laddy/security` are restored
  infra via `RESTORED_INFRA_PATHS`; `orchestrator/*.py` is not), so the gate can
  judge it — but it will almost certainly classify **L3 (RISK_DECISION →
  Director Y/N)**, and the security panel MAY object to "this weakens the gate."
- That objection is **expected**. Landing this is the intended **last bootstrap
  hand-merge**: after `--advisory` exists, future changes flow through it with a
  documented record instead of dead-ending. Reviewers should judge whether the
  **trust invariant holds** (deterministic gates still fail closed; only
  panel/rw2 waived), not the mere fact that a gate became waivable.
