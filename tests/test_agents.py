"""Tests for the vendor-agnostic agent runner (Claude backend)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.agents import (
    DEFAULT_CLAUDE_CMD,
    DEFAULT_CODEX_CMD,
    AgentResult,
    ClaudeRunner,
    CodexRunner,
    detect_quota,
    set_model_flag,
)
from tests.fakes import FakeRunner


class RecordingExec:
    def __init__(self, rc: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[tuple[list[str], Path, str]] = []

    def __call__(self, cmd: Sequence[str], cwd: Path, stdin: str) -> tuple[int, str, str]:
        self.calls.append((list(cmd), cwd, stdin))
        return (self.rc, self.stdout, self.stderr)


def test_claude_runner_builds_command_and_parses_json(tmp_path: Path) -> None:
    exec_fn = RecordingExec(
        stdout=json.dumps({"result": "done", "session_id": "abc", "is_error": False})
    )
    runner = ClaudeRunner(exec_fn=exec_fn)
    result = runner.run("do the thing", tmp_path)

    cmd, cwd, stdin = exec_fn.calls[0]
    assert cmd == list(DEFAULT_CLAUDE_CMD)
    assert cwd == tmp_path
    assert stdin == "do the thing"
    assert result == AgentResult(text="done", session_id="abc", exit_reason="ok", returncode=0)


def test_claude_runner_resume_adds_flag(tmp_path: Path) -> None:
    exec_fn = RecordingExec(
        stdout=json.dumps({"result": "x", "session_id": "abc", "is_error": False})
    )
    runner = ClaudeRunner(exec_fn=exec_fn)
    runner.run("continue", tmp_path, resume="abc")

    cmd, _, _ = exec_fn.calls[0]
    assert cmd[: len(DEFAULT_CLAUDE_CMD)] == list(DEFAULT_CLAUDE_CMD)
    assert cmd[len(DEFAULT_CLAUDE_CMD) :] == ["--resume", "abc"]


def test_claude_runner_is_error_flag_maps_to_error(tmp_path: Path) -> None:
    exec_fn = RecordingExec(
        stdout=json.dumps({"result": "limit", "session_id": "abc", "is_error": True})
    )
    result = ClaudeRunner(exec_fn=exec_fn).run("p", tmp_path)
    assert result.exit_reason == "error"
    assert result.session_id == "abc"


def test_claude_runner_nonjson_output_is_preserved(tmp_path: Path) -> None:
    exec_fn = RecordingExec(rc=1, stdout="boom", stderr="trace")
    result = ClaudeRunner(exec_fn=exec_fn).run("p", tmp_path)
    assert result.exit_reason == "error"
    assert result.returncode == 1
    assert "boom" in result.text
    assert result.session_id is None


@pytest.mark.parametrize("payload", ["null", '"a bare string"', "[1, 2, 3]", "42"])
def test_claude_runner_valid_json_non_object_does_not_crash(
    tmp_path: Path, payload: str
) -> None:
    # valid JSON that is not an object must be treated as plain text,
    # never raise AttributeError and kill the detached loop
    exec_fn = RecordingExec(rc=0, stdout=payload)
    result = ClaudeRunner(exec_fn=exec_fn).run("p", tmp_path)
    assert result.session_id is None
    assert result.exit_reason == "ok"
    assert result.text == payload


def test_fake_runner_records_prompts(tmp_path: Path) -> None:
    fake = FakeRunner(["hello"])
    result = fake.run("prompt-1", tmp_path, resume=None)
    assert result.text == "hello"
    assert result.exit_reason == "ok"
    assert fake.calls[0].prompt == "prompt-1"
    assert fake.calls[0].resume is None


def test_codex_runner_passes_prompt_on_stdin(tmp_path: Path) -> None:
    exec_fn = RecordingExec(stdout="codex says done")
    runner = CodexRunner(exec_fn=exec_fn)
    result = runner.run("do it", tmp_path, resume="ignored-token")

    cmd, cwd, stdin = exec_fn.calls[0]
    assert cmd == list(DEFAULT_CODEX_CMD)
    assert stdin == "do it"
    # no session resume support: token accepted but ignored
    assert result.session_id is None
    assert result.text == "codex says done"
    assert result.exit_reason == "ok"


def test_codex_runner_nonzero_rc_is_error(tmp_path: Path) -> None:
    exec_fn = RecordingExec(rc=2, stdout="", stderr="rate limited")
    result = CodexRunner(exec_fn=exec_fn).run("p", tmp_path)
    assert result.exit_reason == "error"
    assert result.text == "rate limited"


def test_set_model_flag_appends_when_absent() -> None:
    assert set_model_flag(("claude", "-p"), "opus") == ("claude", "-p", "--model", "opus")


def test_set_model_flag_replaces_existing_value() -> None:
    # rw2's default carries `--model sonnet`; an override replaces the value in
    # place rather than appending a second (CLI-dependent last-wins) --model.
    out = set_model_flag(("claude", "-p", "--model", "sonnet"), "opus")
    assert out == ("claude", "-p", "--model", "opus")
    assert out.count("--model") == 1


def test_set_model_flag_empty_command_appends() -> None:
    assert set_model_flag((), "opus") == ("--model", "opus")


def test_set_model_flag_trailing_model_without_value_appends() -> None:
    # a malformed base command ending in a bare `--model` (no value to replace)
    # must not index past the end; it appends a fresh pair instead of raising.
    assert set_model_flag(("claude", "--model"), "opus") == (
        "claude", "--model", "--model", "opus",
    )


_NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("text", "expect_quota", "expect_reset"),
    [
        # claude -p subscription-limit format: message|epoch
        ("Claude AI usage limit reached|1752238800", True,
         datetime.fromtimestamp(1752238800, tz=timezone.utc)),
        ("You've hit your usage limit. Resets in 2 hours.", True,
         _NOW + timedelta(hours=2)),
        ("rate limit exceeded, resets in 30 minutes", True,
         _NOW + timedelta(minutes=30)),
        ("Rate limit hit. Resets at 2026-07-11 17:00", True,
         datetime(2026, 7, 11, 17, 0, tzinfo=timezone.utc)),
        # quota-ish but no parseable time -> quota with unknown reset
        ("429 Too Many Requests", True, None),
        ("usage limit reached, try later", True, None),
        ("HTTP 429 from api.anthropic.com", True, None),
        # NOT quota -> None (conservative default). These are the false
        # positives a bare `\b429\b` / bare `rate limit` would cause: a 429
        # that is a line number or a pass count, or an errored dev summary
        # that merely MENTIONS rate-limit code (this repo has rate-limit
        # tasks and one literally named quota-resume-queue). Misclassifying
        # any of them as quota parks the loop for up to QUOTA_MAX_WAIT_HOURS.
        ("File models.py, line 429", False, None),
        ("429 passed, 1 failed", False, None),
        ("added rate limiting middleware to the API router", False, None),
        # A failed dev summary that merely writes/tests rate-limit CODE: the
        # event word ("reset"/"retry"/"exhaustively"/"reach") only co-occurs
        # near "rate limit", it is not the limit being HIT -> must stay error.
        ("Test failed: rate limiting middleware did not reset counters between requests",
         False, None),
        ("added rate limiting; will retry the flaky CI job", False, None),
        ("rate limiting tests exhaustively cover edge cases", False, None),
        ("rate limit config review: agreed to reach out to infra team", False, None),
        # bare "reset" event dropped: a passing FAILED-agent string naming a
        # rate-limit reset endpoint/test must not falsely classify as quota.
        ("rate limit reset endpoint returns 200", False, None),
        ("SyntaxError: unexpected token", False, None),
        ("network unreachable", False, None),
        ("", False, None),
    ],
)
def test_detect_quota(text: str, expect_quota: bool, expect_reset) -> None:
    sig = detect_quota(text, _NOW)
    if not expect_quota:
        assert sig is None
    else:
        assert sig is not None
        assert sig.reset_at == expect_reset


@pytest.mark.parametrize(
    "text",
    [
        "usage limit reached; resets at 2026-99-99T00:00",  # regex-matching invalid date
        "usage limit reached; resets in 999999999 hours",  # OverflowError on timedelta
    ],
)
def test_detect_quota_malformed_reset_does_not_raise(text: str) -> None:
    # a garbled reset must NOT raise out of detect_quota and crash the detached
    # loop: still a quota signal, just with an unknown reset time.
    sig = detect_quota(text, _NOW)
    assert sig is not None
    assert sig.reset_at is None


def test_claude_runner_classifies_quota_error() -> None:
    def fake_exec(cmd, cwd, stdin):
        return 1, "", "Claude AI usage limit reached|1752238800"

    runner = ClaudeRunner(exec_fn=fake_exec, now_fn=lambda: _NOW)
    result = runner.run("p", Path("."))
    assert result.exit_reason == "quota"
    assert result.quota_reset_at == datetime.fromtimestamp(1752238800, tz=timezone.utc)


def test_claude_runner_json_error_payload_quota() -> None:
    payload = json.dumps(
        {"is_error": True, "result": "usage limit reached, resets in 1 hour", "session_id": "s1"}
    )

    def fake_exec(cmd, cwd, stdin):
        return 0, payload, ""

    runner = ClaudeRunner(exec_fn=fake_exec, now_fn=lambda: _NOW)
    result = runner.run("p", Path("."))
    assert result.exit_reason == "quota"
    assert result.quota_reset_at == _NOW + timedelta(hours=1)


def test_claude_runner_ordinary_error_stays_error() -> None:
    def fake_exec(cmd, cwd, stdin):
        return 1, "", "boom"

    result = ClaudeRunner(exec_fn=fake_exec, now_fn=lambda: _NOW).run("p", Path("."))
    assert result.exit_reason == "error"
    assert result.quota_reset_at is None


def test_codex_runner_classifies_quota() -> None:
    def fake_exec(cmd, cwd, stdin):
        return 1, "", "429 Too Many Requests"

    result = CodexRunner(exec_fn=fake_exec, now_fn=lambda: _NOW).run("p", Path("."))
    assert result.exit_reason == "quota"
    assert result.quota_reset_at is None
