---
type: feature
roles: [developer, rw1]
risk: low
---
# smoke1 — docs-only smoke test that the dev-loop converges and pushes

## Goal

Prove the autonomous dev-loop runs end-to-end on the laddy-as-target dogfood
setup: developer makes a trivial change, fast tests + rw1 review pass, the
authoritative Docker gate is green, and the branch is pushed to the hub ready
to merge. This is the smallest safe change that exercises the whole path; it
lands nothing of substance.

## Scope

**In:** create exactly one new documentation file.

**Out:** any change to product/engine code, tests, config, or existing files.
Do not touch anything under `orchestrator/`, `scripts/`, `tests/`, `.laddy/`,
or any file other than the one named below. No dependency or tooling changes.

## Acceptance criteria

1. A new file `docs/smoke-test.md` exists.
2. Its entire content is exactly this single line (trailing newline only):
   `laddy autonomous dev-loop smoke test passed.`
3. No other file in the repository is created, modified, or deleted — the diff
   is exactly one added file.
