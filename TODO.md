# TODO

- Coverage target is now per-target (M1: `<target>/.laddy/policy.toml` coverage_package). REMAINING: laddy's own gate image lacks pytest-cov/diff-cover/semgrep/gitleaks, so repo_laddy task branches still fail the local gate (fail-closed) - add the scanner set to laddy's `.laddy/docker/` image before dogfooding laddy-as-target.
- Dead role fixtures in tests/test_run_cli.py:39 and tests/test_oracle_evalrun.py:117 - pass via real ENGINE_DIR/roles instead.
- basedpyright warnings (~1500, non-blocking by design - failOnWarnings=false) - burn down opportunistically.
- Report-only `path_guard` (orchestrator/policy.py REPORT_ALLOWED_PREFIXES) allows ANY file under `<agent-dir>/specs/`, not only `*.md` - a report-only task could commit `specs/x.py`. Not collected by pytest (testpaths=["tests"]) so low risk, but tighten the guard to spec markdown once C3's draft-status check has settled.
- Add a way (probably via an argument) to generate a plan and hand it back for review - same flow as spec authoring.
