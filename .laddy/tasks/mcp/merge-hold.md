# Merge hold: mcp  (blast L3, broken)

## What failed

- policy recompute failed/mismatch: reason=state_sha_mismatch state=f33b69140f293ad375560fb2436f996ecfec9fa6 actual=a54d4ddf959330ca4480bddb40c943d52a406bfa
- security scan flagged 1 item(s): gitleaks flagged the change (exit 1)
- security panel blocker(s): Six-digit TOTP authentication is exposed without online-guess throttling or replay prevention; The server can start with an empty or trivially weak TOTP secret; Saved note contents can be exposed to other local VPS users; The new MCP runtime dependency floats to any future release

## Security panel findings

- Six-digit TOTP authentication is exposed without online-guess throttling or replay prevention
- The server can start with an empty or trivially weak TOTP secret
- Saved note contents can be exposed to other local VPS users
- The new MCP runtime dependency floats to any future release

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

`mcp` is NOT merged and NOT deleted.
