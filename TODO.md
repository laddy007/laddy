# TODO

- Symlink-safe artifact writes now EXIST: `artifacts.write_text_nofollow` /
  `write_bytes_nofollow` (O_NOFOLLOW) + `TaskArtifacts._ensure`'s lstat walk that
  rejects a symlinked task-dir component (raises `ArtifactPathError`). All
  `TaskArtifacts.write_text/write_json/copy_spec` route through them, so every
  task-dir artifact (advisory record, merge-hold, reports, verdicts, spec copy)
  fails closed on a branch-planted symlink. USE THESE wherever a
  branch-controlled path is written on the trusted machine instead of a bare
  `Path.write_text`/`write_bytes`. DONE 2026-07-19: append_jsonl now opens O_NOFOLLOW. Still bare (tighten
  when touched): direct `spec_path.write_text` in clarify.py, run.py
  (_phase_new seed + _refresh_stub_spec), loop.py, oracle. Folds into the `report-path-guard-md` spec's theme.
- Dead role fixtures in tests/test_run_cli.py:39 and tests/test_oracle_evalrun.py:117 - pass via real ENGINE_DIR/roles instead.
- basedpyright warnings (~1500, non-blocking by design - failOnWarnings=false) - burn down opportunistically.
- Report-only `path_guard` (orchestrator/policy.py REPORT_ALLOWED_PREFIXES) allows ANY file under `<agent-dir>/specs/`, not only `*.md` - a report-only task could commit `specs/x.py`. Not collected by pytest (testpaths=["tests"]) so low risk, but tighten the guard to spec markdown once C3's draft-status check has settled.
- Add a way (probably via an argument) to generate a plan and hand it back for review - same flow as spec authoring.
- migrate from docker to podman or rootless docker
- máme roli rw?
- Onboarding should INSTALL the agent CLIs, not just warn (vps-onboard.sh:212-213 only warn when claude/codex are missing). Install the binaries system-wide in the root phase (the binary is not the secret); the login/token stays a manual per-user step (interactive auth, must live in /home/<user>/.claude, never root's). Make the installed tool set CONFIG-DRIVEN, not hardcoded claude/codex - a list (in vps.conf or a manifest) of {tool -> install command} so tools can be swapped (e.g. replace codex) or added later without editing the script.
  - Verified install methods (reviewed on the live VPS 2026-07): Claude Code via Anthropic's official signed APT repo - key `https://downloads.claude.ai/keys/claude-code.asc` into /etc/apt/keyrings, `signed-by=` repo `https://downloads.claude.ai/claude-code/apt/stable stable main`, pin fingerprint `31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE`, then `apt install claude-code`. Codex via `npm i -g @openai/codex`. Install-ONLY (drop the one-off per-user removal/migration steps from the manual script). Coordinate with the rootless-docker rewrite below - both touch the root phase, so do them in one pass.
- VPS loop has NO worktree setup step: DEFAULT_FAST_COMMANDS activates `.venv` (`. .venv/bin/activate && ... pytest`) but nothing on the VPS creates it (env.vps has no SETUP_COMMANDS; loop/gitops/kickoff never run venv/pip). NOTE (2026-07-19): `SETUP_COMMANDS` is read by NOTHING anywhere - it was a dead knob in env.local.example (now removed); there is no setup step on either node. Either add a real SETUP_COMMANDS step to the loop (run once per fresh worktree before fast_tests) or document that TEST_COMMANDS must bootstrap its own env. Also needs `python3-venv` on the box.
- Config ergonomics: let LADDY_USERS take just a PROJECT name and derive user/ssh-alias(`vps-<project>`)/engine-path(`/home/<project>/laddy`) by convention (they are 1:1 today), keeping explicit 4-field overrides optional. Change lives in lib/laddy_users.sh.
- ~~Role -> vendor binding is HALF hardcoded~~ MOSTLY DONE (fullrun-s0, on
  this branch): ROLE_<NAME>_{VENDOR,MODEL,THINKING} env knobs + _resolve_runner
  make the role->runner mapping config-driven (documented in
  env.local.example AND env.vps.example). REMAINING: only the config-driven
  CLI-install toolset item above (vps-onboard).
  - CURRENT STATE: rw2 now runs **Claude (Sonnet)** by default (DEFAULT_RW2_CMD, override with RW2_CMD), NOT Codex - this deployment has no codex login, so rw1 and rw2 are both Claude and the cross-vendor guarantee is dropped. The LOCAL merge-panel rw2 (config.review_codex_cmd, run by merge-verified.sh) still defaults to codex - revisit when codex is back or make it config-driven too.
- ~~kickoff reuses a stale task worktree~~ DONE 2026-07-19 (_refresh_stub_spec:
  clarify pulls the hub spec over an exact --new stub and commits). Original: a failed `--new` (authoring added nothing) leaves wt/<task> with a headline-only spec, and a later `kickoff <task>` (no --new) REUSES it (run.py:276 only creates wt if absent) - so clarify sees the stub, not the spec pushed to the hub. kickoff should detect an empty/stub spec in a reused worktree (or a spec on the hub newer than the worktree base) and refresh, or at least warn. Workaround: `rm -rf <AGENT_WORK_ROOT>/wt/<task>` before re-running.
- ~~kickoff gates die on a dropped terminal~~ DONE 2026-07-19: scripts/lib/
  tmux_wrap.sh + kickoff self-wrap (LADDY_NO_TMUX escape hatch); wire the
  fullrun half when fullrun S3 lands. ALSO: LADDY_ASK_REMOTE=1 makes the gate
  block on a file, not a TTY. Original: `scripts/kickoff.sh` runs the `clarify` and `design` (approve) gates in the FOREGROUND - only the loop is `nohup`'d - so an SSH drop (`client_loop: send disconnect: Broken pipe`) kills them before the loop ever detaches, and the task silently never starts. Seen live on `fullrun-s0`: policy classified it high-risk despite `risk: medium` frontmatter (it edits `orchestrator/run.py`), the design gate engaged, ran the explorer (~4 min), then blocked on "Approve this approach?" while SSH was already gone -> design exited on EOF -> kickoff bailed "not detaching" -> no loop, no log, no lock. Fix: a shared `scripts/lib/tmux_wrap.sh` self-wrap that both `kickoff.sh` (VPS) and the future `fullrun.sh` (local driver, fullrun slice S3) source at the very top: `exec tmux new-session -A -s <name> -- "$0" "$@"` (attach-or-create) only when interactive (`$TMUX` unset, `[ -t 1 ]`, tmux present), with a `*_NO_TMUX` escape hatch for CI/headless. The `[ -t 1 ]` guard self-corrects at the boundary: when `fullrun` drives `kickoff` over SSH (no TTY) the kickoff wrap is a no-op, so the two never nest. Wire the `fullrun` half as an acceptance criterion in the fullrun S3 (driver) slice so it is built in, not bolted on afterwards.

odlogovat z roota

~~přidat nějaké pořadí tasků~~ DONE 2026-07-19: `--phase enqueue t1 t2 ... --chain` (ordered, worktree of each link starts from the predecessor branch; failure stops the chain) + `kickoff --code-ready` (review-only run on finished code).
až dojedu tab local claude - zjistit jak je to s mcp (je nová spec?)
dva specy merge-hold.md
!     0local creating spec openhabds

## From the 2026-07-17 session (mcp + fullrun-s2 hand-merged, bypassing the gate)

- **mcp secret: RESOLVED in code (fix/mcp-secret-out-of-source).** The TOTP secret is
  gone from `note_server/` source and read from `NOTE_SERVER_TOTP_SECRET` (base32, no
  default); the `# gitleaks:allow` line is dropped with it, and `.laddy/specs/mcp.md`
  no longer instructs hardcoding. The secret was ROTATED, so the old value is dead
  everywhere it still literally appears (branch history commit 44670ac1,
  `.laddy/tasks/mcp/*` run artifacts). Before `git push origin` (Tier 3,
  `https://github.com/laddy007/laddy.git`), the Director decides whether a dead but
  real-looking secret string in public history is acceptable or whether to scrub
  branch history first. Merge into LOCAL main is safe.
- **mcp's two real defects need a NEW task once mcp merges** (not a `--resume`): a
  6-digit network factor with no throttling / replay protection (brute-forceable if
  the server is ever exposed), and saved notes defaulting to group/other-readable.
  Its spec asked for neither, so no reviewer would ever have caught them - see the
  `decide()` item below.
- ~~`requirements-dev.txt` pins nothing~~ DONE 2026-07-19: pinned.
- **fullrun-s2 is the safe hand-merge, but it now owns the gate.** Its red suite was
  only the branch-vs-restore mismatch and goes green once the ruleset IS main's. But
  both new rules are trivially bypassable (Rule A by precomputed flags, Rule B by any
  unrelated or late `st_nlink`) and they are now the TRUSTED rules every future task
  is measured by. Tighten them in a follow-up; verify main's suite is green first.
- **`decide()` cannot tell a defect from a declared risk.** Every security-panel
  blocker becomes BROKEN ("needs a fix, not a risk call"), but a finding against a
  risk the SPEC declared is a risk call - `mcp`'s hardcoded secret is exactly that,
  and no developer following that spec could fix it. Route declared-risk findings to
  RISK_DECISION; keep out-of-spec findings BROKEN.
- **Decide: infra-override - BROKEN or RISK_DECISION?** A branch changing
  `.laddy/docker` / `.laddy/security` currently holds BROKEN (chosen fail-closed).
  Consider routing it to RISK_DECISION so the Director can accept it after reading
  the diff - `fullrun-s2` had to be hand-merged precisely because it cannot.
- **`request_payload` (verdict.py) burns retries on failures no retry can fix.** An
  expired CLI login abstained the whole panel three times over. Classify
  non-retryable failures - deliberate follow-up to `agent-error-visibility`.
- `scripts/local-task.sh` defaults pre-date the engine/target split:
  `LOCAL_SOURCE_REPO=/mnt/c/myapp`, `LOCAL_BASE_BRANCH=fix/agent-loop-hardening`.
  Untruthful config - fix or document. (2026-07-19: PYTHON_BIN is now honored
  like the sibling launchers; the four LOCAL_* defaults still point at myapp -
  read them from local.conf/env.local instead.)


~~proč merge-verified.sh nevypisuje co se děje?~~ DONE 2026-07-19: [gate]
progress lines in gather_gates.
~~merger-verified funguje asi jen na vps branche ... co když to upravím ručně~~
DONE/ANSWERED 2026-07-19: presne tohle je `merge-verified.sh <task> --local
<sha|branch|worktree>` - soudí lokálně commitnutou revizi stejnou gate, bez
VPS. Nově zdokumentováno v USAGE.md §6.


kod pro ntfy vytáhout mimo git a odrotovat (2026-07-19 check: the engine
tree commits NO topic anywhere - the topic lives only in git-ignored env.vps;
if a topic leaked, it is in target-repo artifacts/history, not here)

- phone follow-ups: ntfy ACTION buttons (remote_ask sends a plain message;
  an ntfy Actions payload could answer approve/reject one-tap via the PWA's
  /api/answer?token=... URL), and `tailscale serve` HTTPS note in docs.

## Coherence findings from the 2026-07-19 whole-project review

- ~~note_server missing from ENGINE_SENSITIVE_GLOBS~~ GLOB DONE 2026-07-19
  (fix/merge-flow-queue 3f1ed0e: note_server/* rides L3 + test). STILL OPEN:
  decide whether note_server (product code in an engine that claims to hold
  none) moves out to its own target repo.
- Root `docker/`, `security/`, `specs/` are stale pre-split templates: root
  docker/Dockerfile.test is still the myapp gate (pnpm, py3.12) and has
  DIVERGED from the live `.laddy/docker/`; root security/ is a byte-identical
  copy of `.laddy/security/`. setup.md now points new targets at the `.laddy/`
  copies; delete the root dirs or refresh them consciously.
- ~~local-onboard/merge-verified env.local contract mismatch~~ DONE
  2026-07-19: merge-verified.sh prefers the TARGET's env.local, engine's is
  the fallback.
- monitoring/README.md installs from `/root/myapp-trusted/.laddy/monitoring/`
  as root - pre-split paths and an unreconciled monitor-as-root story; rewrite
  against the engine `monitoring/` + unprivileged-user topology.
- README/CLAUDE.md no longer claim rw2 = Codex (fixed 2026-07-19: "cross-vendor
  when Codex is configured"); the deeper fix stays the role->vendor binding
  item above.

## Proposals from the 2026-07-19 GitHub survey (Director to pick)

Top picks, ranked (full report in the session log; S/M/L = rough effort):

1. Evidence bundle + trajectory replay per task branch (M) - ship per-role
   session logs, test output, coverage delta, verdict JSONs with the branch;
   merge gate replays WHY per commit. Trust through legibility (Copilot
   session logs / Cursor "demos over diffs" / SWE-agent trajectories).
2. Best-of-N attempts with panel-ranked selection (M) - N workers on one spec,
   `<task>/attempt-N` branches, existing cross-vendor panel ranks them;
   side-by-side at the gate. Nobody ships auto-ranking yet.
3. Spec deltas + living-spec archive (M, OpenSpec) - specs carry
   ADDED/MODIFIED/REMOVED requirement deltas; the merge gate folds them into a
   living specs/ dir. Fixes one-shot-spec drift.
4. Egress allowlist proxy + blocked-attempt ledger (M) - two-phase network on
   the VPS (setup: registries; agent: model APIs only); blocked attempts become
   merge-report line items (exfiltration signal). 2026 Claude-Action CVEs are
   the motivation.
5. Per-role cost ledger + hard budget caps (S) - parse the agent CLIs' own
   JSONL session logs (ccusage approach), price via litellm tables, aggregate
   per task x round x role, show dollars + wall-time at the gate.
6. Git-native per-repo memory (S) - gate-side LESSONS.md appended after each
   merge from reviewer corrections; worker proposes, only the trusted gate
   persists. No vector stores.
7. Repro-test-first protocol for bug specs (S, Agentless) - bugfix spec type
   requires a failing test commit before the fix; gates check the red->green
   flip.
8. Fleet mode: parallel tasks + one status surface (L) - N concurrent workers,
   kanban-shaped status over stream-json events, claude-squad-style tmux
   attach + pause/resume. Scheduler + viewer, NOT an orchestrator agent (Kilo
   Code deprecated theirs).
9. Snapshot-keyed environment cache (M) - cache worker state keyed on
   hash(setup script + lockfiles); cold-start latency is what caps parallelism
   and best-of-N.
10. Mid-run steering + named autonomy ladder (S) - `laddy msg <task> "..."`
    appends to the next round's prompt; named levels (guarded/standard/full)
    instead of flag soup; PLAN.md round-0 artifact whose divergence from the
    final diff is a free reviewer signal.

Spec-authoring optimizations (overlaps: create-spec skill + clarify gate):

- `[NEEDS CLARIFICATION]` markers in specs (Spec Kit): create-spec writes them
  instead of guessing; kickoff/enqueue refuse while any marker remains -
  queued 3am runs never hit an unanswerable clarify.
- Optional EARS acceptance-criteria stanza ("WHEN X THE SYSTEM SHALL Y",
  Kiro): most reviewable AC format; reviewers cite which requirement each hunk
  satisfies.
- Bugfix spec template (repro + root-cause hypothesis + required regression
  test) as a distinct type next to feature/bug/spike/audit/investigate.
- Local clarify pre-pass in create-spec: the skill already reads the target
  code - have it ask the clarify-gate-style blocking questions at authoring
  time and bake answers into the spec (pairs with `[NEEDS CLARIFICATION]`).
- Plan-handback (existing TODO item above) fits Jules' editable-PLAN.md
  pattern: plan approval as an explicit state before code.

NOT adopting (surveyed, rejected): persona swarms (claude-flow/BMAD),
dedicated orchestrator-agent layer, external memory services (mem0/Letta),
normalized cost units (Devin ACU retreat).

přidat možnost řetězení tasků

