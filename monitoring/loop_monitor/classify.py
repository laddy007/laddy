"""Single source of truth for safe process classification.

Command lines are inspected in memory because executable names alone cannot
distinguish ``python -m uvicorn`` from pytest or a generic ``node`` process
from Vite. The returned structure contains only closed categories, a safe
task identifier and a repository basename. Raw argv is never returned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SAFE_TASK = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


@dataclass(frozen=True)
class ProcessIdentity:
    category: str
    operation: str
    vendor: str | None = None
    task: str | None = None
    repo: str | None = None
    is_agent_helper: bool = False

    def to_json(self) -> dict[str, object]:
        value: dict[str, object] = {
            "category": self.category,
            "operation": self.operation,
        }
        if self.vendor is not None:
            value["vendor"] = self.vendor
        if self.task is not None:
            value["task"] = self.task
        if self.repo is not None:
            value["repo"] = self.repo
        if self.is_agent_helper:
            value["agent_helper"] = True
        return value


def _repo_name(cwd: str | None) -> str | None:
    if not cwd or cwd.startswith("["):
        return None
    name = Path(cwd).name
    if not name or not _SAFE_TASK.fullmatch(name):
        return None
    return name


def _task_after(argv: tuple[str, ...], marker: str) -> str | None:
    try:
        index = argv.index(marker)
    except ValueError:
        return None
    if index + 1 >= len(argv):
        return None
    task = argv[index + 1]
    return task if _SAFE_TASK.fullmatch(task) else None


def _contains(words: str, *needles: str) -> bool:
    return any(needle in words for needle in needles)


def _has_tool(argv: tuple[str, ...], *tools: str) -> bool:
    for token in argv:
        lowered = token.lower()
        name = Path(lowered).name
        stem = name.removesuffix(".js").removesuffix(".mjs").removesuffix(".cjs")
        if any(
            stem == tool
            or stem.startswith(f"{tool}-")
            or f"/{tool}/" in lowered
            or f"/{tool}." in lowered
            for tool in tools
        ):
            return True
    return False


def classify_process(
    *, comm: str, argv: tuple[str, ...], cwd: str | None
) -> ProcessIdentity:
    """Classify a process without exposing its argument vector."""

    executable = (argv[0] if argv else comm).lower()
    executable_name = Path(executable).name
    # A shell's -c payload may mention commands in a watcher, condition or
    # comment long after they stopped. Its real child is classified instead.
    semantic_argv = (
        argv[:1] if executable_name in {"bash", "sh", "dash", "zsh"} else argv
    )
    lowered = "\x00".join(semantic_argv).lower()
    repo = _repo_name(cwd)

    is_claude = (
        comm.lower() == "claude"
        or "/claude/versions/" in executable
        or executable.endswith("/claude")
        or executable == "claude"
    )
    if is_claude:
        helper = _contains(lowered, "bg-pty-host", "bg-spare", "daemon\x00run")
        return ProcessIdentity(
            category="agent_helper" if helper else "agent",
            operation="claude_helper" if helper else "claude_agent",
            vendor="claude",
            repo=repo,
            is_agent_helper=helper,
        )

    is_codex = (
        comm.lower().startswith("codex")
        or executable.endswith("/codex")
        or "/@openai/codex/" in executable
    )
    if is_codex:
        helper = _contains(lowered, "app-server", "mcp-server", "exec-server")
        return ProcessIdentity(
            category="agent_helper" if helper else "agent",
            operation="codex_helper" if helper else "codex_agent",
            vendor="codex",
            repo=repo,
            is_agent_helper=helper,
        )

    if "orchestrator.run" in lowered:
        return ProcessIdentity(
            category="orchestrator",
            operation="agent_orchestrator",
            task=_task_after(argv, "orchestrator.run"),
            repo=repo,
        )

    lowered_tokens = tuple(token.lower() for token in semantic_argv)
    pytest_tokens = {Path(token).name for token in lowered_tokens}
    module_pytest = any(
        semantic_argv[index] == "-m" and semantic_argv[index + 1].lower() == "pytest"
        for index in range(len(semantic_argv) - 1)
    )
    if (
        comm.lower().startswith("pytest")
        or pytest_tokens.intersection({"pytest", "py.test"})
        or module_pytest
    ):
        return ProcessIdentity(category="test", operation="pytest", repo=repo)

    if _has_tool(semantic_argv, "vitest", "jest") or (
        _has_tool(semantic_argv, "playwright") and "test" in lowered_tokens
    ):
        return ProcessIdentity(category="test", operation="frontend_test", repo=repo)

    if _contains(lowered, "uvicorn", "gunicorn", "hypercorn"):
        return ProcessIdentity(
            category="backend", operation="backend_server", repo=repo
        )

    if comm.lower() == "postgres" or executable.endswith("/postgres"):
        return ProcessIdentity(category="database", operation="postgres", repo=repo)

    if _contains(lowered, "alembic", "pg_dump", "pg_restore", "psql"):
        return ProcessIdentity(
            category="database", operation="database_operation", repo=repo
        )

    build_driver = _has_tool(
        semantic_argv,
        "npm",
        "npm-cli",
        "pnpm",
        "yarn",
        "vite",
        "astro",
        "docker",
        "esbuild",
        "rollup",
        "webpack",
    )
    if (
        ("build" in lowered_tokens and build_driver)
        or _has_tool(semantic_argv, "tsc", "esbuild", "rollup", "webpack")
        or comm.lower() in {"buildkitd", "buildctl"}
    ):
        return ProcessIdentity(category="build", operation="build", repo=repo)

    if _has_tool(semantic_argv, "vite", "astro") or (
        "dev" in lowered_tokens
        and _has_tool(semantic_argv, "npm", "npm-cli", "pnpm", "yarn")
    ):
        return ProcessIdentity(category="frontend", operation="frontend_dev", repo=repo)

    if _has_tool(semantic_argv, "ruff", "basedpyright", "mypy"):
        return ProcessIdentity(category="quality", operation="quality_check", repo=repo)

    if comm.lower() in {"dockerd", "containerd", "containerd-shim"}:
        return ProcessIdentity(
            category="container_runtime", operation=comm.lower(), repo=repo
        )

    module_monitor = executable_name.startswith("python") and any(
        semantic_argv[index] == "-m"
        and semantic_argv[index + 1].lower() in {"loop_monitor", "loop_monitor.cli"}
        for index in range(len(semantic_argv) - 1)
    )
    if module_monitor or executable_name == "loop-monitor":
        return ProcessIdentity(
            category="monitoring", operation="loop_monitor", repo=repo
        )

    return ProcessIdentity(category="other", operation="other", repo=repo)


def inherited_identity(
    identity: ProcessIdentity, parent: ProcessIdentity | None
) -> ProcessIdentity:
    """Derive worker categories from a classified parent without copying rules."""

    if parent is None:
        return identity
    if identity.operation == "pytest" and parent.category == "test":
        return ProcessIdentity(
            category="test",
            operation="pytest_worker",
            repo=identity.repo or parent.repo,
        )
    if identity.category != "other":
        return identity
    if parent.operation in {"pytest", "pytest_worker"}:
        return ProcessIdentity(
            category="test",
            operation="pytest_worker",
            repo=identity.repo or parent.repo,
        )
    if parent.category == "build":
        return ProcessIdentity(
            category="build", operation="build_worker", repo=identity.repo
        )
    if parent.category == "frontend":
        return ProcessIdentity(
            category="frontend", operation="frontend_worker", repo=identity.repo
        )
    return identity
