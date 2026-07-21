---
type: audit
roles: [investigator, rw1, rw2]
risk: high
report_only: true
status: draft
---
# engine-rest-audit -- report-only security/correctness audit of the laddy engine outside the C2+C3 core

## Goal
Produce a **report-only** findings handoff (`audit-engine-rest-handoff.md`, repo
root) covering the parts of the laddy engine the C2+C3 audit did **not** touch.
C2+C3 audited the merge/trust core (`local_merge, merge_check, merge_subject,
gitops, policy, verdict, target_policy, flags`) and its findings shipped in
`fable-findings-fix` (this branch is based on that tip). This audit covers
**everything else**: the loop, the CLI/phases, the gate authority, agent I/O,
artifacts/state, queue/quota, spec/config parsing, the oracle, and the shell
layer. It is the first half of an audit -> fix -> independent-review -> fix
pipeline; this spec authors and runs the **audit only**.

This spec is **report-only**: it lands a findings document, changes **no engine
behavior**, ships **no test**, and touches **no source file**. The fixes are a
separate spec derived from the handoff.

## Non-goals / do not chase
- **No fixes here.** This run writes only the handoff doc. A fix that "looks
  cheap" is still out of scope -- record it, do not apply it.
- **Do not re-audit the C2+C3 core** except where a *rest* module calls into it
  in a way C2+C3 did not examine (e.g. how `loop.py` feeds `merge_decision`).
  The eight core modules' internals are already covered and fixed.
- **Do not re-open C2+C3's Rejected list** (in `audit-c2c3-handoff.md`). Those
  were adjudicated and refuted; re-filing one is a false finding.
- **Respect the engine/target split.** An engine-generic guard that fails closed
  and a target cannot weaken is *correct*, not a finding. A finding is where the
  engine fails **open**, trusts branch/agent content it should not, or lets a
  target weaken an engine guard.

## Confidence discipline (learned from C2+C3)
Every finding carries a confidence tag and the fixer reproduces before fixing:
- `[VERIFIED]` -- hand-checked against the code end to end, with a concrete
  path (file:line -> call site -> observable wrong outcome).
- `[CONFIRMED]` -- reasoned and adjudicated, not independently reproduced.
- **Overclaim discipline.** A conservative/over-inclusive behavior (fails
  closed, is merely redundant, or is defense-in-depth already backed by another
  gate) is **NOT** a fail-open finding. C2+C3 rejected several "escape"
  framings that were actually over-inclusion; do not repeat that error. If a
  path looks fail-open, trace it to an **observable** bad outcome or mark it
  `[CONFIRMED]` with the exact doubt stated. A plausible-but-wrong finding costs
  the fixer a wasted reproduce cycle -- prefer fewer, real findings.

## Threat model (what a finding must actually let happen)
The engine runs untrusted work on the Director's trusted machine and an
unattended VPS. A real finding lets one of these occur:
1. Untrusted **branch content** (files, specs, artifacts a task commits) changes
   an engine decision, reaches a trusted CLI as steering/config, or is trusted
   as authority (classification, verdict, risk, gate result).
2. Untrusted **agent output** (a developer/reviewer/investigator LLM response)
   is parsed as authority without validation, or injects instructions.
3. An engine guard **fails open** on error / missing / malformed input (an
   exception, absent artifact, or unparseable file leading to merge / L1 /
   pass / a lower lane instead of a hold).
4. A **target** weakens an engine-generic guard via its `.laddy/` config.
5. A **state/derive** violation: a stored status that can drift from the
   append-only log, a bare clock/sleep that breaks replay or lets time-based
   logic be gamed, or a non-idempotent resume that double-acts.
6. A **secret/credential** leak (env, logs, artifacts, the hub) or a push to a
   forbidden remote.

## Audit domains (the fan-out)
Each domain is one investigator pass producing a section of the handoff. They
are independent; run in parallel. `[C]` marks the highest trust-criticality.

### D1 -- Loop & phases `[C]`
`loop.py`, `run.py`. The orchestration heart: phase dispatch (clarify/design/
loop), the developer<->rw1<->rw2 bounce, senior escalation, deadlock handling,
enqueue of ready specs, `MAX_LOOPS`/`cap_reached`, how it feeds `policy`/
`merge_decision`, resume/idempotency across the append-only log. Look for:
authority taken from agent output without validation; a stored status that can
drift; a resume that re-acts; a bounce that can be forced to converge; declared
risk / verdict / flags flowing in from untrusted content.

### D2 -- Gate authority `[C]`
`testgate.py`. The exit-code authority that says "tests pass". This is the
forge-the-result surface: can a branch (conftest, plugin, env, coverage config,
a test that mutates the gate, a `sys.exit(0)`) make a red suite report green,
or the coverage/diff-cover/semgrep/gitleaks steps pass vacuously? Docker vs
local gate parity. Does any step fail **open** on tool error / timeout?

### D3 -- Agent I/O & untrusted output `[C]`
`agents.py`, `agent_retry.py`, `clarify.py`, `handoff.py`, `human_text.py`,
`verdict.py` (only its *callers* here, not the C2+C3-fixed extractor). Headless
claude/codex invocation, session-id/is_error parsing, retry/timeout, prompt
assembly (injection surface), rendering of untrusted text into
digests/prompts/terminals. Look for: untrusted output parsed as authority;
prompt-injection via branch content echoed into a role prompt; an error/refusal/
truncation mis-read as success.

### D4 -- Artifacts, state & derive-don't-store `[C]`
`artifacts.py`, `fingerprint.py`, `spec.py`, `config.py`, `fsutil.py`. The
append-only `iteration-log.jsonl`, artifact read/write, spec frontmatter parse
(H2/H3 showed specs are a trust surface -- re-scan the *parser* itself: duplicate
keys, type coercion, BOM/CRLF, injection), env/role-binding config parse,
filesystem helpers (path traversal, symlink, TOCTOU). Look for: stored status
that can drift; a parser that fails open; a secret read into an artifact or log.

### D5 -- Queue & quota `[C-]`
`queue.py`, `quota.py`, `terminals.py`. Ready-task discovery, locking, the
quota-window wait/backoff, terminal-state handling. Look for: a lock that does
not hold under concurrency; a quota parse that trusts attacker-influenced CLI
output to set a wait; a terminal state that mis-fires a merge/push; a bare
clock/sleep.

### D6 -- Oracle `[C-]`
`orchestrator/oracle/*`. Post-merge, non-blocking escape measurement. It never
blocks a merge (lower stakes) but it writes a data series and runs evals. Look
for: the system-under-measurement writing the measuring instrument (the
`ORACLE_ESCAPE` class -- C2+C3 fixed the flags boundary; check the oracle side);
an eval that executes untrusted content; a trigger that can be forced.

### D7 -- Shell layer `[C]`
`scripts/*.sh`, `scripts/lib/*.sh`. **Entirely outside the pytest gate** -- so
findings here are invisible to the test suite and matter disproportionately.
`kickoff.sh`, `merge-verified.sh`, `push-hub.sh`, `vps-onboard.sh`,
`local-onboard.sh`, `local-task.sh`, `upgrade_laddy.sh`, `watch-vps.sh`,
`why-crashed.sh`. Look for: unquoted expansions / word-splitting on
attacker-influenced values (task ids, branch names, paths); a push to a
forbidden remote; `set -e`/`pipefail` gaps that swallow a failed guard;
env-file sourcing that executes branch content; the merge-verified task-id
confirmation wiring (does the shell honor what `local_merge` now enforces?);
secret handling.

### D8 -- Roles & prompts `[C-]`
`roles/*.md`, `prompts/*.md`. The personas that gate. Look for: a role prompt
that instructs the agent to trust branch content as authority; a reviewer prompt
whose output contract the extractor cannot rely on (delimiting); a gap where the
persona could be steered by injected content. (Prose review; findings are
"the contract is weak here", cross-referenced to the code that depends on it.)

## Method (how this run executes)
The Director drives this unattended via subagents (the loop's own kickoff is not
used for the audit). One investigator subagent per domain D1..D8, in parallel,
each returning a structured findings list. Then an adjudication pass:
- De-duplicate and cross-check findings against each other and the code.
- Apply the overclaim discipline: demote or drop anything that is actually
  conservative/fail-closed/redundant.
- Rank HIGH / MEDIUM / LOW by the threat model, each with file:line, a repro
  sketch, a confidence tag, and a suggested fix direction (NOT applied).
- Emit `audit-engine-rest-handoff.md` at the repo root, in the same shape as
  `audit-c2c3-handoff.md` (findings + confidence tags + a "Rejected -- do NOT
  chase" section for anything considered and refuted).
Then an independent cross-vendor review pass (rw2 lens) sanity-checks the
handoff for overclaims and misses before it is considered done.

## Acceptance criteria (Definition of Done for the AUDIT)
1. `audit-engine-rest-handoff.md` exists at the repo root, covering all eight
   domains D1..D8, each with its findings (or an explicit "no findings, here is
   what I checked and why it is sound").
2. Every finding has: file:line anchor(s), a confidence tag (`[VERIFIED]` /
   `[CONFIRMED]`), a threat-model category (1..6), a repro sketch, and a
   suggested fix direction. HIGH/MEDIUM/LOW ranked.
3. A "Rejected -- do NOT chase" section lists what was considered and refuted,
   so the fix run does not re-file overclaims.
4. An independent (adversarial, ideally cross-vendor) pass has sanity-checked
   the handoff and its objections are resolved or recorded.
5. **No engine source changed, no test added, no behavior altered** by this run
   -- the diff is the handoff doc (and this spec) only. Report-only path holds.

## Downstream (not part of this run)
The handoff feeds a separate fix spec (`engine-rest-fix`), executed like
`fable-findings-fix`: staged, each finding shipping its own test, full gate
green, adversarial review, then an independent clean review and a second fix
round. That spec is authored after this audit lands and the Director has read it.
