# Merge hold: fix/merge-flow-queue  (blast L3, broken)

## What failed

- local full test suite is red
- diff-coverage below threshold: branch does not merge cleanly into current main (conflict); re-run the task
- security scan flagged 1 item(s): branch does not merge cleanly into current main (conflict); re-run the task
- gate infra changed by this branch was NOT verified - the gate ran trusted main's copy of: .laddy/security/gitleaks.toml
- security panel blocker(s): The --local re-judge fails OPEN on a malformed spec: SpecError is caught alongside OSError and collapsed to report_only=False, disabling the path_guard and nondraft-spec guards that the non-local route fails closed on.; director_resume is trusted from the branch-writable iteration-log.jsonl with no authenticity anchor, so the untrusted developer agent can forge the Director's resume channel.; --chain treats MERGE_DECIDED:stop_before_merge as predecessor... [truncated]

## Security panel findings

- The --local re-judge fails OPEN on a malformed spec: SpecError is caught alongside OSError and collapsed to report_only=False, disabling the path_guard and nondraft-spec guards that the non-local route fails closed on.
- director_resume is trusted from the branch-writable iteration-log.jsonl with no authenticity anchor, so the untrusted developer agent can forge the Director's resume channel.
- --chain treats MERGE_DECIDED:stop_before_merge as predecessor success, so a chained successor is built on a branch the policy explicitly withheld from auto-merge and launders its reviewer-declared risk.
- Codex instruction injection remains possible through AGENTS.override.md.
- The network-facing TOTP authentication endpoint has no online-guessing controls.
- A new trusted runtime dependency is resolved from an unconstrained, unhashed package specification.

## Local test failure (tail)

```
branch does not merge cleanly into current main (conflict); re-run the task
```

## What is needed

This branch changes the gate's own infrastructure, which the gate
restores from trusted main before it runs, so re-running does not clear it:
the next run restores the same paths. No gate here can judge the branch's
own copy - landing those paths is your call, on a route you trust.

Any red gate above may be the restore's doing rather than a defect:
the suite ran against main's infra, not this branch's.

`fix/merge-flow-queue` is NOT merged and NOT deleted.
