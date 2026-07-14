# TODO

- Dead role fixtures in tests/test_run_cli.py:39 and tests/test_oracle_evalrun.py:117 - pass via real ENGINE_DIR/roles instead.
- basedpyright warnings (~1500, non-blocking by design - failOnWarnings=false) - burn down opportunistically.
- Report-only `path_guard` (orchestrator/policy.py REPORT_ALLOWED_PREFIXES) allows ANY file under `<agent-dir>/specs/`, not only `*.md` - a report-only task could commit `specs/x.py`. Not collected by pytest (testpaths=["tests"]) so low risk, but tighten the guard to spec markdown once C3's draft-status check has settled.
- Add a way (probably via an argument) to generate a plan and hand it back for review - same flow as spec authoring.
- migrate from docker to podman or rootless docker
- máme roli rw?
- Onboarding should INSTALL the agent CLIs, not just warn (vps-onboard.sh:212-213 only warn when claude/codex are missing). Install the binaries system-wide in the root phase (the binary is not the secret); the login/token stays a manual per-user step (interactive auth, must live in /home/<user>/.claude, never root's). Make the installed tool set CONFIG-DRIVEN, not hardcoded claude/codex - a list (in vps.conf or a manifest) of {tool -> install command} so tools can be swapped (e.g. replace codex) or added later without editing the script.
- Config ergonomics: let LADDY_USERS take just a PROJECT name and derive user/ssh-alias(`vps-<project>`)/engine-path(`/home/<project>/laddy`) by convention (they are 1:1 today), keeping explicit 4-field overrides optional. Change lives in lib/laddy_users.sh.