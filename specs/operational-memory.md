---
type: feature
---

# operational-memory - git-tracked lessons for `.laddy` roles

## Authoritative context

`.laddy` roles are the canonical prompts. Legacy `.agent/memory/MEMORY.md` and
old role edits are design references only.

Relevant current files:

- `.laddy/roles/developer.md`
- `.laddy/roles/investigator.md`
- `.laddy/roles/rw1.md`
- `.laddy/roles/rw2.md`
- `.laddy/roles/security.md`
- `.laddy/roles/senior-reviewer.md`
- `tests/agent_orchestrator/test_role_prompts.py`

## Goal

Add a small git-tracked operational memory file for recurring lessons and make
roles read it before raw history when the task involves known past failure
patterns.

## Non-goals

- No vector DB or external memory store.
- No automatic memory writes by the loop.
- No runtime dependency on network services.
- No migration of every legacy `.agent` lesson.

## Scope

1. Create `.laddy/memory/MEMORY.md`.
2. Define a compact entry format with `Trigger`, `Lesson`, and `Use when`.
3. Seed only lessons that are still relevant to the current `.laddy` runtime.
4. Update role prompts to consult operational memory before raw history when a
   trigger matches.
5. Add prompt tests that keep the memory instruction present in relevant roles.

## Acceptance criteria

- Memory is plain Markdown and git-tracked.
- Entries are actionable and trigger-based, not a chronological diary.
- Developer, investigator, and reviewer roles mention memory lookup.
- Tests fail if role prompt references to operational memory are removed.
- No role tells agents to treat memory as higher authority than the current spec
  or repository rules.
