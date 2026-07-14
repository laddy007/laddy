"""Regression guards: the prevention mandates baked into the loop's role and
spec-authoring prompts must not be silently dropped by a later edit. These
assert the REQUIRED phrasing is present, not that the wording is exact."""

from __future__ import annotations

from pathlib import Path

_LADDY = Path(__file__).resolve().parents[1]


def _role(name: str) -> str:
    return (_LADDY / "roles" / f"{name}.md").read_text(encoding="utf-8")


def _skill(name: str) -> str:
    return (_LADDY / "skills" / name / "SKILL.md").read_text(encoding="utf-8")


def test_developer_mandates_acceptance_criteria_and_failure_mode_tests() -> None:
    body = _role("developer").lower()
    assert "acceptance criteria are tests" in body
    assert "failure-mode test" in body
    assert "does nothing" in body  # the writes-nothing path is called out


def test_rw1_checks_acceptance_criteria_and_failure_mode_coverage() -> None:
    body = _role("rw1").lower()
    assert "acceptance-criteria coverage" in body
    assert "failure-mode coverage" in body


def test_rw2_enumerates_failure_mode_angles() -> None:
    body = _role("rw2").lower()
    assert "failure-mode angles" in body
    for angle in ("malformed", "offline", "mid-operation", "across modules"):
        assert angle in body, angle


def test_create_spec_requires_testable_acceptance_criteria() -> None:
    body = _skill("create-spec").lower()
    assert "## acceptance criteria" in body
    assert "every criterion is a single testable statement" in body


def test_explorer_covers_design_contract_angles() -> None:
    body = _role("explorer").lower()
    for phrase in ("enumerate", "side-effect", "across modules"):
        assert phrase in body, phrase


def test_developer_allows_orchestrator_edit_only_when_design_approved() -> None:
    body = _role("developer").lower()
    assert "design-approved" in body


def test_create_spec_auto_stamps_risk_high() -> None:
    body = _skill("create-spec").lower()
    assert "risk: high" in body
