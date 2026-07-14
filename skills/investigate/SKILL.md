---
name: investigate
description: Diagnosis-only investigation of a myapp problem — reproduce it
  (Playwright MCP), find the root cause with evidence, and save a findings
  report to .laddy/investigations/<name>.md. NO fixes, no product-code
  changes. Use when the Director reports a bug/symptom to diagnose
  ("prošetři", "zjisti proč", "investigate X"). Replaces the retired
  investigate.sh. Prefer reproducing on staging; never mutate production.
---

# investigate — diagnosis only, no fix

Investigation id: `<name>` (ask if not given; must match
`^[A-Za-z0-9._-]+$`). If the Director has not already said so, ask for the
exact symptom and target URL first.

Follow `.laddy/roles/investigator.md`. In short:

1. Run `/superpowers:systematic-debugging` and follow it.
2. Use the Playwright MCP tools (see the project `playwright` skill) to
   REPRODUCE the problem on the target page: load it, capture the failing
   network request(s) + responses and the console. Start logged out (that
   alone may reproduce it); only if the logged-in path is needed, arrange a
   staging test account with the Director.
3. Read the relevant frontend + backend code and find the ROOT CAUSE with
   evidence. Decide: real defect vs working-as-designed vs needs-more-info.

Hard rules:

- DIAGNOSIS ONLY — do not change any product code or apply a fix.
- Prefer staging for reproduction; never mutate production.
- Reproduction evidence (requests, tracebacks, console output) goes in the
  report text.

When done, save the findings report to `.laddy/investigations/<name>.md`
(create the directory if missing). If a fix task follows, offer to draft
its spec with the `create-spec` skill.
