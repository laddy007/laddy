# Merge hold: create-spec  (broken - engine failure)

## What failed

- RuntimeError("git worktree add --detach /mnt/c/myprogramfiles/laddy-merge-work/verify-create-spec d63c2e63bce52242bc31c3ed1ecb7d0b58730ec6 failed: fatal: '/mnt/c/myprogramfiles/laddy-merge-work/verify-create-spec' already exists\n")

The merge ENGINE failed while processing this task - an engine-side
error (e.g. a malformed committed artifact), NOT a policy stop and
NOT a gate verdict on the change: no gate result exists for it.

## What is needed

Inspect the error above and the branch's committed artifacts; push a
fixed revision of the branch (or fix the engine defect) and re-run.
The other ready tasks were processed normally.

`create-spec` is NOT merged and NOT deleted.
