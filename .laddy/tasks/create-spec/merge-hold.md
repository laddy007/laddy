# Merge hold: create-spec  (blast L3, risk_decision)

## Sensitive surface touched

- `orchestrator/run.py`
- `scripts/create-spec.sh`

## Waived judgment-gate findings (--advisory)

- security panel blocker(s): The launcher can execute branch-controlled Claude startup configuration on the Director's trusted machine.; The untrusted change contains prior-sign-off claims capable of steering the trusted review.

The deterministic gates passed, but the security panel / rw2
flagged the above and they are being WAIVED by --advisory: this
is a risk call on a change that is NOT fully verified. The waived
findings are recorded durably in merge-advisory.md.

## Your decision

Merge `create-spec` into main under --advisory? Type the
exact task id to merge; anything else declines - you decide
on this summary, not by reading the diff.
