"""Tests for the config-driven role -> {vendor, model, thinking} resolver
(spec fullrun-s0). Exercises the REAL default resolver on `Deps`, never a fake:
the point of the slice is that pointing a role at another vendor/model/thinking
needs env only, no code change."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from orchestrator.agents import (
    DEFAULT_CLAUDE_CMD,
    DEFAULT_RW2_CMD,
    DEFAULT_SENIOR_CMD,
    AgentRunner,
    ClaudeRunner,
    CodexRunner,
)
from orchestrator.config import ConfigError, OrchestratorConfig
from orchestrator.run import Deps, _resolve_runner

_BASE_ENV = {"AGENT_REPO_URL": "file:///tmp/hub.git"}


def _cfg(**extra: str) -> OrchestratorConfig:
    return OrchestratorConfig.from_env({**_BASE_ENV, **extra})


def _cmd(runner: AgentRunner) -> tuple[str, ...]:
    # whitebox: both concrete runners stash the constructed command on
    # _base_cmd (not part of the AgentRunner protocol) -> read it dynamically.
    return tuple(getattr(runner, "_base_cmd"))  # noqa: B009


# --- AC1: defaults unchanged --------------------------------------------------


@pytest.mark.parametrize("role", ["developer", "rw1", "clarify"])
def test_default_roles_are_claude_with_default_cmd(role: str) -> None:
    runner = Deps().make_runner(_cfg(), role)
    assert isinstance(runner, ClaudeRunner)
    assert _cmd(runner) == DEFAULT_CLAUDE_CMD


def test_default_rw2_is_claude_sonnet() -> None:
    runner = Deps().make_runner(_cfg(), "rw2")
    assert isinstance(runner, ClaudeRunner)
    assert _cmd(runner) == DEFAULT_RW2_CMD
    assert "--model" in _cmd(runner) and "sonnet" in _cmd(runner)


def test_default_senior_is_claude_opus() -> None:
    runner = Deps().make_runner(_cfg(), "senior")
    assert isinstance(runner, ClaudeRunner)
    assert _cmd(runner) == DEFAULT_SENIOR_CMD
    assert "claude-opus-4-8" in _cmd(runner)


def test_legacy_cmd_env_overrides_still_flow_through() -> None:
    cfg = _cfg(
        CLAUDE_CMD="claude -p --model haiku",
        RW2_CMD="claude -p --model opus",
        SENIOR_CMD="claude -p --model claude-opus-4-8",
    )
    # CLAUDE_CMD drives developer/rw1/clarify; JSON output is force-appended.
    dev = _cmd(Deps().make_runner(cfg, "developer"))
    assert "haiku" in dev and "--output-format" in dev and "json" in dev
    assert "opus" in _cmd(Deps().make_runner(cfg, "rw2"))
    assert "claude-opus-4-8" in _cmd(Deps().make_runner(cfg, "senior"))


# --- AC2: vendor swap with no code change ------------------------------------


def test_rw2_vendor_codex_yields_codex_runner() -> None:
    runner = Deps().make_runner(_cfg(ROLE_RW2_VENDOR="codex"), "rw2")
    assert isinstance(runner, CodexRunner)
    assert runner.name == "codex"


def test_only_named_role_is_switched_others_stay_claude() -> None:
    cfg = _cfg(ROLE_RW2_VENDOR="codex")
    assert isinstance(Deps().make_runner(cfg, "rw2"), CodexRunner)
    for role in ("developer", "rw1", "clarify", "senior"):
        assert isinstance(Deps().make_runner(cfg, role), ClaudeRunner), role


def test_vendor_value_is_case_insensitive() -> None:
    runner = Deps().make_runner(_cfg(ROLE_RW2_VENDOR="Codex"), "rw2")
    assert isinstance(runner, CodexRunner)


# --- AC3: model + thinking threaded into the command -------------------------


def test_claude_model_override_appends_model_flag() -> None:
    cmd = _cmd(_resolve_runner(_cfg(ROLE_DEVELOPER_MODEL="opus"), "developer"))
    assert "--model" in cmd and "opus" in cmd
    # default developer command had no --model, so it is a single appended pair
    assert cmd.count("--model") == 1


def test_claude_model_override_replaces_existing_model() -> None:
    # rw2's Claude default carries `--model sonnet`; the override must REPLACE
    # it (one --model, last-wins is CLI-dependent), not append a duplicate.
    cmd = _cmd(_resolve_runner(_cfg(ROLE_RW2_MODEL="opus"), "rw2"))
    assert cmd.count("--model") == 1
    assert "opus" in cmd and "sonnet" not in cmd


def test_codex_model_and_thinking_threaded() -> None:
    cfg = _cfg(
        ROLE_RW2_VENDOR="codex", ROLE_RW2_MODEL="gpt-5", ROLE_RW2_THINKING="high"
    )
    cmd = _cmd(_resolve_runner(cfg, "rw2"))
    assert "--model" in cmd and "gpt-5" in cmd
    # codex reasoning effort rides the `-c key=value` global override, kept
    # ahead of any positional (codex takes the prompt on stdin).
    idx = cmd.index("-c")
    assert cmd[idx + 1] == "model_reasoning_effort=high"


def test_claude_thinking_is_documented_noop() -> None:
    # `claude -p` exposes no headless reasoning flag: THINKING must neither
    # raise nor corrupt the command -> byte-for-byte the default.
    runner = _resolve_runner(_cfg(ROLE_DEVELOPER_THINKING="high"), "developer")
    assert isinstance(runner, ClaudeRunner)
    assert _cmd(runner) == DEFAULT_CLAUDE_CMD


def test_model_without_vendor_defaults_to_claude() -> None:
    # a role that sets only MODEL (no VENDOR) stays on the default vendor.
    runner = _resolve_runner(_cfg(ROLE_RW1_MODEL="haiku"), "rw1")
    assert isinstance(runner, ClaudeRunner)
    assert "haiku" in _cmd(runner)


# --- AC4: uniform resolver, no per-role special-casing left ------------------


def test_deps_has_single_role_keyed_resolver() -> None:
    deps = Deps()
    assert deps.make_runner is _resolve_runner
    # the old per-role factories are gone
    assert not hasattr(deps, "make_rw2_runner")
    assert not hasattr(deps, "make_senior_runner")


def test_every_role_resolves_through_the_same_function() -> None:
    # unknown / future roles need no code change: they resolve to the default
    # claude command via the exact same path, proving the resolver is generic.
    for role in ("developer", "rw1", "rw2", "senior", "clarify", "rw3"):
        assert isinstance(_resolve_runner(_cfg(), role), (ClaudeRunner, CodexRunner))
    assert _cmd(_resolve_runner(_cfg(), "rw3")) == DEFAULT_CLAUDE_CMD


# --- AC5 / fail-closed: invalid vendor ---------------------------------------


def test_invalid_vendor_is_config_error_not_silent_fallback() -> None:
    with pytest.raises(ConfigError, match="ROLE_RW2_VENDOR"):
        _cfg(ROLE_RW2_VENDOR="gemini")


def test_role_bindings_default_empty_and_immutable() -> None:
    cfg = _cfg()
    assert cfg.role_bindings == {}
    assert isinstance(cfg.role_bindings, Mapping)
    with pytest.raises(TypeError):  # MappingProxyType is read-only
        cfg.role_bindings["x"] = object()  # pyright: ignore[reportIndexIssue]
