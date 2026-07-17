# Merge hold: local-gate-advisory  (blast L3, broken)

## What failed

- security panel blocker(s): security panel member 'claude' did not return a valid verdict; holding for human review - output still malformed after 2 retries: missing required key: verified; Advisory recording follows a branch-controlled symlink on the Director's trusted machine

## Security panel findings

- security panel member 'claude' did not return a valid verdict; holding for human review - output still malformed after 2 retries: missing required key: verified
- Advisory recording follows a branch-controlled symlink on the Director's trusted machine

## What is needed

This change is not mergeable as-is. Re-run the task on the VPS to fix
the failing gate(s), or address them and push a new revision of the
branch.

Or fix it right here on the trusted machine and re-judge locally:
commit the fix ON TOP of this branch with ordinary git, then run
`merge-verified.sh <task> --local <ref>` (a sha, branch, or worktree
path). --local does not trust the code more - it trusts the route:
you are the trusted author and the IDENTICAL gate still judges the
diff, and the judged sha is the merged sha, so nothing unverified
slips in. It is a stopgap until bounce-to-VPS exists (and a
legitimate escape hatch after).

`local-gate-advisory` is NOT merged and NOT deleted.
