# Merge hold: engine-rest-fix  (blast L3, broken)

## What failed

- local full test suite is red
- diff-coverage below threshold: branch does not merge cleanly into current main (conflict); re-run the task
- security scan flagged 1 item(s): branch does not merge cleanly into current main (conflict); re-run the task
- gate infra changed by this branch was NOT verified - the gate ran trusted main's copy of: .laddy/security/gitleaks.toml
- security panel blocker(s): save_note's TOTP check has no rate limiting, lockout, backoff, or failed-attempt logging, so the only credential on a publicly exposed write endpoint is brute-forceable.; The public TOTP endpoint permits unlimited online guesses and token replay.; A credential identified as the production TOTP secret remains committed in the tip and history.; The shell-config hardening omits tracked vps.conf and local.conf.; Branch-writable handoff fields bypass terminal... [truncated]

## Security panel findings

- save_note's TOTP check has no rate limiting, lockout, backoff, or failed-attempt logging, so the only credential on a publicly exposed write endpoint is brute-forceable.
- The public TOTP endpoint permits unlimited online guesses and token replay.
- A credential identified as the production TOTP secret remains committed in the tip and history.
- The shell-config hardening omits tracked vps.conf and local.conf.
- Branch-writable handoff fields bypass terminal-control neutralization.
- The new MCP runtime dependency is installed without a version bound, lockfile, or hashes.

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

`engine-rest-fix` is NOT merged and NOT deleted.
