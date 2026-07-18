# Merge hold: lokal-changes  (blast L3, broken)

## What failed

- security panel blocker(s): The local mode fails open on every fetch error and bypasses available hub-main tamper evidence.

## Security panel findings

- The local mode fails open on every fetch error and bypasses available hub-main tamper evidence.

## What is needed

This change is not mergeable as-is. Re-run the task on the VPS to fix
the failing gate(s), or address them and push a new revision of the
branch. `lokal-changes` is NOT merged and NOT deleted.
