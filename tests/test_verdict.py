"""Tests for the reviewer verdict schema + validator (design S9, App. E rule 3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.agent_retry import request_verdict
from orchestrator.verdict import (
    Verdict,
    VerdictError,
    extract_json,
    parse_verdict,
    validate_review,
)
from tests.fakes import FakeRunner, advisory, blocker, verdict_json


def test_parse_valid_verdict_roundtrip() -> None:
    v = parse_verdict(verdict_json("CHANGES_REQUESTED", [blocker()], risk="medium"))
    assert isinstance(v, Verdict)
    assert v.verdict == "CHANGES_REQUESTED"
    assert not v.approved
    assert v.risk_level == "medium"
    assert v.files_reviewed == ("a.py",)
    assert v.claims_verified[0].claim == "c"
    assert v.claims_verified[0].verified is True
    assert v.findings[0].severity == "blocker"
    assert v.blockers == v.findings
    assert v.test_assessment == "ok"
    assert v.residual_risks == ()


def test_verdict_accepts_json_inside_code_fence() -> None:
    wrapped = "Here is my verdict:\n```json\n" + verdict_json() + "\n```\nDone."
    v = parse_verdict(wrapped)
    assert v.approved


def test_extract_json_without_fence_takes_outer_object() -> None:
    text = "prefix " + verdict_json() + " suffix"
    assert json.loads(extract_json(text))["verdict"] == "APPROVED"


def test_extract_json_no_object_raises() -> None:
    with pytest.raises(VerdictError, match="no JSON object"):
        extract_json("no json here")


def test_missing_key_raises_with_key_name() -> None:
    payload = json.loads(verdict_json())
    del payload["test_assessment"]
    with pytest.raises(VerdictError, match="test_assessment"):
        parse_verdict(json.dumps(payload))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("verdict", "MAYBE"),
        ("risk_level", "extreme"),
    ],
)
def test_bad_top_level_enum_raises(key: str, value: str) -> None:
    payload = json.loads(verdict_json())
    payload[key] = value
    with pytest.raises(VerdictError, match=key):
        parse_verdict(json.dumps(payload))


def test_bad_finding_enums_raise() -> None:
    bad_sev = blocker()
    bad_sev["severity"] = "fatal"
    with pytest.raises(VerdictError, match="severity"):
        parse_verdict(verdict_json("CHANGES_REQUESTED", [bad_sev]))

    bad_cat = blocker()
    bad_cat["category"] = "style"
    with pytest.raises(VerdictError, match="category"):
        parse_verdict(verdict_json("CHANGES_REQUESTED", [bad_cat]))


def test_advisory_with_failure_scenario_rejected() -> None:
    finding = advisory()
    finding["failure_scenario"] = "concurrent update loses a row"
    with pytest.raises(VerdictError, match="advisory"):
        parse_verdict(verdict_json("APPROVED", [finding]))


def test_blocker_with_empty_failure_scenario_rejected() -> None:
    finding = blocker(failure_scenario="")
    with pytest.raises(VerdictError, match="blocker"):
        parse_verdict(verdict_json("CHANGES_REQUESTED", [finding]))


def test_approved_with_blocker_finding_rejected_by_review_validator() -> None:
    # parse accepts it (report-only verify rounds need this shape) ...
    verdict = parse_verdict(verdict_json("APPROVED", [blocker()]))
    # ... but the code-review validator (default in request_verdict) rejects it
    with pytest.raises(VerdictError, match="APPROVED"):
        validate_review(verdict)


def test_changes_requested_without_blockers_rejected_by_review_validator() -> None:
    # CHANGES_REQUESTED with nothing binding to address is contradictory: the
    # rework round would have an empty verdict section and no fingerprint,
    # looping to CAP_REACHED. The validator forces the reviewer to either
    # approve or name a real blocker.
    for findings in ([], [advisory()]):
        verdict = parse_verdict(verdict_json("CHANGES_REQUESTED", findings))
        with pytest.raises(VerdictError, match="CHANGES_REQUESTED"):
            validate_review(verdict)


def test_request_verdict_applies_review_validator_by_default(tmp_path: Path) -> None:
    runner = FakeRunner([verdict_json("APPROVED", [blocker()]), verdict_json("APPROVED")])
    verdict, _ = request_verdict(runner, "review", tmp_path)
    assert verdict.approved and not verdict.findings
    assert len(runner.calls) == 2  # first output rejected, retried


def test_wrong_type_raises() -> None:
    payload = json.loads(verdict_json())
    payload["files_reviewed"] = "a.py"
    with pytest.raises(VerdictError, match="files_reviewed"):
        parse_verdict(json.dumps(payload))


def test_request_verdict_retries_malformed_then_succeeds(tmp_path: Path) -> None:
    runner = FakeRunner(["not a verdict at all", verdict_json()])
    verdict, result = request_verdict(runner, "review this ORIGINAL", tmp_path)
    assert verdict.approved
    assert len(runner.calls) == 2
    assert runner.calls[0].prompt == "review this ORIGINAL"
    assert "rejected by the schema validator" in runner.calls[1].prompt
    # the FULL original prompt is re-sent on retry so a non-resumable runner
    # (codex) still has the review context, not just the bare error notice
    assert "review this ORIGINAL" in runner.calls[1].prompt
    # retry continues the same reviewer session (token optimization for claude)
    assert runner.calls[1].resume == "fake-s1"
    assert result.session_id == "fake-s2"


def test_request_verdict_gives_up_after_retries(tmp_path: Path) -> None:
    runner = FakeRunner(["junk", "junk", "junk"])
    with pytest.raises(VerdictError, match="after 2 retries"):
        request_verdict(runner, "review this", tmp_path, max_retries=2)
    assert len(runner.calls) == 3
