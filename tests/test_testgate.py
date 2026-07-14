"""Tests for the fast inner test gate."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from orchestrator import TARGET_DIR_NAME
from orchestrator.testgate import TestResult, run_fast
from tests.fakes import FakeShell

# The gate shell runs `bash -lc` - unambiguous on the Linux VPS, but on Windows
# a bare `bash` resolves env-dependently (System32 WSL stub vs Git Bash) and the
# login shell can exit non-zero on its own. These tests exercise the REAL shell,
# so they belong to the POSIX runtime; everything else here uses fakes.
requires_bash = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="gate shell runs bash on the Linux VPS; host bash resolution is unreliable off POSIX",
)


def test_run_fast_pass(tmp_path: Path) -> None:
    shell = FakeShell(results=[(0, "all green")])
    result = run_fast("pytest -q", tmp_path, shell)
    assert result == TestResult(passed=True, output_tail="all green", command="pytest -q")
    assert shell.calls == [("pytest -q", tmp_path)]


def test_run_fast_fail(tmp_path: Path) -> None:
    shell = FakeShell(results=[(1, "FAILED test_x")])
    result = run_fast("pytest -q", tmp_path, shell)
    assert result.passed is False
    assert "FAILED test_x" in result.output_tail


def test_run_fast_truncates_output_to_last_400_lines(tmp_path: Path) -> None:
    long_output = "\n".join(f"line {i}" for i in range(1000))
    shell = FakeShell(results=[(1, long_output)])
    result = run_fast("pytest -q", tmp_path, shell)
    lines = result.output_tail.splitlines()
    assert len(lines) == 400
    assert lines[0] == "line 600"
    assert lines[-1] == "line 999"


def test_docker_gate_command_contains_sha_compose_and_clean_clone(tmp_path: Path) -> None:
    from orchestrator.testgate import BACKEND_GATE, DockerGate

    compose_rel = f"{TARGET_DIR_NAME}/docker/compose.test.yml"
    gate = DockerGate(frontend_gate="FE_GATE", compose_rel=compose_rel)
    cmd = gate.command("abc123", include_frontend=False)
    assert "git clone --no-local ." in cmd
    assert "checkout -q abc123" in cmd
    assert compose_rel in cmd
    assert BACKEND_GATE in cmd
    assert "pnpm" not in cmd
    # the compose file is referenced by its repo-RELATIVE path so, after
    # `cd $tmp/repo`, both -f and the `build.context: ../..` resolve inside
    # the SHA-pinned clone - not the live worktree (finding: gate tested
    # uncommitted worktree content and certified it against code_sha)
    assert 'cd "$tmp/repo"' in cmd
    # every docker compose invocation carries a unique -p project name so
    # concurrent gate runs never clobber each other's containers/volumes
    assert f"-p laddy-gate-abc123-{os.getpid()} -f {compose_rel} run" in cmd
    assert f"-p laddy-gate-abc123-{os.getpid()} -f {compose_rel} down -v" in cmd
    compose_flag = cmd.split("docker compose -p ", 1)[1].split(" -f ", 1)[1].split(" ", 1)[0]
    assert not compose_flag.startswith("/"), compose_flag
    assert ":" not in compose_flag  # no windows drive / absolute path
    assert compose_flag == compose_rel


def test_containerized_uses_unique_compose_project() -> None:
    # Two gate runs (VPS pre-filter + local binding gate, or two concurrent
    # tasks) must never clobber each other's containers/volumes - each run
    # gets its own COMPOSE_PROJECT_NAME derived from the sha + host pid.
    from orchestrator.testgate import _containerized

    cmd = _containerized(".laddy/docker/compose.test.yml", "a" * 40, "true")
    assert f"-p laddy-gate-{'a' * 12}-{os.getpid()}" in cmd


def test_docker_gate_frontend_appended_when_touched(tmp_path: Path) -> None:
    from orchestrator.testgate import DockerGate

    gate = DockerGate(frontend_gate="FE_BUILD_CMD", compose_rel="c.yml")
    cmd = gate.command("abc123", include_frontend=True)
    assert "FE_BUILD_CMD" in cmd


def test_docker_gate_run_returns_result(tmp_path: Path) -> None:
    from orchestrator.testgate import DockerGate

    shell = FakeShell(results=[(1, "FAILED test_z")])
    gate = DockerGate(frontend_gate="FE", compose_rel="c.yml", shell=shell)
    result = gate.run(tmp_path, "abc123", include_frontend=False)
    assert result.passed is False
    assert result.sha == "abc123"
    assert "FAILED test_z" in result.output_tail
    assert shell.calls[0][1] == tmp_path


@requires_bash
def test_gate_shell_surfaces_test_summary_under_stderr_flood() -> None:
    # Flow-audit finding: docker/compose flood stderr with build + teardown
    # progress; the pytest result is on stdout. A naive stdout+stderr concat
    # buries the summary (end of stdout) under stderr noise once `_tail` reads
    # the END, so the authoritative artifact / rework detail showed docker
    # noise instead of the actual gate result. The gate shell must keep the
    # test signal visible in the tail.
    from orchestrator.testgate import _subprocess_shell_gate, _tail

    cmd = "for i in $(seq 1 800); do echo compose-noise >&2; done; echo '7 passed in 0.10s'"
    rc, output = _subprocess_shell_gate(cmd, Path("."))
    assert rc == 0
    assert "7 passed in 0.10s" in _tail(output)


@requires_bash
def test_gate_shell_keeps_stderr_when_stdout_empty() -> None:
    # An infra failure (docker build broke) never produces a test summary; the
    # error is on stderr and must still survive into the tail.
    from orchestrator.testgate import _subprocess_shell_gate, _tail

    rc, output = _subprocess_shell_gate("echo 'build failed: no space left' >&2; exit 1", Path("."))
    assert rc == 1
    assert "build failed: no space left" in _tail(output)


def test_docker_gate_defaults_to_signal_preserving_shell() -> None:
    from orchestrator.testgate import DockerGate, _subprocess_shell_gate

    assert DockerGate(frontend_gate="FE", compose_rel="c.yml").shell is _subprocess_shell_gate


def test_frontend_touched() -> None:
    from orchestrator.testgate import frontend_touched

    prefixes = ("frontend/", "apps/", "packages/")
    assert frontend_touched(["frontend/src/App.tsx"], prefixes) is True
    assert frontend_touched(["apps/public/src/x.astro"], prefixes) is True
    assert frontend_touched(["myapp/models.py", "tests/test_x.py"], prefixes) is False


# --- BindingGate: deterministic gate, all in-container at the pinned sha ------


def test_binding_gate_command_runs_everything_in_container_offline() -> None:
    from orchestrator.testgate import BindingGate

    compose_rel = f"{TARGET_DIR_NAME}/docker/compose.test.yml"
    cmd = BindingGate(compose_rel=compose_rel).command("deadbeef", "myapp")
    # containerized on a clean SHA-pinned clone (untrusted code never runs on host)
    assert "git clone --no-local ." in cmd
    assert "checkout -q deadbeef" in cmd
    assert compose_rel in cmd
    # coverage.xml is produced (fixes the gate that always held) and consumed
    assert "--cov=myapp" in cmd and "--cov-report=xml" in cmd
    assert "diff-cover coverage.xml" in cmd
    # semgrep runs OFFLINE against the committed ruleset, never --config auto
    assert f"--config {TARGET_DIR_NAME}/security/semgrep.yml" in cmd
    assert "--config auto" not in cmd
    assert "gitleaks detect" in cmd
    # all three scanners are diff-scoped to origin/main (judge the change, not
    # the legacy - a full-history run re-flags pre-existing findings forever)
    assert "--baseline-commit origin/main" in cmd  # semgrep
    assert "--log-opts=origin/main..HEAD" in cmd  # gitleaks
    assert "--compare-branch=origin/main" in cmd  # diff-cover
    # the @@GATE line echoes per-step codes for DIAGNOSTICS; pass/fail is the
    # container EXIT status, so the gate ends with a composite `exit`.
    assert "echo @@GATE lint=" in cmd
    assert "exit $(( L || T || P || C || S || G ))" in cmd


def test_binding_gate_command_uses_custom_compare_ref() -> None:
    # #11: the local gate baselines the scanners to the current local-main sha
    # (the trial-merge parent), not the stale origin/main remote-tracking ref.
    from orchestrator.testgate import BindingGate

    cmd = BindingGate(compose_rel="c.yml").command("sha", "myapp", compare_ref="localmainsha")
    assert "--compare-branch=localmainsha" in cmd  # diff-cover
    assert "--baseline-commit localmainsha" in cmd  # semgrep
    assert "--log-opts=localmainsha..HEAD" in cmd  # gitleaks
    assert "origin/main" not in cmd


def test_binding_gate_command_single_quotes_gate_so_rc_survives() -> None:
    from orchestrator.testgate import BindingGate

    cmd = BindingGate(compose_rel="c.yml").command("abc", "myapp")
    # the gate value is single-quoted (not double) so the host shell does not
    # expand $? / $L before the container sees them
    assert "GATE_COMMAND='" in cmd


def test_binding_gate_runs_trusted_infra_not_the_branch() -> None:
    # NÁLEZ 1: the compose file / Dockerfile / semgrep ruleset the gate BUILDS
    # and RUNS must come from TRUSTED main, not the untrusted branch clone -
    # else a branch adds `privileged: true` / a host bind-mount to
    # compose.test.yml and escapes onto the Director's daemon DURING
    # verification (before any L3 classification can hold the merge).
    from orchestrator.testgate import BindingGate

    cmd = BindingGate(compose_rel="c.yml").command("branchsha", "myapp", trusted_ref="trustedsha")
    # the gate infra is restored from the trusted ref over the branch clone
    assert f"checkout trustedsha -- {TARGET_DIR_NAME}/docker {TARGET_DIR_NAME}/security" in cmd
    # ...AFTER the branch sha is checked out, BEFORE cd + docker compose
    assert cmd.index("checkout -q branchsha") < cmd.index("checkout trustedsha")
    assert cmd.index("checkout trustedsha") < cmd.index("docker compose")


def test_binding_gate_without_trusted_ref_does_not_touch_infra() -> None:
    # the VPS pre-filter (DockerGate) runs on the untrusted box - no trusted ref
    # to restore from; the infra-checkout must be absent, not empty/broken.
    from orchestrator.testgate import BindingGate

    cmd = BindingGate(compose_rel="c.yml").command("branchsha", "myapp")
    assert f"{TARGET_DIR_NAME}/docker {TARGET_DIR_NAME}/security" not in cmd


_GREEN = "@@GATE lint=0 types=0 tests=0 coverage=0 semgrep=0 gitleaks=0"


def test_parse_binding_all_green() -> None:
    from orchestrator.testgate import parse_binding_output

    # zero container exit is authoritative green - the @@GATE line is not even
    # needed (it is diagnostic only).
    r = parse_binding_output("...pytest ran...\n", container_rc=0)
    assert r.tests_passed and r.coverage_ok and r.scan_findings == ()


def test_parse_binding_zero_exit_green_ignores_a_red_line() -> None:
    from orchestrator.testgate import parse_binding_output

    # a zero exit means every gate's captured code was zero; a stray "red" line
    # (noise, a partial earlier echo) cannot flip an authoritative pass.
    out = "@@GATE lint=1 types=0 tests=1 coverage=0 semgrep=0 gitleaks=0\n"
    r = parse_binding_output(out, container_rc=0)
    assert r.tests_passed and r.coverage_ok


def test_parse_binding_test_failure() -> None:
    from orchestrator.testgate import parse_binding_output

    out = "FAILED test_x\n@@GATE lint=0 types=0 tests=1 coverage=0 semgrep=0 gitleaks=0\n"
    r = parse_binding_output(out, container_rc=1)
    assert r.tests_passed is False
    assert "FAILED test_x" in r.tests_tail


def test_parse_binding_coverage_and_scan_failures() -> None:
    from orchestrator.testgate import parse_binding_output

    out = "@@GATE lint=0 types=0 tests=0 coverage=1 semgrep=1 gitleaks=0\n"
    r = parse_binding_output(out, container_rc=1)
    assert r.tests_passed is True  # diagnostic granularity on a failed run
    assert r.coverage_ok is False
    assert any("semgrep" in f for f in r.scan_findings)


def test_parse_binding_missing_scanner_is_explicit_finding() -> None:
    from orchestrator.testgate import parse_binding_output

    out = "@@GATE lint=0 types=0 tests=0 coverage=0 semgrep=127 gitleaks=0\n"
    r = parse_binding_output(out, container_rc=1)
    assert any("semgrep" in f and "not installed" in f.lower() for f in r.scan_findings)


def test_parse_binding_no_sentinel_fails_closed() -> None:
    from orchestrator.testgate import parse_binding_output

    # docker itself failed / image build error: non-zero exit, no @@GATE line
    r = parse_binding_output("docker: build error\n", container_rc=1)
    assert r.tests_passed is False
    assert r.coverage_ok is False
    assert r.scan_findings  # a finding explaining the gate did not report


def test_parse_binding_forged_all_green_with_nonzero_exit_is_failed() -> None:
    from orchestrator.testgate import parse_binding_output

    # C1 regression: in-container code that learned to print a genuine @@GATE
    # all-pass line cannot clear the gate - the non-zero container exit (which
    # it cannot forge) is authoritative, so this fails closed as tampering.
    r = parse_binding_output(f"{_GREEN}\n", container_rc=1)
    assert r.tests_passed is False
    assert r.coverage_ok is False
    assert any("tamper" in f.lower() for f in r.scan_findings)


def test_binding_gate_run_keys_off_exit_code(tmp_path: Path) -> None:
    from orchestrator.testgate import BindingGate
    from tests.fakes import FakeSplitShell

    # the fake derives the container exit code from the codes (tests=1 -> rc 1)
    shell = FakeSplitShell(
        echo_sentinel="lint=0 types=0 tests=1 coverage=0 semgrep=0 gitleaks=0"
    )
    r = BindingGate(compose_rel="c.yml", shell=shell).run(tmp_path, "abc", "myapp")
    assert r.tests_passed is False
    assert shell.calls[0][1] == tmp_path
