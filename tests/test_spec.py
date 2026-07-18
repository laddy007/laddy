"""Tests for spec front-matter parsing + role composition."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.spec import SpecError, parse_spec


def _spec(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "spec.md"
    path.write_text(content, encoding="utf-8")
    return path


def test_no_front_matter_defaults_to_feature(tmp_path: Path) -> None:
    spec = parse_spec(_spec(tmp_path, "# Task\n\nDo X.\n"))
    assert spec.task_type == "feature"
    assert spec.roles == ("developer", "rw1", "rw2")
    assert spec.report_only is False
    assert spec.is_draft is False


def test_type_bug_composition(tmp_path: Path) -> None:
    spec = parse_spec(_spec(tmp_path, "---\ntype: bug\n---\n# Fix\n"))
    assert spec.roles == ("explorer", "developer", "debugger", "rw1", "rw2")


def test_type_audit_is_report_only(tmp_path: Path) -> None:
    spec = parse_spec(_spec(tmp_path, "---\ntype: audit\n---\n# Audit\n"))
    assert spec.report_only is True
    assert spec.roles == ("investigator", "verify")


def test_explicit_roles_override_type_table(tmp_path: Path) -> None:
    spec = parse_spec(
        _spec(tmp_path, "---\ntype: feature\nroles: [developer, rw1]\n---\n# X\n")
    )
    assert spec.roles == ("developer", "rw1")


def test_unknown_type_raises(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="unknown task type"):
        parse_spec(_spec(tmp_path, "---\ntype: megafeature\n---\n"))


def test_draft_proposal_detected(tmp_path: Path) -> None:
    spec = parse_spec(
        _spec(tmp_path, "---\ntype: feature\nstatus: draft-proposal\n---\n# X\n")
    )
    assert spec.is_draft is True


def test_unclosed_front_matter_raises(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="not closed"):
        parse_spec(_spec(tmp_path, "---\ntype: bug\n# no closing fence\n"))


def test_bad_roles_syntax_raises(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="inline list"):
        parse_spec(_spec(tmp_path, "---\nroles: developer, rw1\n---\n"))


def test_leading_bom_before_fence_raises(tmp_path: Path) -> None:
    # M-D4-1: a UTF-8 BOM defeats the opening fence; the old parser dropped to
    # type=feature (executable, non-draft). Fail closed instead.
    with pytest.raises(SpecError, match="BOM"):
        parse_spec(_spec(tmp_path, "\ufeff---\ntype: audit\n---\n"))


def test_leading_blank_line_before_fence_raises(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="first line"):
        parse_spec(_spec(tmp_path, "\n---\ntype: audit\n---\n"))


def test_leading_whitespace_line_before_fence_raises(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="first line"):
        parse_spec(_spec(tmp_path, "   \n---\ntype: audit\n---\n"))


def test_duplicate_front_matter_key_raises(tmp_path: Path) -> None:
    # L-D4-2: front matter is not YAML; last-wins was a spoofing surface.
    with pytest.raises(SpecError, match="duplicate front matter key"):
        parse_spec(_spec(tmp_path, "---\ntype: audit\ntype: feature\n---\n"))


def test_crlf_front_matter_still_parses(tmp_path: Path) -> None:
    # CRLF must keep working (splitlines handles it); guards the BOM/blank fix.
    spec = parse_spec(_spec(tmp_path, "---\r\ntype: audit\r\n---\r\n"))
    assert spec.task_type == "audit"
    assert spec.report_only is True


def test_plain_markdown_thematic_break_is_not_front_matter(tmp_path: Path) -> None:
    # A genuinely front-matter-less file with a later '---' rule stays valid.
    spec = parse_spec(_spec(tmp_path, "# Task\n\nDo X.\n\n---\n\nmore\n"))
    assert spec.task_type == "feature"


def test_role_plan_shape(tmp_path: Path) -> None:
    spec = parse_spec(_spec(tmp_path, "---\ntype: spike\n---\n"))
    assert spec.role_plan("t9") == {
        "task": "t9",
        "type": "spike",
        "roles": ["explorer", "developer", "rw1"],
    }


def test_status_done_sets_is_done(tmp_path: Path) -> None:
    p = tmp_path / "t.md"
    p.write_text("---\nstatus: done\n---\n# t\n", encoding="utf-8")
    assert parse_spec(p).is_done is True


def test_no_status_is_not_done(tmp_path: Path) -> None:
    p = tmp_path / "t.md"
    p.write_text("# t\n", encoding="utf-8")
    assert parse_spec(p).is_done is False


def test_parse_spec_reads_risk_field(tmp_path: Path) -> None:
    p = tmp_path / "t.md"
    p.write_text("---\ntype: feature\nrisk: high\n---\n# t\n", encoding="utf-8")
    assert parse_spec(p).risk == "high"


def test_parse_spec_risk_absent_is_none(tmp_path: Path) -> None:
    p = tmp_path / "t.md"
    p.write_text("---\ntype: feature\n---\n# t\n", encoding="utf-8")
    assert parse_spec(p).risk is None
