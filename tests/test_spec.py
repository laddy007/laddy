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
