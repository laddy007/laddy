# Merge hold: engine-rest-fix  (blast L3, broken)

## What failed

- gate infra changed by this branch was NOT verified - the gate ran trusted main's copy of: .laddy/security/gitleaks.toml, .laddy/security/semgrep.yml
- security panel blocker(s): check_local_fix swallows SpecError as well as OSError, letting a branch disable the entire report-only guard by corrupting its own spec file - the inverse of merge_check.check, which hard-fails the same condition.; Hub branch names become task ids with no charset validation and are passed to git as bare positional arguments with no `--` separator, giving an untrusted VPS agent argument injection into git commands run on the Director's trusted machine.; A... [truncated]

## Security panel findings

- check_local_fix swallows SpecError as well as OSError, letting a branch disable the entire report-only guard by corrupting its own spec file - the inverse of merge_check.check, which hard-fails the same condition.
- Hub branch names become task ids with no charset validation and are passed to git as bare positional arguments with no `--` separator, giving an untrusted VPS agent argument injection into git commands run on the Director's trusted machine.
- A hub branch whose name collides with the base branch's tracking ref makes the discovery fetch fail outside the per-task isolation try block, wedging the entire merge gate until an operator intervenes manually.
- security panel member 'codex' did not return a valid verdict; holding for human review - output still malformed after 2 retries: agent run did not complete cleanly (exit_reason='error', rc=1); its output is not trustworthy

## What is needed

This branch changes the gate's own infrastructure, which the gate
restores from trusted main before it runs, so re-running does not clear it:
the next run restores the same paths. No gate here can judge the branch's
own copy - landing those paths is your call, on a route you trust.

Any red gate above may be the restore's doing rather than a defect:
the suite ran against main's infra, not this branch's.

`engine-rest-fix` is NOT merged and NOT deleted.
