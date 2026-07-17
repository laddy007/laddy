# Merge hold: mcp  (blast L3, broken)

## What failed

- security panel blocker(s): security panel member 'claude' did not return a valid verdict; holding for human review; The production TOTP seed is committed and the leak scanner is explicitly bypassed.; A six-digit network authentication factor is accepted without throttling or replay protection.; Saved note contents default to group/other-readable permissions.

## Security panel findings

- security panel member 'claude' did not return a valid verdict; holding for human review
- The production TOTP seed is committed and the leak scanner is explicitly bypassed.
- A six-digit network authentication factor is accepted without throttling or replay protection.
- Saved note contents default to group/other-readable permissions.

## What is needed

This change is not mergeable as-is. Re-run the task on the VPS to fix
the failing gate(s), or address them and push a new revision of the
branch. `mcp` is NOT merged and NOT deleted.
