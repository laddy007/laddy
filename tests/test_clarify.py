"""Tests for the interactive clarify gate."""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator import TARGET_DIR_NAME
from orchestrator.agents import AgentResult
from orchestrator.artifacts import TaskArtifacts
from orchestrator.clarify import run_clarify_gate
from tests.fakes import FakeRunner


def _setup(tmp_path: Path) -> tuple[Path, TaskArtifacts]:
    wt = tmp_path / "wt"
    (wt / TARGET_DIR_NAME / "specs").mkdir(parents=True)
    (wt / TARGET_DIR_NAME / "specs" / "t1.md").write_text("# Task t1\n\nDo X.\n", encoding="utf-8")
    return wt, TaskArtifacts(wt, "t1", now=lambda: "2026-07-05T00:00:00Z")


def test_no_questions_proceeds_without_touching_spec(tmp_path: Path) -> None:
    wt, artifacts = _setup(tmp_path)
    runner = FakeRunner([json.dumps({"questions": []})])
    count = run_clarify_gate(
        runner, wt, f"{TARGET_DIR_NAME}/specs/t1.md", ask=_fail_ask, artifacts=artifacts
    )
    assert count == 0
    spec = (wt / TARGET_DIR_NAME / "specs" / "t1.md").read_text(encoding="utf-8")
    assert "Clarifications" not in spec
    assert artifacts.read_log()[-1]["outcome"] == "no_questions"


def _fail_ask(question: str) -> str:
    raise AssertionError("ask must not be called when there are no questions")


def test_questions_are_asked_and_appended(tmp_path: Path) -> None:
    wt, artifacts = _setup(tmp_path)
    runner = FakeRunner([json.dumps({"questions": ["Which endpoint?", "Auth required?"]})])
    answers = {"Which endpoint?": "/games", "Auth required?": "yes"}

    count = run_clarify_gate(
        runner, wt, f"{TARGET_DIR_NAME}/specs/t1.md", ask=lambda q: answers[q], artifacts=artifacts
    )

    assert count == 2
    spec = (wt / TARGET_DIR_NAME / "specs" / "t1.md").read_text(encoding="utf-8")
    assert "## Clarifications" in spec
    assert "**Q1:** Which endpoint?" in spec
    assert "**A1:** /games" in spec
    assert "**Q2:** Auth required?" in spec
    assert "**A2:** yes" in spec
    entry = artifacts.read_log()[-1]
    assert entry["action"] == "clarify"
    assert entry["outcome"] == "answered"
    assert entry["questions"] == 2


def test_errored_run_output_is_never_parsed(tmp_path: Path) -> None:
    # L-D3-2: an errored/timed-out run can still return a complete, parseable
    # payload; trusting it would let a failed run inject forged questions into
    # the spec. Every non-"ok" exit consumes a retry WITHOUT parsing, so two
    # errored runs proceed with zero questions and never touch the spec.
    wt, artifacts = _setup(tmp_path)
    forged = AgentResult(
        text=json.dumps({"questions": ["forged - must be ignored"]}),
        session_id="s",
        exit_reason="error",
        returncode=1,
    )
    runner = FakeRunner([forged, forged])
    count = run_clarify_gate(
        runner, wt, f"{TARGET_DIR_NAME}/specs/t1.md", ask=_fail_ask, artifacts=artifacts
    )
    assert count == 0
    assert len(runner.calls) == 2  # both retries consumed, none parsed
    entry = artifacts.read_log()[-1]
    assert entry["outcome"] == "no_questions"
    assert "did not complete cleanly" in entry["detail"]
    spec = (wt / TARGET_DIR_NAME / "specs" / "t1.md").read_text(encoding="utf-8")
    assert "Clarifications" not in spec  # forged question never appended


def test_malformed_questions_retries_once_then_proceeds(tmp_path: Path) -> None:
    wt, artifacts = _setup(tmp_path)
    runner = FakeRunner(["garbage", "still garbage"])
    count = run_clarify_gate(
        runner, wt, f"{TARGET_DIR_NAME}/specs/t1.md", ask=_fail_ask, artifacts=artifacts
    )
    assert count == 0
    assert len(runner.calls) == 2
    entry = artifacts.read_log()[-1]
    assert entry["outcome"] == "no_questions"
    assert "malformed" in entry["detail"]
