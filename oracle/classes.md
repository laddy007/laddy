# Oracle escape-class registry

Registered slugs for classifying oracle escapes (design
`docs/development/superpowers/specs/2026-07-12-self-improvement-oracle-design.md`).
The oracle classifies findings ONLY into slugs listed here; a new class
starts with a commit adding its line (convergence R2: a discriminator is a
shared constant, never free text). Recurrence (>= 2 escapes in one class,
see `orchestrator/oracle/escapes.py`) marks a confirmed upgrade target;
one escape is a hypothesis.

Format: `` - `slug` — one-line definition `` (parsed by
`orchestrator/oracle/classes.py`; slugs are kebab-case).

Seed classes (from the 7-bug incident and the audit-prompt taxonomy):

- `failure-mode` — missing or wrong handling of an error/failure path the change introduces
- `edge-case` — wrong behavior on a boundary or degenerate input within the changed feature
- `cross-module` — the change breaks an interaction with a module outside the diff's focus
- `contract-violation` — a declared contract (docstring/spec/invariant) the implementation does not uphold
- `design-approach` — the implementation approach contradicts spec or architecture intent (judgment class)
- `regression` — the diff breaks existing behavior or an invariant elsewhere; ALWAYS an escape even when the task spec is silent about that area
