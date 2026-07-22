# Advisory merge: create-spec

This branch was merged under `--advisory`. The deterministic gates
(VPS artifact attestation when applicable, local test suite,
diff-coverage, secret/FS scan, and the infra-override guard) all passed,
but the JUDGMENT gates below were
WAIVED, not cleared. This is NOT a fully-verified merge: the findings
were recorded and the branch merged anyway, for later cleanup.

## Waived judgment-gate findings (security panel / rw2)

- security panel blocker(s): Attacker-controlled review artifacts claim prior approval and attempt to steer the trusted security decision.
