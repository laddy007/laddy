# TODO

- Symlink-safe artifact writes now EXIST: `artifacts.write_text_nofollow` /
  `write_bytes_nofollow` (O_NOFOLLOW) + `TaskArtifacts._ensure`'s lstat walk that
  rejects a symlinked task-dir component (raises `ArtifactPathError`). All
  `TaskArtifacts.write_text/write_json/copy_spec` route through them, so every
  task-dir artifact (advisory record, merge-hold, reports, verdicts, spec copy)
  fails closed on a branch-planted symlink. USE THESE wherever a
  branch-controlled path is written on the trusted machine instead of a bare
  `Path.write_text`/`write_bytes`. Still bare (tighten when touched): the LOG
  append (`append_jsonl` -> `path.open("a")`, dir is guarded but the file is
  not O_NOFOLLOW); direct `spec_path.write_text` in clarify.py:60 / run.py:197 /
  loop.py:1076 / oracle. Folds into the `report-path-guard-md` spec's theme.
- Dead role fixtures in tests/test_run_cli.py:39 and tests/test_oracle_evalrun.py:117 - pass via real ENGINE_DIR/roles instead.
- basedpyright warnings (~1500, non-blocking by design - failOnWarnings=false) - burn down opportunistically.
- Report-only `path_guard` (orchestrator/policy.py REPORT_ALLOWED_PREFIXES) allows ANY file under `<agent-dir>/specs/`, not only `*.md` - a report-only task could commit `specs/x.py`. Not collected by pytest (testpaths=["tests"]) so low risk, but tighten the guard to spec markdown once C3's draft-status check has settled.
- Add a way (probably via an argument) to generate a plan and hand it back for review - same flow as spec authoring.
- migrate from docker to podman or rootless docker
- máme roli rw?
- Onboarding should INSTALL the agent CLIs, not just warn (vps-onboard.sh:212-213 only warn when claude/codex are missing). Install the binaries system-wide in the root phase (the binary is not the secret); the login/token stays a manual per-user step (interactive auth, must live in /home/<user>/.claude, never root's). Make the installed tool set CONFIG-DRIVEN, not hardcoded claude/codex - a list (in vps.conf or a manifest) of {tool -> install command} so tools can be swapped (e.g. replace codex) or added later without editing the script.
  - Verified install methods (reviewed on the live VPS 2026-07): Claude Code via Anthropic's official signed APT repo - key `https://downloads.claude.ai/keys/claude-code.asc` into /etc/apt/keyrings, `signed-by=` repo `https://downloads.claude.ai/claude-code/apt/stable stable main`, pin fingerprint `31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE`, then `apt install claude-code`. Codex via `npm i -g @openai/codex`. Install-ONLY (drop the one-off per-user removal/migration steps from the manual script). Coordinate with the rootless-docker rewrite below - both touch the root phase, so do them in one pass.
- VPS loop has NO worktree setup step: DEFAULT_FAST_COMMANDS activates `.venv` (`. .venv/bin/activate && ... pytest`) but nothing on the VPS creates it (env.vps has no SETUP_COMMANDS; loop/gitops/kickoff never run venv/pip). The local merge node HAS SETUP_COMMANDS (env.local) - the VPS side is missing the equivalent. Either add a SETUP_COMMANDS step to the loop (run once per fresh worktree before fast_tests) or document that TEST_COMMANDS on the VPS must bootstrap its own env. Also needs `python3-venv` on the box.
- Config ergonomics: let LADDY_USERS take just a PROJECT name and derive user/ssh-alias(`vps-<project>`)/engine-path(`/home/<project>/laddy`) by convention (they are 1:1 today), keeping explicit 4-field overrides optional. Change lives in lib/laddy_users.sh.
- Role -> vendor binding is HALF hardcoded: the CLI *commands* are config (env.vps CLAUDE_CMD/CODEX_CMD/SENIOR_CMD/REVIEW_*/RW2_CMD), but WHICH vendor runs each role is fixed in run.py (developer/rw1/clarify/senior/rw2 -> ClaudeRunner). Make the role->runner mapping config-driven so any role's vendor is swappable without editing run.py, and wire it into onboarding/env.vps. Overlaps with the config-driven toolset item above - do together.
  - CURRENT STATE: rw2 now runs **Claude (Sonnet)** by default (DEFAULT_RW2_CMD, override with RW2_CMD), NOT Codex - this deployment has no codex login, so rw1 and rw2 are both Claude and the cross-vendor guarantee is dropped. The LOCAL merge-panel rw2 (config.review_codex_cmd, run by merge-verified.sh) still defaults to codex - revisit when codex is back or make it config-driven too.
- kickoff reuses a stale task worktree: a failed `--new` (authoring added nothing) leaves wt/<task> with a headline-only spec, and a later `kickoff <task>` (no --new) REUSES it (run.py:276 only creates wt if absent) - so clarify sees the stub, not the spec pushed to the hub. kickoff should detect an empty/stub spec in a reused worktree (or a spec on the hub newer than the worktree base) and refresh, or at least warn. Workaround: `rm -rf <AGENT_WORK_ROOT>/wt/<task>` before re-running.
- kickoff/fullrun interactive gates die on a dropped/closed terminal - auto-wrap in tmux. `scripts/kickoff.sh` runs the `clarify` and `design` (approve) gates in the FOREGROUND - only the loop is `nohup`'d - so an SSH drop (`client_loop: send disconnect: Broken pipe`) kills them before the loop ever detaches, and the task silently never starts. Seen live on `fullrun-s0`: policy classified it high-risk despite `risk: medium` frontmatter (it edits `orchestrator/run.py`), the design gate engaged, ran the explorer (~4 min), then blocked on "Approve this approach?" while SSH was already gone -> design exited on EOF -> kickoff bailed "not detaching" -> no loop, no log, no lock. Fix: a shared `scripts/lib/tmux_wrap.sh` self-wrap that both `kickoff.sh` (VPS) and the future `fullrun.sh` (local driver, fullrun slice S3) source at the very top: `exec tmux new-session -A -s <name> -- "$0" "$@"` (attach-or-create) only when interactive (`$TMUX` unset, `[ -t 1 ]`, tmux present), with a `*_NO_TMUX` escape hatch for CI/headless. The `[ -t 1 ]` guard self-corrects at the boundary: when `fullrun` drives `kickoff` over SSH (no TTY) the kickoff wrap is a no-op, so the two never nest. Wire the `fullrun` half as an acceptance criterion in the fullrun S3 (driver) slice so it is built in, not bolted on afterwards.

odlogovat z roota

přidat nějaké pořadí tasků (čísla v názvu?)
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
- `requirements-dev.txt` pins nothing (pytest/ruff/basedpyright/mcp all floating)
  and the gate image installs it on the TRUSTED machine. Pin them.
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
  Untruthful config - fix or document.


proč merge-verified.sh nevypisuje co se děje?
merger-verified funguje asi jen na vps branche ... ale co když něco neprojde, já to upravím ručně a budu to chtít merger-verified (jen pokud to nedoáže sám před vrácení devu)


kod pro ntfy vytáhout mimo git a odrotovat
