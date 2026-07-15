---
type: feature
roles: [developer, rw1, rw2]
risk: high
status: draft-proposal
---
# fullrun — automate the VPS↔local round-trip with a trusted rw3 in the loop

> **Umbrella design spec — not directly runnable.** Marked `draft-proposal` so
> `kickoff fullrun` refuses it: this is 5 slices, run them one at a time as
> their own specs (`fullrun-s0`, …). See the Slices section.

## Goal
Turn the Director's manual ping-pong (push-hub → kickoff on VPS → watch →
merge-verified on local → decide → re-kickoff) into one local driver,
**without touching the trust model**: the local machine still re-derives every
gate on trusted infra and remains the sole merge authority; the VPS still never
writes main. The automation is plumbing plus one structural change — model the
local trusted review as **rw3**, a first-class reviewer in the existing verdict/
feedback chain — so a hold flows back to the developer the same way an rw1/rw2
`changes_requested` does, instead of dead-ending in a `merge-hold.md` a human
must translate by hand.

## Motivation (why now)
Two of the first two dogfood tasks passed the VPS loop but were HELD at local
merge-verified on security findings (symlink TOCTOU, hard-link overwrite). Two
compounding gaps caused that: (1) the VPS loop runs no security-specific review
at all (the `security` role + `run_security_panel` live only in
`local_merge.py`), and (2) the VPS rw2 is the same vendor as rw1 (no codex on
the VPS → cross-vendor guarantee dropped). Today closing that means a human
reads the hold, re-writes the ask, and re-kicks the VPS by hand. fullrun + rw3
automate that mechanical loop and route the trusted, cross-vendor findings back
as normal developer feedback.

## Scope
In:
- a new local driver `scripts/fullrun.sh` + its Python (`orchestrator/fullrun.py`);
- **rw3** as a new verdict type + loop wiring (`orchestrator/verdict.py`,
  `orchestrator/loop.py`, `orchestrator/run.py`): mirror rw2's `RW2_VERDICT`/
  `validate_rw2`/state transitions with an `RW3_VERDICT`/`validate_rw3`, feeding
  `changes_requested`/`nogo` back to `developer` and `go` to the merge step;
- reuse of `orchestrator/local_merge.py` (its deterministic gate re-run + codex
  review + merge) as the *implementation* behind rw3 — rw3 is its LLM-review
  face wired into the loop, not a second reviewer engine;
- **deterministic FS-safety gate on the VPS too**: custom semgrep rules
  (symlink/TOCTOU/`O_NOFOLLOW`-missing/hard-link/untrusted-path-write) added to
  the authoritative gate infra so both the VPS in-loop gate and the local
  re-derivation catch the known-dangerous classes deterministically, no LLM;
- a **human-handoff bundle** (see below) and ntfy delivery;
- **config-driven role→runner binding** in **`env.vps` / `env.local`**
  (`orchestrator/run.py`, `agents.py`): replace the hardcoded `make_*_runner`
  lambdas with a per-role `{vendor, model, thinking}` resolved from the same
  shell env the other knobs already use, so rw3 can be codex-on-local without
  editing run.py, and any role's vendor/model/reasoning is swappable per
  deployment. Today the CLI *commands* are env config (`CLAUDE_CMD`/`RW2_CMD`/…)
  but the *vendor* per role is fixed in run.py and reasoning/thinking is not
  wired at all — add `thinking` as an explicit knob, mapped to the CLI flag
  where the vendor exposes one;
- **local spec authoring without vps.conf**: authoring (`--phase new`) runs on
  the local trusted clone via `env.local` (the path `scripts/local-task.sh`
  already takes) — vps.conf is required only for the VPS *dispatch* phase.
  (local-task.sh's stale `myapp`/`fix/agent-loop-hardening` defaults get fixed
  as part of this.);
- tests under `tests/` for every new unit.
Out: any change that lets the VPS write main or that trusts a VPS-produced
verdict as authoritative; auto-approving any Tier-3 Director gate; new external
dependencies beyond semgrep (already in the gate).

## Behaviour

### 1. Scope argument
`fullrun [TARGET]` where `TARGET` is one of:
- a **task id** — run just that task;
- a **project** name (from `vps.conf` `LADDY_USERS`) — run all that project's
  ready tasks;
- `all` (**default**) — every project's every ready task.
Resolution reuses `lib/laddy_users.sh` (projects) and the queue/`discover_ready`
(ready tasks). Per-task work is independent; one task's hold never blocks others.
Batch (`project`/`all`) drives **only already-authored, ready tasks** — a task
still needing the interactive `new`/`clarify`/`design` gates is skipped (those
stay a deliberate, separate human step); it is not silently auto-authored.

### 2. Per-task cycle (the driver)
For each in-scope task, loop until merged, given-up, or handed to a human:
1. `push-hub` the target base so the VPS branches from current main.
2. ssh VPS → `kickoff` (detached loop) — unless the task still needs the
   interactive `new`/`clarify`/`design` gates, which stay human (see §4).
3. Poll the VPS terminal state (parse the iteration-log / `merge-decision.json`),
   not a fixed sleep.
4. On `stop_before_merge`, run the **local trusted verification**: the
   deterministic gate re-run (incl. the new semgrep FS-safety rules) runs on
   **every** task; the **rw3 cross-vendor codex review runs only for sensitive
   (L2/L3) tasks**. A non-sensitive (L1) task with a green deterministic gate
   needs no LLM review and merges directly (keeps the common path fast). rw3,
   when it runs, produces an `RW3_VERDICT`.
5. Route the outcome through the **existing feedback chain**:
   - L1 green (or rw3 `go`) + gates green + not sensitive → merge into local
     main; done.
   - rw3 `changes_requested`/`nogo` → feed the findings back (see Feedback
     transport) and re-dispatch the VPS loop (GOTO 2). This is the normal
     `changes_requested → developer` transition, just sourced from a trusted,
     cross-machine reviewer.
   - sensitive (L3) / risk-decision → **hand to human** (§4), never auto-merge.
6. **Convergence guard**: reuse `loop.py`'s `_repeats`/`_fingerprints`. The same
   rw3 finding twice in a row (or an iteration cap) → stop the bounce and hand
   to human with the bundle. Never loop forever.

### 2a. Feedback transport (rw3 → developer)
rw3 emits a standard `Verdict` (`changes_requested` + blocker findings) in the
**same schema** rw1/rw2 use, commits it to the task's append-only artifact log
(`.laddy/tasks/<task>/`), and pushes it to the hub. The next VPS kickoff/resume
fetches the branch, reads the rw3 verdict as the latest review outcome, and
transitions to `developer` with those findings — the identical
`changes_requested → developer` path rw2 already uses. No new message format is
introduced (honours the uniform-communication requirement); the only new thing
is that this verdict was produced on the trusted local machine and arrived via
git. This also makes every rw3 round an auditable committed artifact and lets
the convergence guard fingerprint rw3 findings for free.

### 3. Deterministic FS-safety rules (both ends)
Author semgrep rules for at least: `os.open`/`open` on a caller-supplied path
without `O_NOFOLLOW`; `realpath(...)` used to authorize a path that is then
re-opened by string (parent TOCTOU); overwrite/`ftruncate` without an
`st_nlink`/hard-link check; writing to a path derived from loop/agent input
outside a validated root. Wire them into the authoritative gate infra so the
**VPS in-loop gate** fails fast on these, and the local re-derivation re-checks
them (trust anchor). Both dogfood failures must be caught by these rules.

### 4. Human handoff (Tier-3 gates + give-up)
fullrun pauses and never decides these itself: L3 **design approval**, L3
**merge risk-confirm**, the **GitHub-push `y/N`**, and a **convergence give-up**.
At each, it must let the Director *see what they are deciding on*:
- a **readable, colour-highlighted diff** of the change (reuse
  `scripts/colorize-log.sh` conventions / a diff colorizer);
- a **written comment from the local agent (rw3)**: what it found, the risk
  classification and why, what it would take to clear, and the exact decision
  being asked.
Delivered via **ntfy** (`NTFY_TOPIC`, already wired for terminal states) plus a
rendered bundle the Director can open; the driver blocks until they answer, then
resumes.

### 5. Notify on every rw3 return
Independently of the human-decision gates, **every time rw3 returns a verdict
that bounces the task back to the developer** (`changes_requested`/`nogo`), send
an ntfy with the same bundle (colour diff + rw3 comment). Scope is **bounces
only** (`changes_requested`/`nogo`) — not every verdict — so the Director sees
each real round-trip without noise. A final merge and a convergence give-up also
notify (they are decision/terminal events), but a plain rw3 `go` that merges an
L1 change does not.

## Acceptance criteria
1. `fullrun <task>` drives push → kickoff → poll → rw3 → merge/feedback for a
   single task end to end against fakes (no real VPS/LLM in tests).
2. `fullrun <project>` and `fullrun` (=`all`) resolve scope from `LADDY_USERS`
   + ready-task discovery and process each task independently; one hold does not
   block the batch.
3. rw3 is a real reviewer in the chain and runs **only on sensitive (L2/L3)**
   tasks: an rw3 `changes_requested`/`nogo` routes back to `developer`
   (re-dispatch); an L1 task merges on a green deterministic gate with no rw3
   LLM review; a sensitive task merges only on rw3 `go` + green gates + human
   confirm — verified by loop-state tests mirroring the existing rw2 transition
   tests.
4. The two failure classes from the dogfood tasks (parent-symlink TOCTOU;
   force-mode hard-link overwrite) are each caught by the new semgrep rules,
   asserted by a fixture that reintroduces the anti-pattern and fails the gate.
5. Every Tier-3 gate (L3 design, L3 risk-confirm, GitHub push) and the
   convergence give-up **pause for the human** with a bundle that contains both
   a colourised diff and the rw3 comment; none is ever auto-approved. Asserted
   by tests that a non-interactive run declines/holds rather than merges.
6. The convergence guard stops after a repeated rw3 finding / iteration cap and
   hands off, rather than bouncing — asserted with a repeating-finding fake.
7. Trust invariants intact: no path lets a VPS verdict authorize a merge; the
   hub-main-ancestor tripwire and "VPS never writes main" checks still hold.
   Suite (`ruff`, `basedpyright`, `pytest`) green.
8. **Uniform agent communication**: rw3 uses the identical `Verdict` schema +
   `AgentRunner` protocol + append-only artifact log as rw1/rw2; `validate_rw3`
   mirrors `validate_rw2` (same malformed/abstention rules). No new
   inter-agent message format is introduced. Asserted by reusing the rw2
   verdict/validation tests for rw3.
9. **Config-driven binding**: each reviewer/developer role resolves its
   `{vendor, model, thinking}` from config, not a hardcoded runner in run.py;
   changing rw3 from claude to codex (or a model/thinking level) is a config
   edit with no code change, asserted by a test that swaps the binding and sees
   the selected runner used.
10. **Local authoring without vps.conf**: `--phase new` authoring completes on
    the local trusted clone with only `env.local` present (no vps.conf),
    asserted by an authoring-path test that has no vps.conf in the environment.
11. **ntfy on every rw3 bounce**: each `changes_requested`/`nogo` rw3 verdict
    emits an ntfy carrying the diff+comment bundle, asserted with a fake ntfy
    sink.

## Suggested slices (do NOT try this as one loop run)
This is large and trust-critical; slice it:
- **S0** — config-driven role→{vendor, model, thinking} binding (replaces the
  hardcoded `make_*_runner`). Standalone; prerequisite for rw3=codex-on-local.
- **S1** — rw3 verdict + loop/run wiring over `local_merge` (feedback chain,
  no driver yet). Highest structural value; unblocks the rest.
- **S2** — semgrep FS-safety rules on both gate ends (deterministic, standalone).
- **S3** — `fullrun` driver: single-task cycle + convergence guard + ntfy on
  every rw3 bounce.
- **S4** — scope arg (project/`all`) + batch independence + local authoring
  path (env.local, no vps.conf; fix local-task.sh defaults).
- **S5** — human-handoff bundle (colour diff + rw3 comment) + ntfy pause/resume.

## Notes
- Keep the trust boundary explicit in code and comments: rw3, the deterministic
  re-run, and the merge all execute locally on trusted infra; the VPS in-loop
  copies are *fast feedback*, never authority.
- rw3 reuses the existing `roles/rw2.md` + `roles/security.md` prompts on
  trusted infra; it is a wiring/authority change, not a new review engine.
- `RW3_VERDICT`/`validate_rw3` should mirror `RW2_VERDICT`/`validate_rw2` so the
  schema, malformed-handling, and abstention-as-blocker rules stay uniform.
