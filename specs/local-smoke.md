---
status: done
type: feature
---
# Local dev-loop quick-reference doc

## Goal
Create a new documentation file `docs/development/agent-loop-local-quickref.md`
that gives a short (under 25 lines) quick reference for running the agent
dev-loop **locally** (in WSL): how to kick a task and how to merge it. Keep it
factual and point at `.laddy/USAGE.md` for the full guide.

## Acceptance
- New file `docs/development/agent-loop-local-quickref.md` exists and is Markdown.
- It names the two commands:
  - `.laddy/scripts/local-task.sh <task>` — run the convergence loop locally,
  - `.laddy/scripts/merge-verified.sh <task>` — gate + merge the result.
- It states that the loop runs in WSL on an ext4 clone (not /mnt/c).
- No source code is changed anywhere else (docs-only).
- Under 25 lines total.
