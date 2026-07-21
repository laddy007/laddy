"""Typed orchestrator configuration from environment variables.

Bash (kickoff.sh) only exports env vars; all interpretation happens here.
Mirrors the knobs of the legacy agent-flow.sh where they still apply.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

DEFAULT_FAST_COMMANDS = (
    ". .venv/bin/activate && ruff check . && basedpyright && pytest -n auto -q"
)

# DEFAULT_FAST_COMMANDS activates .venv, but a fresh (gitignored) task worktree
# has none and the loop had no setup step - so fast_tests died every round on
# "activate: No such file" and the task burned rounds to the cap. This bootstrap
# runs ONCE per worktree (loop._ensure_setup) before the first fast_tests.
# ${PYTHON_BIN:-python3}: the box's default python3 may be too old for the engine
# (tomllib), so honor the same PYTHON_BIN knob kickoff sources from env.vps. A
# non-Python target overrides this via SETUP_COMMANDS, exactly as with TEST_COMMANDS.
DEFAULT_SETUP_COMMANDS = (
    "test -d .venv || ${PYTHON_BIN:-python3} -m venv .venv "
    "&& . .venv/bin/activate && pip install -q -r requirements-dev.txt"
)

# Recognised runner vendors for a role binding (ROLE_<NAME>_VENDOR).
ROLE_VENDORS = ("claude", "codex")

# ROLE_<NAME>_{VENDOR,MODEL,THINKING}: per-role runner binding, parsed
# generically so a new role (rw3, ...) needs no code change. The role name is
# [A-Za-z0-9]+ (no underscores) so the trailing knob segment is unambiguous.
_ROLE_ENV_RE = re.compile(r"^ROLE_([A-Za-z0-9]+)_(VENDOR|MODEL|THINKING)$")


def _claude_cmd(raw: str | None) -> tuple[str, ...]:
    """Parse a CLAUDE_CMD override, enforcing structured JSON output.

    The loop depends on ``--output-format json`` for session ids and
    is_error detection; a user override that omits it would silently
    disable session resume and error detection (ClaudeRunner would always
    hit its plain-text fallback). So we append it when missing rather than
    trust the operator to remember it.
    """
    if not raw:
        return ()
    parts = shlex.split(raw)
    if "--output-format" not in parts:
        parts += ["--output-format", "json"]
    return tuple(parts)


def _least_privilege(parts: tuple[str, ...]) -> tuple[str, ...]:
    """Strip write/exec grants from a LOCAL review command (trust-model S4/S10).

    The local panel reviews untrusted branch code on the trusted machine, so
    its reviewers must never carry --dangerously-skip-permissions / --full-auto
    even if an operator pastes them into a REVIEW_*_CMD override.
    """
    from orchestrator.agents import DANGEROUS_AGENT_FLAGS

    return tuple(p for p in parts if p not in DANGEROUS_AGENT_FLAGS)


def _review_cmd(raw: str | None, default: tuple[str, ...], *, claude: bool) -> tuple[str, ...]:
    if not raw:
        return default
    parts = _claude_cmd(raw) if claude else tuple(shlex.split(raw))
    return _least_privilege(parts)


class ConfigError(ValueError):
    """Invalid orchestrator configuration."""


@dataclass(frozen=True)
class RoleBinding:
    """A role's resolved {vendor, model, thinking} triple from ROLE_* env.

    Every field is optional: an unset field falls back to the legacy per-role
    default (vendor -> claude, model/thinking -> the vendor's own default), so a
    binding is purely additive over today's behaviour.
    """

    vendor: str | None = None
    model: str | None = None
    thinking: str | None = None


def _parse_role_bindings(env: Mapping[str, str]) -> Mapping[str, RoleBinding]:
    """Collect ROLE_<NAME>_{VENDOR,MODEL,THINKING} into role -> RoleBinding.

    Generic on purpose (no hardcoded role list): future roles are configured
    with no code change. Blank values are ignored (treated as unset). An
    unrecognised vendor is fail-closed -> ConfigError, never a silent fallback.
    """
    collected: dict[str, dict[str, str]] = {}
    for key, raw in env.items():
        match = _ROLE_ENV_RE.match(key)
        if match is None:
            continue
        value = raw.strip()
        if not value:
            continue
        role = match.group(1).lower()
        knob = match.group(2).lower()  # vendor | model | thinking
        collected.setdefault(role, {})[knob] = value

    bindings: dict[str, RoleBinding] = {}
    for role, knobs in collected.items():
        vendor = knobs.get("vendor")
        if vendor is not None:
            vendor = vendor.lower()
            if vendor not in ROLE_VENDORS:
                raise ConfigError(
                    f"ROLE_{role.upper()}_VENDOR must be one of "
                    f"{', '.join(ROLE_VENDORS)} (got {knobs['vendor']!r})"
                )
        bindings[role] = RoleBinding(
            vendor=vendor, model=knobs.get("model"), thinking=knobs.get("thinking")
        )
    return MappingProxyType(bindings)


@dataclass(frozen=True)
class OrchestratorConfig:
    repo_url: str
    work_root: Path
    default_branch: str = "main"
    # Remote the bare <task> branches live on, for the LOCAL merge tool
    # (orchestrator.local_merge) - it is never read on the VPS: gitops.push
    # there hardcodes "origin" against the hub, so this knob is dead in that
    # topology. Locally it names the git remote (configured by the operator,
    # e.g. via `git remote add`) that points at the per-user bare hub
    # (repo_<project>/hub.git) the VPS pushes <task> branches to.
    # Defaults to "origin" because the common setup adds the hub as the
    # local checkout's origin; override with AGENT_BRANCH_REMOTE when the
    # hub is configured under a different remote name.
    branch_remote: str = "origin"
    max_loops: int = 4
    fast_commands: str = DEFAULT_FAST_COMMANDS
    # Bootstrap run ONCE per fresh worktree before the first fast_tests
    # (loop._ensure_setup), so DEFAULT_FAST_COMMANDS's `. .venv/bin/activate`
    # finds a venv. Empty = no bootstrap. See DEFAULT_SETUP_COMMANDS.
    setup_commands: str = DEFAULT_SETUP_COMMANDS
    claude_cmd: tuple[str, ...] = field(default_factory=tuple)
    codex_cmd: tuple[str, ...] = field(default_factory=tuple)
    rw2_cmd: tuple[str, ...] = field(default_factory=tuple)
    senior_cmd: tuple[str, ...] = field(default_factory=tuple)
    # LOCAL trusted-panel reviewers - least-privilege, never write/exec.
    review_claude_cmd: tuple[str, ...] = field(default_factory=tuple)
    review_codex_cmd: tuple[str, ...] = field(default_factory=tuple)
    review_senior_cmd: tuple[str, ...] = field(default_factory=tuple)
    # Per-role runner bindings from ROLE_<NAME>_{VENDOR,MODEL,THINKING}. Empty
    # by default -> every role resolves to its legacy Claude command; the
    # *_cmd knobs above remain the backward-compat fallbacks.
    role_bindings: Mapping[str, RoleBinding] = field(default_factory=dict)
    ntfy_topic: str | None = None
    # LADDY_ASK_REMOTE=1: interactive gate questions go through the file+ntfy
    # channel (orchestrator.remote_ask) instead of stdin - answerable from the
    # phone; the gate no longer dies with the SSH session.
    ask_remote: bool = False
    # --- Quota-window handling (spec: quota-resume-queue) --------------------
    quota_reset_buffer_s: int = 120
    quota_backoff_minutes: tuple[int, ...] = (15, 30, 60)
    quota_max_wait_hours: int = 30

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> OrchestratorConfig:
        from orchestrator.agents import (
            DEFAULT_CLAUDE_REVIEW_CMD,
            DEFAULT_CODEX_REVIEW_CMD,
            DEFAULT_SENIOR_REVIEW_CMD,
        )

        try:
            max_loops = int(env.get("MAX_LOOPS", "4"))
        except ValueError as exc:
            raise ConfigError(f"MAX_LOOPS must be an integer: {exc}") from exc
        if max_loops < 1:
            raise ConfigError("MAX_LOOPS must be >= 1")
        work_root = Path(env.get("AGENT_WORK_ROOT", str(Path.home() / "agent-work")))

        def _positive_int(name: str, default: str) -> int:
            try:
                value = int(env.get(name, default))
            except ValueError as exc:
                raise ConfigError(f"{name} must be an integer: {exc}") from exc
            if value < 1:
                raise ConfigError(f"{name} must be >= 1")
            return value

        raw_backoff = env.get("QUOTA_BACKOFF_MINUTES", "15,30,60")
        try:
            quota_backoff = tuple(int(p) for p in raw_backoff.split(",") if p.strip())
        except ValueError as exc:
            raise ConfigError(f"QUOTA_BACKOFF_MINUTES must be ints: {exc}") from exc
        if not quota_backoff or any(m < 1 for m in quota_backoff):
            raise ConfigError("QUOTA_BACKOFF_MINUTES must be >= 1 minute each")

        repo_url = env.get("AGENT_REPO_URL")
        if not repo_url:
            raise ConfigError(
                "AGENT_REPO_URL is required (the target hub, e.g. "
                "$HOME/repo_<project>/hub.git) - there is no default: a silent "
                "GitHub fallback is exactly what this topology forbids"
            )

        return cls(
            repo_url=repo_url,
            work_root=work_root,
            default_branch=env.get("DEFAULT_BRANCH", "main"),
            branch_remote=env.get("AGENT_BRANCH_REMOTE", "origin"),
            max_loops=max_loops,
            fast_commands=env.get("TEST_COMMANDS", DEFAULT_FAST_COMMANDS),
            setup_commands=env.get("SETUP_COMMANDS", DEFAULT_SETUP_COMMANDS),
            claude_cmd=_claude_cmd(env.get("CLAUDE_CMD")),
            codex_cmd=tuple(shlex.split(env["CODEX_CMD"])) if env.get("CODEX_CMD") else (),
            rw2_cmd=_claude_cmd(env.get("RW2_CMD")),
            senior_cmd=_claude_cmd(env.get("SENIOR_CMD")),
            review_claude_cmd=_review_cmd(
                env.get("REVIEW_CLAUDE_CMD"), DEFAULT_CLAUDE_REVIEW_CMD, claude=True
            ),
            review_codex_cmd=_review_cmd(
                env.get("REVIEW_CODEX_CMD"), DEFAULT_CODEX_REVIEW_CMD, claude=False
            ),
            review_senior_cmd=_review_cmd(
                env.get("REVIEW_SENIOR_CMD"), DEFAULT_SENIOR_REVIEW_CMD, claude=True
            ),
            role_bindings=_parse_role_bindings(env),
            ntfy_topic=env.get("NTFY_TOPIC") or None,
            ask_remote=env.get("LADDY_ASK_REMOTE") == "1",
            quota_reset_buffer_s=_positive_int("QUOTA_RESET_BUFFER_SECONDS", "120"),
            quota_backoff_minutes=quota_backoff,
            quota_max_wait_hours=_positive_int("QUOTA_MAX_WAIT_HOURS", "30"),
        )
