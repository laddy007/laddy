"""Shared fakes for orchestrator unit tests. No real LLM/network/docker."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.agents import AgentResult
from orchestrator.local_merge import merge_subject
from orchestrator.target_policy import POLICY_REL, TargetPolicy, dump_target_policy


def write_policy_toml(repo_root: Path) -> None:
    """Seed a test repo with a rich sample policy so code paths that
    load_target_policy find a valid <target>/.laddy/policy.toml. Uses
    TargetPolicy.myapp() (auth/payments/frontend/migration surface) - the
    oracle/merge tests key off its sample paths (myapp/models.py sensitive,
    etc.), so this stays the rich fixture and is deliberately DECOUPLED from the
    shipped .laddy/policy.toml (which is now laddy's own sparse dogfood policy)."""
    dst = repo_root / POLICY_REL
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(
        dump_target_policy(TargetPolicy.myapp()),
        encoding="utf-8",
        newline="\n",
    )


@dataclass
class FakeCall:
    prompt: str
    cwd: Path
    resume: str | None


class FakeRunner:
    """Returns queued outputs; records every call. str output => ok result."""

    name = "fake"

    def __init__(self, outputs: Sequence[AgentResult | str] | None = None) -> None:
        self._outputs: list[AgentResult | str] = list(outputs or [])
        self.calls: list[FakeCall] = []

    def queue(self, *outputs: AgentResult | str) -> None:
        self._outputs.extend(outputs)

    def run(self, prompt: str, cwd: Path, resume: str | None = None) -> AgentResult:
        self.calls.append(FakeCall(prompt, cwd, resume))
        if not self._outputs:
            raise AssertionError(f"FakeRunner({self.name}) ran out of queued outputs")
        out = self._outputs.pop(0)
        if isinstance(out, str):
            return AgentResult(
                text=out,
                session_id=f"{self.name}-s{len(self.calls)}",
                exit_reason="ok",
                returncode=0,
            )
        return out


@dataclass
class FakeShell:
    """ShellRunner fake: queued (rc, output) results; records commands."""

    results: list[tuple[int, str]] = field(default_factory=list)
    calls: list[tuple[str, Path]] = field(default_factory=list)

    def __call__(self, command: str, cwd: Path) -> tuple[int, str]:
        self.calls.append((command, cwd))
        if not self.results:
            # fail CLOSED, like FakeRunner: an unqueued command means the code
            # under test ran an extra gate - inventing a green result would
            # hide exactly that regression
            raise AssertionError(f"FakeShell ran out of queued results on: {command}")
        return self.results.pop(0)


@dataclass
class FakeSplitShell:
    """SplitShellRunner fake (BindingGate): queued (rc, stdout, stderr).

    ``echo_sentinel`` makes the fake emit the gate's @@GATE diagnostic line
    followed by those codes on stdout, and returns a container exit code derived
    from them (0 iff every gate reads ``=0``) - matching the real gate, whose
    EXIT STATUS (not the stdout line) is the authoritative pass/fail signal.
    """

    results: list[tuple[int, str, str]] = field(default_factory=list)
    calls: list[tuple[str, Path]] = field(default_factory=list)
    echo_sentinel: str | None = None  # the "lint=0 ..." codes to echo, or None
    stdout_prefix: str = ""  # stdout emitted before the sentinel line (e.g. a test log)

    def __call__(self, command: str, cwd: Path) -> tuple[int, str, str]:
        self.calls.append((command, cwd))
        if self.echo_sentinel is not None:
            sentinel = command.split("echo ", 1)[1].split(" ", 1)[0]
            rc = 0 if all(
                tok.partition("=")[2] == "0" for tok in self.echo_sentinel.split()
            ) else 1
            return (rc, f"{self.stdout_prefix}\n{sentinel} {self.echo_sentinel}\n", "")
        if not self.results:
            raise AssertionError(
                f"FakeSplitShell ran out of queued results on: {command}"
            )
        return self.results.pop(0)


def verdict_json(
    verdict: str = "APPROVED",
    findings: list[dict[str, object]] | None = None,
    risk: str = "low",
) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "risk_level": risk,
            "files_reviewed": ["a.py"],
            "claims_verified": [{"claim": "c", "evidence": "e", "verified": True}],
            "findings": findings or [],
            "test_assessment": "ok",
            "residual_risks": [],
        }
    )


def blocker(
    category: str = "correctness",
    file: str = "a.py",
    summary: str = "off-by-one",
    failure_scenario: str = "input 0 crashes",
) -> dict[str, object]:
    return {
        "severity": "blocker",
        "category": category,
        "file": file,
        "line": 1,
        "summary": summary,
        "failure_scenario": failure_scenario,
    }


def advisory(
    category: str = "quality",
    summary: str = "naming could be clearer",
) -> dict[str, object]:
    return {
        "severity": "advisory",
        "category": category,
        "file": "a.py",
        "line": 2,
        "summary": summary,
        "failure_scenario": "",
    }


# --- git repo helpers for oracle tests (real local repos, no remote) ---------

_GIT_IDENTITY = ("-c", "user.name=test", "-c", "user.email=test@example.com")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *_GIT_IDENTITY, *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def init_repo(path: Path) -> Path:
    """A local repo on main with one seed commit (incl. the policy config)."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "--initial-branch=main")
    (path / "README.md").write_text("seed\n", encoding="utf-8", newline="\n")
    write_policy_toml(path)
    git(path, "add", "-A")
    git(path, "commit", "-m", "init")
    return path


def merge_agent_task(repo: Path, task_id: str, files: Mapping[str, str]) -> str:
    """Create the bare ``<task_id>`` branch with ``files`` and merge it into
    main using the EXACT merge subject local_merge.merge_branch produces, so
    the oracle's range scanner sees production-shaped history. Returns the
    merge sha."""
    git(repo, "checkout", "-B", task_id)
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", f"task {task_id}")
    branch_sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    git(repo, "merge", "--no-ff", task_id,
        "-m", merge_subject(task_id, branch_sha))
    return git(repo, "rev-parse", "HEAD")
