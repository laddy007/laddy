"""Tests for the reviewer verdict schema + validator (design S9, App. E rule 3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.agent_retry import request_verdict
from orchestrator.verdict import (
    _ERROR_TEXT_MAX,
    Verdict,
    VerdictError,
    extract_json,
    parse_verdict,
    validate_review,
)
from tests.fakes import FakeRunner, advisory, blocker, verdict_json


def _errored(text: str, *, exit_reason: str = "error", rc: int = 1) -> AgentResult:
    return AgentResult(
        text=text, session_id=None, exit_reason=exit_reason, returncode=rc
    )


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


# --- agent-error-visibility: a failed run reports what the agent said --------

_AUTH_ERR = "Failed to authenticate: OAuth session expired and could not be refreshed"


def test_failed_run_error_carries_the_agents_own_words(tmp_path: Path) -> None:
    # AC1: the sentence the agent gave survives into the VerdictError, so a
    # human can tell an expired login from a rejected --model flag.
    runner = FakeRunner([_errored(_AUTH_ERR) for _ in range(3)])
    with pytest.raises(VerdictError) as exc:
        request_verdict(runner, "review", tmp_path)
    assert _AUTH_ERR in str(exc.value)
    assert "exit_reason='error'" in str(exc.value)
    assert "rc=1" in str(exc.value)


def test_failed_run_error_snippet_is_bounded(tmp_path: Path) -> None:
    # AC3: a runaway blob is clipped to the named limit, not eyeballed.
    runner = FakeRunner([_errored("x" * 10_000) for _ in range(3)])
    with pytest.raises(VerdictError) as exc:
        request_verdict(runner, "review", tmp_path)
    # the longest run of x's in the message is the bounded snippet
    longest_x = max(len(run) for run in str(exc.value).split(" ") if set(run) == {"x"})
    assert longest_x == _ERROR_TEXT_MAX


def test_failed_run_error_collapses_whitespace(tmp_path: Path) -> None:
    # AC4: multi-line agent text must not inject raw newlines/tabs into the
    # digest - it stays one readable item.
    runner = FakeRunner([_errored("line1\n\n  line2\tline3") for _ in range(3)])
    with pytest.raises(VerdictError) as exc:
        request_verdict(runner, "review", tmp_path)
    msg = str(exc.value)
    assert "\n" not in msg
    assert "\t" not in msg
    assert "line1 line2 line3" in msg


def test_failed_run_with_empty_text_reads_cleanly(tmp_path: Path) -> None:
    # AC5: a silently-failed run still names exit_reason and rc, with no
    # dangling separator - asserted on the exact tail, not merely "no crash".
    runner = FakeRunner([_errored("") for _ in range(3)])
    with pytest.raises(VerdictError) as exc:
        request_verdict(runner, "review", tmp_path)
    assert str(exc.value).endswith(
        "(exit_reason='error', rc=1); its output is not trustworthy"
    )


def test_failed_run_whitespace_only_text_reads_cleanly(tmp_path: Path) -> None:
    # AC5 boundary: whitespace-only collapses to empty, same clean tail.
    runner = FakeRunner([_errored("   \n\t  ") for _ in range(3)])
    with pytest.raises(VerdictError) as exc:
        request_verdict(runner, "review", tmp_path)
    assert str(exc.value).endswith(
        "(exit_reason='error', rc=1); its output is not trustworthy"
    )


def test_failed_run_with_valid_verdict_json_is_never_parsed(tmp_path: Path) -> None:
    # AC6: a non-"ok" run whose text is a schema-valid verdict still abstains -
    # it consumes its retries and never becomes a verdict, and its content
    # appears only quoted (bounded) in the error, never trusted.
    runner = FakeRunner([_errored(verdict_json("APPROVED")) for _ in range(3)])
    with pytest.raises(VerdictError, match="after 2 retries"):
        request_verdict(runner, "review", tmp_path)
    assert len(runner.calls) == 3


def test_failed_run_quota_path_carries_text_too(tmp_path: Path) -> None:
    # AC7: a "quota" exit behaves as before apart from the added text; the
    # snippet rides along and exit_reason is named.
    runner = FakeRunner(
        [_errored("rate limit hit", exit_reason="quota", rc=1) for _ in range(3)]
    )
    with pytest.raises(VerdictError) as exc:
        request_verdict(runner, "review", tmp_path)
    assert "exit_reason='quota'" in str(exc.value)
    assert "rate limit hit" in str(exc.value)
