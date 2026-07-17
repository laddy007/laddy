"""Test gates (design doc S11 names this module tests.py; renamed testgate.py
so it can never be confused with the pytest suite).

Slice 1: fast inner gate - one configured shell command run in the task
worktree. The authoritative Docker gate lands in Slice 2.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from orchestrator import TARGET_DIR_NAME

# (command, cwd) -> (returncode, combined output)
ShellRunner = Callable[[str, Path], tuple[int, str]]
# (command, cwd) -> (returncode, stdout, stderr) - the binding gate needs the
# streams kept apart (see parse_binding_output / _subprocess_shell_split).
SplitShellRunner = Callable[[str, Path], tuple[int, str, str]]

_TAIL_LINES = 400

# A wedged gate (a hung `pytest` / `docker compose`) must never block the loop
# forever. Generous default (the full backend suite + frontend build can be
# slow); override with GATE_TIMEOUT_S. On timeout the run reports non-zero and,
# for the binding gate, no @@GATE sentinel -> fail-closed.
_GATE_TIMEOUT_S = int(os.environ.get("GATE_TIMEOUT_S", str(90 * 60)))


def _subprocess_shell_split(command: str, cwd: Path) -> tuple[int, str, str]:
    """Run a gate command, keeping stdout and stderr SEPARATE.

    The binding gate's pass/fail is the container's exit code (returned here as
    the first element); the streams are kept apart only so the diagnostic tail
    reads cleanly - the @@GATE line and the pytest summary are on stdout while
    docker/compose progress floods stderr, and mixing them buries the signal.
    """
    try:
        proc = subprocess.run(
            ["bash", "-lc", command],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GATE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        err = exc.stderr if isinstance(exc.stderr, str) else ""
        return 124, out, f"{err}\ngate timed out after {_GATE_TIMEOUT_S}s"
    return proc.returncode, proc.stdout, proc.stderr


def _subprocess_shell(command: str, cwd: Path) -> tuple[int, str]:
    rc, out, err = _subprocess_shell_split(command, cwd)
    return rc, out + err


def _subprocess_shell_gate(command: str, cwd: Path) -> tuple[int, str]:
    """Combined-output shell for the containerized authoritative gate that
    keeps the test SIGNAL visible in the tail.

    docker/compose write build + teardown progress (hundreds of lines) to
    stderr; pytest/ruff write their result to stdout. A naive stdout+stderr
    concatenation puts stdout FIRST, so ``_tail`` (which reads the END) returns
    only the stderr flood - the ``N passed`` summary, or a real test failure,
    never survives into ``output_tail`` or the rework ``detail``. Emit stderr
    first and stdout LAST so the tail lands on the gate's actual result; on an
    infra failure with no stdout, the stderr tail still surfaces the error.
    """
    rc, out, err = _subprocess_shell_split(command, cwd)
    sections: list[str] = []
    if err.strip():
        sections.append("--- stderr ---\n" + err.rstrip("\n"))
    if out.strip():
        sections.append("--- stdout ---\n" + out.rstrip("\n"))
    return rc, "\n".join(sections)


@dataclass(frozen=True)
class TestResult:
    __test__ = False  # keep pytest from collecting this as a test class

    passed: bool
    output_tail: str
    command: str


def _tail(text: str) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-_TAIL_LINES:])


def run_fast(
    commands: str, cwd: Path, shell: ShellRunner = _subprocess_shell
) -> TestResult:
    """Run the fast inner test commands (TEST_COMMANDS config) in the worktree."""
    rc, output = shell(commands, cwd)
    return TestResult(passed=rc == 0, output_tail=_tail(output), command=commands)


# --- Authoritative Docker gate (design S7) ----------------------------------

# Mandatory always: the FULL backend suite + lint + types (no subsetting -
# "touched scope" heuristics are exactly the flexibility an agent can bend).
BACKEND_GATE = "ruff check . && basedpyright && pytest -n auto -q"
# The frontend gate command and which path prefixes trigger it are per-target
# (target_policy: frontend_gate / frontend_prefixes) - a target with no frontend
# simply never triggers it. Threaded in by the caller, never hardcoded here.


def frontend_touched(changed_files: Sequence[str], prefixes: Sequence[str]) -> bool:
    return any(f.startswith(tuple(prefixes)) for f in changed_files)


@dataclass(frozen=True)
class AuthoritativeResult:
    sha: str
    passed: bool
    output_tail: str
    flaky: bool
    include_frontend: bool

    def to_json(self) -> dict[str, object]:
        return {
            "sha": self.sha,
            "passed": self.passed,
            "flaky": self.flaky,
            "include_frontend": self.include_frontend,
            "output_tail": self.output_tail,
        }


class DockerGate:
    """Clean containerized run of the full gate on the current HEAD SHA.

    The worktree is cloned (no-local) into a throwaway directory at the
    exact SHA, then the compose test service runs the gate inside Docker
    against a fresh Postgres (compose db over docker DNS).

    ``compose_rel`` is the compose file path RELATIVE to the repo root, so
    after ``cd`` into the fresh clone both ``-f`` and the compose file's
    relative ``build.context`` resolve inside the clone at ``sha`` - never
    the live worktree (which may hold uncommitted edits).
    """

    def __init__(
        self,
        frontend_gate: str,
        compose_rel: str = f"{TARGET_DIR_NAME}/docker/compose.test.yml",
        shell: ShellRunner = _subprocess_shell_gate,
    ) -> None:
        self.frontend_gate = frontend_gate
        self.compose_rel = compose_rel
        self.shell = shell

    def command(self, sha: str, include_frontend: bool) -> str:
        gate = BACKEND_GATE + (" && " + self.frontend_gate if include_frontend else "")
        return _containerized(self.compose_rel, sha, gate)

    def run(self, wt: Path, sha: str, include_frontend: bool) -> AuthoritativeResult:
        rc, output = self.shell(self.command(sha, include_frontend), wt)
        return AuthoritativeResult(
            sha=sha,
            passed=rc == 0,
            output_tail=_tail(output),
            flaky=False,
            include_frontend=include_frontend,
        )


def _compose_project(sha: str) -> str:
    """Unique COMPOSE_PROJECT_NAME for one gate run.

    Keyed on the sha under test + the host process id so two gate runs (the
    VPS pre-filter and the local binding gate, or two concurrent tasks) never
    share containers/networks/volumes and clobber each other.
    """
    return f"laddy-gate-{sha[:12]}-{os.getpid()}"


# The gate infra restored from the trusted ref over the branch clone. Declared
# once: the restore command below and restored_infra_paths() (which tells the
# caller WHOSE copy a green run actually judged) must never drift apart.
RESTORED_INFRA_PATHS: tuple[str, ...] = (
    f"{TARGET_DIR_NAME}/docker",
    f"{TARGET_DIR_NAME}/security",
)


def restored_infra_paths(changed: Iterable[str]) -> tuple[str, ...]:
    """Which of ``changed`` the restore silently replaces with trusted main's copy.

    A branch that edits these paths never has its OWN version verified: the gate
    ran the trusted copy instead, so a green result says nothing about the
    branch's edit, and a red one may be the restore's doing rather than the
    branch's defect (a branch changing the semgrep ruleset gets its own tests run
    against main's rules). Neither reading is safe to leave implicit, so the
    caller names these paths in its report instead of guessing.

    Restoring is deliberate and stays (NÁLEZ 1) - this only makes its cost
    visible. Matching is path-segment exact: ``<dir>/security-notes.md`` is not
    under ``<dir>/security`` and is not restored.
    """
    return tuple(
        p
        for p in changed
        if any(p == d or p.startswith(f"{d}/") for d in RESTORED_INFRA_PATHS)
    )


def _containerized(
    compose_rel: str, sha: str, gate_command: str, trusted_ref: str | None = None
) -> str:
    """Wrap ``gate_command`` to run inside the compose ``test`` service against
    a clean SHA-pinned clone, so untrusted branch code NEVER executes on the
    host and the reviewed commit (not the live worktree) is what runs.

    ``trusted_ref`` (NÁLEZ 1): the gate INFRASTRUCTURE the daemon builds and
    runs - the compose file, the Dockerfile, and the semgrep ruleset under
    ``<agent-dir>/docker`` + ``<agent-dir>/security`` - is attacker-controlled if taken
    from the branch (a branch could set ``privileged: true`` or bind-mount the
    host and escape during verification, before any L3 classification holds the
    merge). When ``trusted_ref`` is given (the local trusted main), those paths
    are restored from it over the branch clone, extending "judge is local,
    judged is the branch" to the gate infra - not just ``merge_check``. The
    untrusted VPS pre-filter (DockerGate) passes ``None``: no trusted ref exists
    on that box and it is not a trust boundary.

    GATE_COMMAND is SINGLE-quoted: the gate may carry per-step ``$?`` captures
    that must survive the host shell and be expanded only by the container's
    ``bash -lc "$GATE_COMMAND"``. The gate command therefore must not contain a
    single quote (the binding gate and BACKEND_GATE do not).
    """
    if "'" in gate_command:  # pragma: no cover - guards a programming error
        raise ValueError("gate_command must not contain a single quote")
    project = _compose_project(sha)
    restore_infra = (
        f'&& git -C "$tmp/repo" checkout {trusted_ref} -- {" ".join(RESTORED_INFRA_PATHS)} '
        if trusted_ref
        else ""
    )
    return (
        f'tmp="$(mktemp -d)" && git clone --no-local . "$tmp/repo" '
        f'&& git -C "$tmp/repo" checkout -q {sha} '
        f"{restore_infra}"
        f'&& cd "$tmp/repo" '
        f"&& GATE_COMMAND='{gate_command}' TARGET_DIR_NAME='{TARGET_DIR_NAME}' "
        f"docker compose -p {project} -f {compose_rel} "
        # -T: no pseudo-TTY. The gate runs from a subprocess with captured
        # output (never an interactive shell); without it docker compose emits
        # "failed to resize tty" and can mangle the captured stream.
        f"run --rm --build -T test; rc=$?; "
        f'docker compose -p {project} -f {compose_rel} down -v >/dev/null 2>&1; '
        f'rm -rf "$tmp"; exit $rc'
    )


# --- Binding deterministic gate (trust-model doc S6/S7) ----------------------
#
# ONE containerized run at the pinned sha executing every DETERMINISTIC gate:
# lint + types + tests(+coverage.xml) + diff-coverage + semgrep(offline) +
# gitleaks. Each step's exit code is captured and echoed on a single @@GATE
# sentinel line the host parses into granular results - so a single container
# invocation yields per-gate outcomes without four separate host shells (which
# would each run untrusted code on the trusted machine).

_COVERAGE_COMPARE = "origin/main"
_COVERAGE_MIN = 90
SEMGREP_CONFIG = f"{TARGET_DIR_NAME}/security/semgrep.yml"

# All three scanners are DIFF-SCOPED to origin/main: the gate judges THE
# CHANGE, not the repo's legacy. Otherwise every task re-flags pre-existing
# findings (a real full-history gitleaks run reported 84 old hits -> every task
# would be BROKEN). coverage = changed lines (--compare-branch); semgrep = new
# findings only (--baseline-commit); gitleaks = commits in the branch
# (--log-opts origin/main..HEAD).
# The gate's pass/fail is the CONTAINER EXIT CODE (see parse_binding_output):
# the trusted tail runs `exit $(( L || T || ... ))`, which untrusted in-container
# code cannot alter. The @@GATE line echoes the per-step codes for DIAGNOSTICS
# only (which gate failed) - it is a fixed public marker, not a secret, and is
# never trusted for the decision. (An earlier design nonced this line and
# trusted stdout; the nonce leaked via GATE_COMMAND into the very container it
# was meant to defend against, so the decision now keys off the exit code.)
# NOTE: no single quotes anywhere (see _containerized).
_SENTINEL_PREFIX = "@@GATE"


def _binding_gate(coverage_package: str, compare_ref: str = _COVERAGE_COMPARE) -> str:
    # coverage_package is per-target (target_policy) so the gate covers the right
    # package instead of a hardcoded one. compare_ref scopes
    # coverage/semgrep/gitleaks to THE CHANGE. The local binding gate passes the
    # current local-main sha (the trial-merge's parent) so the diff base is the
    # tree we are actually merging into, not the stale origin/main remote-tracking
    # ref; the VPS DockerGate keeps the default. The trailing `exit` makes the
    # container status the authoritative signal: non-zero iff any gate failed -
    # unforgeable by code that ran earlier.
    return (
        "set +e; "
        "ruff check .; L=$?; "
        "basedpyright; T=$?; "
        f"pytest -n auto --cov={coverage_package} --cov-report=xml -q; P=$?; "
        f"diff-cover coverage.xml --compare-branch={compare_ref} "
        f"--fail-under={_COVERAGE_MIN}; C=$?; "
        f"semgrep --error --config {SEMGREP_CONFIG} --baseline-commit {compare_ref} .; S=$?; "
        f"gitleaks detect --no-banner --log-opts={compare_ref}..HEAD; G=$?; "
        f"echo {_SENTINEL_PREFIX} lint=$L types=$T tests=$P coverage=$C semgrep=$S gitleaks=$G; "
        "exit $(( L || T || P || C || S || G ))"
    )


@dataclass(frozen=True)
class BindingResult:
    tests_passed: bool
    tests_tail: str
    coverage_ok: bool
    coverage_detail: str
    scan_findings: tuple[str, ...]


def _scan_finding(name: str, rc: int) -> str | None:
    if rc == 0:
        return None
    if rc == 127:
        return (
            f"scanner {name!r} is not installed in the gate image - cannot "
            "clear the security gate (rebuild the image)"
        )
    return f"{name} flagged the change (exit {rc})"


def parse_binding_output(
    stdout: str, container_rc: int, combined_tail: str | None = None
) -> BindingResult:
    """Turn the containerized gate's result into granular pass/fail.

    The AUTHORITATIVE signal is ``container_rc`` - the exit code of the gate's
    trusted tail (``exit $(( L || T || ... ))``), propagated out of the
    container. Untrusted branch code running earlier in the same container (a
    test, a conftest) cannot alter that tail's exit status, so a zero container
    exit means every deterministic gate genuinely passed - full stop.

    The ``@@GATE`` line is DIAGNOSTIC only: on a non-zero exit it says WHICH gate
    failed for the human-facing reason. It is not nonced and never trusted for
    the decision. A non-zero exit with an all-pass @@GATE line is treated as
    failed (tampering, or a failure the line did not capture); a missing line on
    a non-zero exit is a container/build/timeout failure - both fail closed.
    ``combined_tail`` (stdout+stderr) is used only for the diagnostic tail.
    """
    tail = _tail(combined_tail if combined_tail is not None else stdout)
    if container_rc == 0:
        return BindingResult(
            tests_passed=True,
            tests_tail=tail,
            coverage_ok=True,
            coverage_detail="all gates passed (container exit 0)",
            scan_findings=(),
        )
    line = next(
        (
            ln
            for ln in reversed(stdout.splitlines())
            if ln.strip().startswith(_SENTINEL_PREFIX)
        ),
        None,
    )
    if line is None:
        return BindingResult(
            tests_passed=False,
            tests_tail=tail,
            coverage_ok=False,
            coverage_detail=f"binding gate exited {container_rc} with no @@GATE result "
            "(container/build/timeout failure)",
            scan_findings=("binding gate did not report - treated as failed (fail-closed)",),
        )
    codes: dict[str, int] = {}
    for tok in line.strip().removeprefix(_SENTINEL_PREFIX).split():
        key, _, val = tok.partition("=")
        try:
            codes[key] = int(val)
        except ValueError:
            codes[key] = 1
    findings = tuple(
        f
        for f in (
            _scan_finding("semgrep", codes.get("semgrep", 1)),
            _scan_finding("gitleaks", codes.get("gitleaks", 1)),
        )
        if f is not None
    )
    tests_passed = (
        codes.get("lint", 1) == 0
        and codes.get("types", 1) == 0
        and codes.get("tests", 1) == 0
    )
    coverage_ok = codes.get("coverage", 1) == 0
    if tests_passed and coverage_ok and not findings:
        # non-zero exit but the line claims everything green: never trust it.
        return BindingResult(
            tests_passed=False,
            tests_tail=tail,
            coverage_ok=False,
            coverage_detail=f"binding gate exited {container_rc} but @@GATE reported "
            "all-pass - treated as failed (possible tampering)",
            scan_findings=(
                f"gate exit {container_rc} contradicts an all-pass @@GATE line "
                "- treated as failed (possible tampering)",
            ),
        )
    return BindingResult(
        tests_passed=tests_passed,
        tests_tail=tail,
        coverage_ok=coverage_ok,
        coverage_detail=f"diff-cover exit {codes.get('coverage', 1)} "
        f"(min {_COVERAGE_MIN}% patch coverage vs {_COVERAGE_COMPARE})",
        scan_findings=findings,
    )


class BindingGate:
    """Deterministic local gate: all checks in one container run at the sha."""

    def __init__(
        self,
        compose_rel: str = f"{TARGET_DIR_NAME}/docker/compose.test.yml",
        shell: SplitShellRunner = _subprocess_shell_split,
    ) -> None:
        self.compose_rel = compose_rel
        self.shell = shell

    def command(
        self,
        sha: str,
        coverage_package: str,
        trusted_ref: str | None = None,
        compare_ref: str = _COVERAGE_COMPARE,
    ) -> str:
        return _containerized(
            self.compose_rel,
            sha,
            _binding_gate(coverage_package, compare_ref),
            trusted_ref,
        )

    def run(
        self,
        wt: Path,
        sha: str,
        coverage_package: str,
        trusted_ref: str | None = None,
        compare_ref: str = _COVERAGE_COMPARE,
    ) -> BindingResult:
        # coverage_package is per-target (from the trusted policy the caller
        # loaded). trusted_ref restores the gate infra (compose/Dockerfile/
        # semgrep) from the local trusted main, so the branch cannot ship a
        # hostile container definition (NÁLEZ 1). The decision keys off the
        # container EXIT CODE (unforgeable by code that ran earlier), not the
        # parsed stdout line. compare_ref baselines the scanners to the tree
        # being merged into.
        rc, out, err = self.shell(
            self.command(sha, coverage_package, trusted_ref, compare_ref), wt
        )
        # err first, out LAST (see _subprocess_shell_gate): docker/compose
        # build+teardown noise floods stderr, so putting it first means _tail
        # (last N lines) lands on stdout's @@GATE line and pytest/ruff/
        # basedpyright result instead of burying it under container chatter.
        return parse_binding_output(out, rc, combined_tail=f"{err}\n{out}")
