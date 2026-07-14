"""Tests for two-phase oracle prompt assembly (orchestrator.oracle.prompt)."""

from __future__ import annotations

import pytest

from orchestrator import ENGINE_DIR
from orchestrator.oracle.prompt import (
    PHASE_SPLIT,
    PROMPT_PATH,
    build_phase1_prompt,
    build_phase2_prompt,
)

# The template is an ENGINE resource: PROMPT_PATH points at the real,
# committed file under ENGINE_DIR - no fixture needed to read it.


def test_prompt_path_is_engine_resource() -> None:
    assert PROMPT_PATH == ENGINE_DIR / "prompts" / "oracle-task-review.md"


def test_template_exists_with_phase_marker() -> None:
    text = PROMPT_PATH.read_text(encoding="utf-8")
    assert PHASE_SPLIT in text


def test_template_placeholders_live_in_the_right_phase() -> None:
    # The instrument's shape: phase 1 carries spec+diff+worktree, phase 2
    # carries log+verdicts. A placeholder in the wrong phase = contamination
    # by design.
    text = PROMPT_PATH.read_text(encoding="utf-8")
    phase1, phase2 = text.split(PHASE_SPLIT, 1)
    for token in ("{task}", "{worktree}", "{class_slugs}", "{spec}", "{diff}"):
        assert token in phase1, f"{token} missing from phase 1"
    for token in ("{log}", "{verdicts}"):
        assert token not in phase1, f"{token} leaked into phase 1"
        assert token in phase2, f"{token} missing from phase 2"
    assert "{diff}" not in phase2


def test_build_phase1_fills_everything(tmp_path) -> None:
    out = build_phase1_prompt(
        task_id="t1", spec_text="# SPEC-BODY", diff_text="+DIFF-BODY",
        worktree=tmp_path / "wt", class_slugs=("edge-case", "regression"),
    )
    assert "t1" in out and "# SPEC-BODY" in out and "+DIFF-BODY" in out
    assert "edge-case, regression" in out
    assert str(tmp_path / "wt") in out
    for leftover in ("{task}", "{spec}", "{diff}", "{worktree}", "{class_slugs}"):
        assert leftover not in out
    assert PHASE_SPLIT not in out  # phase 2 never rides along


def test_build_phase2_fills_everything() -> None:
    out = build_phase2_prompt(
        task_id="t1", log_text='{"action":"go"}',
        verdicts_text='{"verdict":"APPROVED"}', class_slugs=("edge-case",),
    )
    assert '{"action":"go"}' in out and '{"verdict":"APPROVED"}' in out
    for leftover in ("{task}", "{log}", "{verdicts}", "{class_slugs}"):
        assert leftover not in out


def test_placeholder_literals_in_substituted_content_stay_literal(tmp_path) -> None:
    # A reviewed task's spec (or log) can quote the template's own
    # placeholders - realistic here, the template is committed in this
    # repo. Sequential replace over the accumulated output would expand a
    # literal '{diff}' inside the spec into the entire shipped diff,
    # garbling the prompt; substitution must be a single pass over the
    # TEMPLATE only.
    out = build_phase1_prompt(
        task_id="t1",
        spec_text="spec quoting the template: {diff} and {worktree}",
        diff_text="+DIFF-BODY",
        worktree=tmp_path / "wt", class_slugs=("edge-case",),
    )
    assert "spec quoting the template: {diff} and {worktree}" in out
    out2 = build_phase2_prompt(
        task_id="t1", log_text="log with literal {verdicts}",
        verdicts_text="VERDICTS-BODY", class_slugs=("edge-case",),
    )
    assert "log with literal {verdicts}" in out2


def test_missing_marker_raises(monkeypatch, tmp_path) -> None:
    bad = tmp_path / "oracle-task-review.md"
    bad.write_text("no marker here {task}\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr("orchestrator.oracle.prompt.PROMPT_PATH", bad)
    with pytest.raises(ValueError, match="PHASE-2"):
        build_phase1_prompt(
            task_id="t", spec_text="s", diff_text="d",
            worktree=tmp_path, class_slugs=(),
        )
