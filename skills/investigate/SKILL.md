---
name: investigate
description: Diagnosis-only investigation of a problem in the current target
  project — reproduce it, find the root cause with evidence, and save a
  findings report to .laddy/investigations/<name>.md. NO fixes, no
  product-code changes. Use when the Director reports a bug/symptom to
  diagnose ("prosetri", "zjisti proc", "investigate X"). Prefer reproducing
  on staging; never mutate production.
---

# investigate — diagnosis only, no fix

Investigation id: `<name>` (ask if not given; must match
`^[A-Za-z0-9._-]+$`). If the Director has not already said so, ask for the
exact symptom (and target URL, for a web project) first.

Follow the engine's `roles/investigator.md` (wherever the laddy engine is
checked out). In short:

1. Run `/superpowers:systematic-debugging` if available and follow it.
2. REPRODUCE the problem first. For a web target use the Playwright MCP
   tools if the project provides them (load the page, capture the failing
   network request(s) + responses and the console; start logged out — that
   alone may reproduce it; only arrange a staging test account with the
   Director if the logged-in path is needed). For a CLI/library target,
   reproduce with a minimal command or test.
3. Read the relevant code and find the ROOT CAUSE with evidence. Decide:
   real defect vs working-as-designed vs needs-more-info.

Hard rules:

- DIAGNOSIS ONLY — do not change any product code or apply a fix.
- Prefer staging for reproduction; never mutate production.
- Reproduction evidence (requests, tracebacks, console output) goes in the
  report text.

When done, save the findings report to `.laddy/investigations/<name>.md`
(create the directory if missing). If a fix task follows, offer to draft
its spec with the `create-spec` skill — an `investigate`-type dev-loop
spec is the unattended alternative when no interactive session is needed.
