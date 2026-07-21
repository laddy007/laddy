"""Tests for typed env config."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config import (
    DEFAULT_FAST_COMMANDS,
    DEFAULT_SETUP_COMMANDS,
    ConfigError,
    OrchestratorConfig,
)


def test_repo_url_is_required() -> None:
    with pytest.raises(ConfigError, match="AGENT_REPO_URL"):
        OrchestratorConfig.from_env({})


def test_defaults() -> None:
    config = OrchestratorConfig.from_env({"AGENT_REPO_URL": "file:///tmp/hub.git"})
    assert config.max_loops == 4
    assert config.default_branch == "main"
    assert config.fast_commands == DEFAULT_FAST_COMMANDS
    assert config.setup_commands == DEFAULT_SETUP_COMMANDS
    assert config.repo_url == "file:///tmp/hub.git"
    assert config.claude_cmd == ()
    assert config.ntfy_topic is None


def test_env_overrides() -> None:
    config = OrchestratorConfig.from_env(
        {
            "AGENT_REPO_URL": "file:///tmp/repo.git",
            "AGENT_WORK_ROOT": "/srv/agent",
            "MAX_LOOPS": "2",
            "TEST_COMMANDS": "pytest -x",
            "SETUP_COMMANDS": "make bootstrap",
            "CLAUDE_CMD": "claude -p --output-format json",
            "DEFAULT_BRANCH": "trunk",
            "NTFY_TOPIC": "myapp-agent",
        }
    )
    assert config.repo_url == "file:///tmp/repo.git"
    assert config.work_root == Path("/srv/agent")
    assert config.max_loops == 2
    assert config.fast_commands == "pytest -x"
    assert config.setup_commands == "make bootstrap"
    assert config.claude_cmd == ("claude", "-p", "--output-format", "json")
    assert config.default_branch == "trunk"
    assert config.ntfy_topic == "myapp-agent"


def test_claude_cmd_enforces_json_output_when_omitted() -> None:
    # the loop depends on --output-format json; a user override that omits it
    # must not silently disable session resume / error detection
    config = OrchestratorConfig.from_env(
        {
            "AGENT_REPO_URL": "file:///tmp/hub.git",
            "CLAUDE_CMD": "claude -p --dangerously-skip-permissions",
        }
    )
    assert config.claude_cmd == (
        "claude",
        "-p",
        "--dangerously-skip-permissions",
        "--output-format",
        "json",
    )


def test_claude_cmd_preserves_explicit_output_format() -> None:
    config = OrchestratorConfig.from_env(
        {
            "AGENT_REPO_URL": "file:///tmp/hub.git",
            "CLAUDE_CMD": "claude -p --output-format json",
        }
    )
    assert config.claude_cmd.count("--output-format") == 1


def test_senior_cmd_also_json_enforced() -> None:
    config = OrchestratorConfig.from_env(
        {"AGENT_REPO_URL": "file:///tmp/hub.git", "SENIOR_CMD": "claude -p --model x"}
    )
    assert "--output-format" in config.senior_cmd and "json" in config.senior_cmd


def test_rw2_defaults_to_claude_sonnet_and_env_overrides() -> None:
    # rw2 now runs Claude (Sonnet), not Codex; the loop factory falls back to
    # DEFAULT_RW2_CMD when RW2_CMD is unset, and RW2_CMD overrides it (JSON
    # output enforced like any claude command).
    from orchestrator.run import Deps

    cfg = OrchestratorConfig.from_env({"AGENT_REPO_URL": "file:///tmp/hub.git"})
    assert cfg.rw2_cmd == ()  # empty -> resolver uses the default
    rw2 = Deps().make_runner(cfg, "rw2")
    assert rw2.name == "claude"

    cfg2 = OrchestratorConfig.from_env(
        {"AGENT_REPO_URL": "file:///tmp/hub.git", "RW2_CMD": "claude -p --model opus"}
    )
    assert "--model" in cfg2.rw2_cmd and "opus" in cfg2.rw2_cmd
    assert "--output-format" in cfg2.rw2_cmd and "json" in cfg2.rw2_cmd


def test_local_review_cmds_are_least_privilege_by_default() -> None:
    # the LOCAL trusted panel reviews untrusted branch code; its reviewers must
    # not carry write/exec grants (skip-permissions / full-auto), or a
    # prompt-injection in the branch could weaponize the reviewer on the
    # Director's machine (trust-model S4/S10).
    config = OrchestratorConfig.from_env({"AGENT_REPO_URL": "file:///tmp/hub.git"})
    for cmd in (config.review_claude_cmd, config.review_codex_cmd, config.review_senior_cmd):
        assert "--dangerously-skip-permissions" not in cmd
        assert "--full-auto" not in cmd
    # and the convergence commands (VPS box, nothing to protect) still may
    assert config.review_claude_cmd  # non-empty defaults exist


def test_review_cmd_env_override_still_drops_dangerous_flags() -> None:
    # even if the operator pastes a dangerous review override, config enforces
    # the least-privilege contract for the local panel
    config = OrchestratorConfig.from_env(
        {
            "AGENT_REPO_URL": "file:///tmp/hub.git",
            "REVIEW_CLAUDE_CMD": "claude -p --dangerously-skip-permissions",
            "REVIEW_CODEX_CMD": "codex exec --full-auto",
        }
    )
    assert "--dangerously-skip-permissions" not in config.review_claude_cmd
    assert "--full-auto" not in config.review_codex_cmd


# --- role bindings (spec fullrun-s0) -----------------------------------------


def test_role_bindings_empty_by_default() -> None:
    cfg = OrchestratorConfig.from_env({"AGENT_REPO_URL": "file:///tmp/hub.git"})
    assert dict(cfg.role_bindings) == {}


def test_role_bindings_parsed_generically() -> None:
    # role name is lowercased; a role never named in code (rw3) still parses.
    cfg = OrchestratorConfig.from_env(
        {
            "AGENT_REPO_URL": "file:///tmp/hub.git",
            "ROLE_RW3_VENDOR": "codex",
            "ROLE_RW3_MODEL": "gpt-5",
            "ROLE_RW3_THINKING": "high",
        }
    )
    b = cfg.role_bindings["rw3"]
    assert (b.vendor, b.model, b.thinking) == ("codex", "gpt-5", "high")


def test_role_binding_partial_fields_are_none() -> None:
    cfg = OrchestratorConfig.from_env(
        {"AGENT_REPO_URL": "file:///tmp/hub.git", "ROLE_DEVELOPER_MODEL": "opus"}
    )
    b = cfg.role_bindings["developer"]
    assert b.model == "opus"
    assert b.vendor is None and b.thinking is None


def test_role_binding_blank_value_is_ignored() -> None:
    # a blank/whitespace env value is treated as unset, not an empty override.
    cfg = OrchestratorConfig.from_env(
        {
            "AGENT_REPO_URL": "file:///tmp/hub.git",
            "ROLE_RW2_VENDOR": "   ",
            "ROLE_RW2_MODEL": "opus",
        }
    )
    b = cfg.role_bindings["rw2"]
    assert b.vendor is None and b.model == "opus"


def test_unrelated_env_does_not_create_bindings() -> None:
    # near-misses must not be swallowed as bindings (no ROLE_ prefix, or an
    # unknown trailing knob).
    cfg = OrchestratorConfig.from_env(
        {
            "AGENT_REPO_URL": "file:///tmp/hub.git",
            "ROLE_RW2_FOO": "bar",  # unknown knob
            "MY_ROLE_RW2_VENDOR": "codex",  # not ROLE_-anchored
        }
    )
    assert dict(cfg.role_bindings) == {}


def test_role_binding_invalid_vendor_raises() -> None:
    with pytest.raises(ConfigError, match="ROLE_RW2_VENDOR"):
        OrchestratorConfig.from_env(
            {"AGENT_REPO_URL": "file:///tmp/hub.git", "ROLE_RW2_VENDOR": "gemini"}
        )


def test_bad_max_loops_raises() -> None:
    with pytest.raises(ConfigError, match="MAX_LOOPS"):
        OrchestratorConfig.from_env(
            {"AGENT_REPO_URL": "file:///tmp/hub.git", "MAX_LOOPS": "many"}
        )
    with pytest.raises(ConfigError, match="MAX_LOOPS"):
        OrchestratorConfig.from_env(
            {"AGENT_REPO_URL": "file:///tmp/hub.git", "MAX_LOOPS": "0"}
        )


def test_quota_knobs_defaults() -> None:
    cfg = OrchestratorConfig.from_env({"AGENT_REPO_URL": "file:///tmp/hub.git"})
    assert cfg.quota_reset_buffer_s == 120
    assert cfg.quota_backoff_minutes == (15, 30, 60)
    assert cfg.quota_max_wait_hours == 30


def test_quota_knobs_from_env() -> None:
    cfg = OrchestratorConfig.from_env(
        {
            "AGENT_REPO_URL": "file:///tmp/hub.git",
            "QUOTA_RESET_BUFFER_SECONDS": "60",
            "QUOTA_BACKOFF_MINUTES": "5,10",
            "QUOTA_MAX_WAIT_HOURS": "8",
        }
    )
    assert cfg.quota_reset_buffer_s == 60
    assert cfg.quota_backoff_minutes == (5, 10)
    assert cfg.quota_max_wait_hours == 8


def test_quota_knobs_invalid_raise_config_error() -> None:
    with pytest.raises(ConfigError):
        OrchestratorConfig.from_env(
            {"AGENT_REPO_URL": "file:///tmp/hub.git", "QUOTA_BACKOFF_MINUTES": "abc"}
        )
    with pytest.raises(ConfigError):
        OrchestratorConfig.from_env(
            {"AGENT_REPO_URL": "file:///tmp/hub.git", "QUOTA_MAX_WAIT_HOURS": "0"}
        )
