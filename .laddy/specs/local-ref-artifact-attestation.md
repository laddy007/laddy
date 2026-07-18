---
type: fix
roles: [developer, rw1, rw2]
risk: high
---
# local-ref-artifact-attestation -- do not compare a trusted local fix to stale VPS state

## Goal

Make `merge-verified.sh <task> --local <ref>` judge a Director-authored code
commit without failing `state_sha_mismatch` merely because the inherited VPS
artifacts describe the older task tip. Preserve the complete trusted-local gate
and leave the default fetched-branch artifact check unchanged and fail-closed.

## Root cause

`merge_check.check()` is an attestation over the VPS-authored artifact chain. It
checks that `state.json`, the append-only review/gate log, and
`merge-decision.json` describe the code history at the fetched task tip. That is
load-bearing on the default remote path.

The `--local` recipe deliberately adds a Director-authored code commit on top of
a held task. `GitOps.code_sha()` therefore moves to the new commit while the
inherited `state.json.head_sha` remains pinned to the last VPS developer commit.
`gather_gates()` nevertheless calls the same VPS artifact attestation, producing
`state_sha_mismatch` before the fresh trusted-local gate can authorize the local
revision. Rewriting only `state.json` is not a remedy: the log's SHA-keyed
approvals remain stale and the recompute correctly fails next.

The conceptual bug is treating one result as "policy passed" in both modes.
For a fetched VPS tip the artifact attestation is applicable and binding. For a
Director-authored local revision it is not applicable; the local revision is
authorized by the trusted policy/classification, trial merge, binding suite,
security panel, rw2 where applicable, and the judged-SHA merge pin.

## Scope

In:

- Model the VPS artifact attestation explicitly as `passed`, `failed`, or
  `not_applicable`; do not encode N/A as a fake successful boolean.
- Default/fetched mode continues to call `merge_check.check()` exactly once.
  A missing, forged, or stale artifact chain remains a deterministic BROKEN
  hold.
- `--local` marks only that VPS artifact attestation N/A. It does not call the
  attestation against the newer local code SHA.
- The trusted target policy is still loaded from current local `main`; blast
  classification, sensitive-path derivation, trial merge, binding suite,
  diff-coverage, semgrep, gitleaks, infra-override guard, security panel, rw2,
  dirty-tree guard, tripwire, and verified-SHA merge pin are unchanged.
- Correct the `lokal-changes` wording that called the inapplicable VPS artifact
  attestation part of an identical gate set.

Out:

- No rewrite or regeneration of `state.json`, the iteration log, reviewer
  verdicts, or `merge-decision.json`.
- No change to `merge_check.check()` itself or to the VPS loop.
- No bypass for a remote task branch, no new override flag, no push, and no
  merge behavior change.
- No advisory-mode work; atomic advisory recording and terminal-safe rendering
  are a separate next step.

## Behaviour

For the normal remote route:

```
fetched task tip -> VPS artifact attestation -> passed or BROKEN
```

For the trusted local-fix route:

```
Director local commit -> VPS artifact attestation N/A
                      -> trusted policy/classification
                      -> trial-merged binding gate
                      -> local judgment gates
                      -> merge the exact judged SHA
```

`not_applicable` is neutral, not green: human-facing diagnostics and typed gate
results must not claim that the stale artifact chain passed. Only `failed`
blocks; `passed` is possible only after the real artifact check ran.

## Acceptance criteria

1. A local code commit on top of a task whose `state.json.head_sha` describes
   the older VPS code produces `state_sha_mismatch` when passed directly to the
   existing `merge_check.check()`, but `gather_gates(..., local_ref=...)` marks
   the VPS artifact attestation `not_applicable` and continues through the fresh
   trusted-local gates.
2. The same `merge_check` collaborator is not called at all in `--local` mode;
   asserted with a collaborator that raises if invoked.
3. Default/fetched mode still calls the collaborator. A returned
   `state_sha_mismatch` becomes a failed attestation and a BROKEN verdict; it is
   never treated as N/A.
4. A red binding suite in `--local` still holds BROKEN. Security-panel and rw2
   blockers still hold, scan/coverage failures still hold, and an infra override
   still holds. Existing tests for these gates remain green.
5. Trusted policy/classification still uses local `main`, and an L3 local change
   still reaches the Director risk decision only after every applicable gate is
   green.
6. The exact local SHA remains both judged and merged; dirty-tree, tripwire,
   no-fetch, no-push, and default-path behavior are unchanged.
7. The result model has no ambiguous boolean pair or freeform dictionary: the
   three attestation states are explicit and exhaustively named.
8. `ruff check .`, `basedpyright` (0 errors), and `pytest -n auto -q` pass; source
   and specs remain LF and ASCII-safe.

## Review notes

- Reject a blanket skip of `merge_check` shared by both modes. The remote path
  must still detect forged or stale VPS artifacts.
- Reject updating `state.json.head_sha` to the local commit. It would fabricate
  approval state and still leave the log's gate SHAs stale.
- Reject `policy_ok=True` as the representation of N/A. The distinction is the
  fix: a check that did not run must never be reported as passed.
- The fresh local gates are not a substitute invented here; they are the
  existing authority of `--local`. This change only stops applying a historical
  VPS-attestation precondition to a newer trusted-local commit it cannot
  describe.
