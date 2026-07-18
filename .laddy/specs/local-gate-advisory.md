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
   rw2 re-run may be waived by `--advisory`. The VPS artifact attestation when
   applicable, local full test suite, diff-coverage, and secret/FS scan
   (gitleaks/semgrep), plus the infra-override guard, still produce a BROKEN
   hold even under `--advisory`.
   (In `decide()` today these are the deterministic block at
   `local_merge.py:197-216`; the judgment gates are `217-223`.)
2. **Opt-in, default off.** Without `--advisory`, gate decision semantics are
   unchanged. The flag is the only thing that waives a judgment gate; the
   addendum's safe renderer and pre-commit merge executor apply in both modes.
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
   `scan_findings`, a failing applicable VPS artifact attestation, and a
   non-empty `infra_overridden`. (Deterministic gates fail closed under
   advisory.)
3. An `--advisory` merge writes `.laddy/tasks/<task>/merge-advisory.md`
   containing the waived findings **in the same merge commit as the code** —
   asserted by inspecting main after the merge (the file is present from a fresh
   checkout of main that never had the task branch).
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
- **The durable write belongs inside the pre-commit merge executor.**
  `LocalMergeEngine.run()` creates one typed request from the verified SHA and
  the final verdict, including its advisory tuple. The executor stages the
  trusted record before creating the merge commit. This request is created on
  BOTH the auto-merge and the `RISK_DECISION -> confirmed` path (AC5). Preserve
  `verdict.advisory` when a confirmed RISK_DECISION verdict becomes a merge.
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

## Security-hardening addendum (binding)

The original implementation sketch placed `merge-advisory.md` in a follow-up
commit after the waived branch had already entered trusted local `main`. That
ordering is unsafe. The task branch controls its artifact tree and can place a
symlink or another invalid object at the record path. A safe refusal to follow
that path would then leave the security-waived code in `main` without the
durable authorization record. Reviewer summaries are also attacker-influenced
data and must not reach the Director's terminal authorization prompt verbatim.

This addendum supersedes any earlier wording that implies a post-merge record
callback or follow-up advisory commit.

### A. Atomic advisory merge

1. The mutating boundary receives one typed request containing the task id, the
   exact verified SHA, and any waived findings. Advisory data must not travel to
   a callback that runs after the merge commit exists.
2. The executor checks out trusted `main` and runs
   `git merge --no-ff --no-commit <verified-sha>`. While `HEAD` is still the
   original trusted-main commit, it safely writes and stages only
   `.laddy/tasks/<task>/merge-advisory.md`.
3. The executor creates one merge commit only after the record write and
   staging succeed. The successful merge commit contains both the verified
   branch code and its trusted advisory record; no follow-up record commit is
   allowed.
4. A merge conflict, symlinked artifact path or parent, pre-existing record,
   write failure, staging failure, or commit failure aborts the uncommitted
   merge. The original trusted-main SHA remains unchanged and no external
   symlink target is modified. `reset --hard` is not an acceptable rollback.
5. A non-advisory merge retains the same verified-SHA TOCTOU pin, no-fetch local
   mode, merge subject, and push separation. It may share the safer no-commit
   executor but must not invent an advisory record.

Acceptance tests must prove that a successful advisory operation produces a
single two-parent merge commit containing both code and record, and that a
branch-provided final or parent symlink makes the operation hold with the
pre-merge main SHA and external target unchanged.

### B. Terminal-safe human rendering

1. Reviewer summaries, scan messages, branch-controlled paths, task ids, and
   other branch-influenced display fields are untrusted. Their raw structured
   verdict data remains unchanged for forensic use; only derived human output
   is rendered.
2. One shared inline renderer is used by the L3 digest, hold/advisory markdown,
   dry-run output, and terminal status lines. It collapses ordinary whitespace
   and newlines to one line, makes C0/C1 controls, ESC/CSI/OSC components,
   carriage return, backspace, and Unicode format/bidi controls visible as
   ASCII escape spellings, and enforces an explicit maximum rendered length.
3. The actual `input()` authorization prompt is a static trusted string. It
   contains no task id, reviewer text, path, or other dynamic value.
4. Tests cover at least CSI, OSC 52, carriage return, backspace, a newline that
   attempts to draw a fake prompt, a Unicode bidi control, and truncation. They
   also assert that the original parsed verdict summary was not rewritten.
