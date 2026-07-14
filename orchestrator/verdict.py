"""Reviewer verdict schema + validation (design doc S9, Appendix E rule 3).

Hand-rolled typed validation instead of a jsonschema dependency: the
load-bearing rule (advisory findings must have an empty failure_scenario)
is a cross-field constraint, and the loop needs precise, promptable error
messages for the bounded retry.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from orchestrator.agents import AgentResult, AgentRunner

VERDICTS = ("APPROVED", "CHANGES_REQUESTED")
RISK_LEVELS = ("low", "medium", "high")
SEVERITIES = ("blocker", "advisory")
CATEGORIES = (
    "correctness",
    "invariant",
    "security",
    "migration",
    "test-adequacy",
    "quality",
)


class VerdictError(ValueError):
    """Reviewer output is not a schema-valid verdict."""


@dataclass(frozen=True)
class Finding:
    severity: str
    category: str
    file: str
    line: int
    summary: str
    failure_scenario: str


@dataclass(frozen=True)
class ClaimVerified:
    claim: str
    evidence: str
    verified: bool


@dataclass(frozen=True)
class Verdict:
    verdict: str
    risk_level: str
    files_reviewed: tuple[str, ...]
    claims_verified: tuple[ClaimVerified, ...]
    findings: tuple[Finding, ...]
    test_assessment: str
    residual_risks: tuple[str, ...]

    @property
    def approved(self) -> bool:
        return self.verdict == "APPROVED"

    @property
    def blockers(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "blocker")


def _first_json_object(text: str) -> str | None:
    """Return the first BALANCED top-level ``{...}`` object in ``text``.

    A brace-depth scan that ignores braces inside strings. This replaces the
    old non-greedy ``\\{.*?\\}`` fence regex, which stopped at the FIRST ``}``
    and so truncated any verdict whose findings/claims arrays contain objects
    (i.e. every non-trivial review) into invalid JSON. Fence-agnostic: the
    verdict is the first complete object whether or not it is ```json-fenced.
    """
    depth = 0
    start = -1
    in_str = False
    escaped = False
    for i, ch in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    return text[start : i + 1]
    return None


def extract_json(text: str) -> str:
    """Pull the verdict JSON object out of agent output (fenced or raw)."""
    obj = _first_json_object(text)
    if obj is None:
        raise VerdictError("no JSON object found in reviewer output")
    return obj


def _require(obj: Mapping[str, Any], key: str, kind: type) -> Any:
    if key not in obj:
        raise VerdictError(f"missing required key: {key}")
    value = obj[key]
    if not isinstance(value, kind):
        raise VerdictError(f"{key} must be {kind.__name__}, got {type(value).__name__}")
    return value


def _require_enum(obj: Mapping[str, Any], key: str, allowed: tuple[str, ...]) -> str:
    value = _require(obj, key, str)
    if value not in allowed:
        raise VerdictError(f"{key} must be one of {allowed}, got {value!r}")
    return value


def _str_list(obj: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = _require(obj, key, list)
    for item in value:
        if not isinstance(item, str):
            raise VerdictError(f"{key} must be a list of strings")
    return tuple(value)


def _parse_claim(raw: Any, index: int) -> ClaimVerified:
    if not isinstance(raw, dict):
        raise VerdictError(f"claims_verified[{index}] must be an object")
    return ClaimVerified(
        claim=_require(raw, "claim", str),
        evidence=_require(raw, "evidence", str),
        verified=_require(raw, "verified", bool),
    )


def _parse_finding(raw: Any, index: int) -> Finding:
    if not isinstance(raw, dict):
        raise VerdictError(f"findings[{index}] must be an object")
    finding = Finding(
        severity=_require_enum(raw, "severity", SEVERITIES),
        category=_require_enum(raw, "category", CATEGORIES),
        file=_require(raw, "file", str),
        line=_require(raw, "line", int),
        summary=_require(raw, "summary", str),
        failure_scenario=_require(raw, "failure_scenario", str),
    )
    # Appendix E rule 3: a concrete failure scenario is binding by definition.
    if finding.severity == "advisory" and finding.failure_scenario.strip():
        raise VerdictError(
            f"findings[{index}]: advisory findings must have an empty "
            "failure_scenario; a finding with a concrete failure_scenario "
            "must be a blocker"
        )
    if finding.severity == "blocker" and not finding.failure_scenario.strip():
        raise VerdictError(
            f"findings[{index}]: blocker findings must name a concrete "
            "failure_scenario (evidence, not taste)"
        )
    return finding


def parse_verdict(text: str) -> Verdict:
    try:
        payload: Any = json.loads(extract_json(text))
    except json.JSONDecodeError as exc:
        raise VerdictError(f"verdict is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise VerdictError("verdict must be a JSON object")

    verdict = Verdict(
        verdict=_require_enum(payload, "verdict", VERDICTS),
        risk_level=_require_enum(payload, "risk_level", RISK_LEVELS),
        files_reviewed=_str_list(payload, "files_reviewed"),
        claims_verified=tuple(
            _parse_claim(c, i)
            for i, c in enumerate(_require(payload, "claims_verified", list))
        ),
        findings=tuple(
            _parse_finding(f, i)
            for i, f in enumerate(_require(payload, "findings", list))
        ),
        test_assessment=_require(payload, "test_assessment", str),
        residual_risks=_str_list(payload, "residual_risks"),
    )
    return verdict


def validate_review(verdict: Verdict) -> None:
    """Consistency rules for CODE-REVIEW verdicts (rw1/rw2/senior):
    APPROVED with blocker findings is contradictory - blockers block - and
    CHANGES_REQUESTED without a blocker is equally contradictory: there is
    nothing binding for the rework round to address, so accepting it would
    burn empty developer rounds (no verdict section, no fingerprint) all the
    way to CAP_REACHED.

    Report-only verify rounds do NOT use this rule: there, confirmed
    findings (blockers included) live inside an APPROVED report verdict.
    """
    if verdict.approved and verdict.blockers:
        raise VerdictError(
            "verdict APPROVED is inconsistent with blocker findings; "
            "blockers block by definition"
        )
    if not verdict.approved and not verdict.blockers:
        raise VerdictError(
            "verdict CHANGES_REQUESTED requires at least one blocker finding; "
            "advisory-only or empty findings mean there is nothing binding to "
            "address - either approve, or name a blocker with a concrete "
            "failure_scenario"
        )


# The full original prompt is re-sent on every retry (not just this notice),
# because non-resumable runners (CodexRunner has no session) would otherwise
# get the retry with ZERO review context and fabricate a schema-valid verdict
# about nothing. Resumable runners just pay a few extra tokens.
RETRY_TEMPLATE = (
    "{original}\n\n"
    "--- RETRY ---\n"
    "Your previous verdict was rejected by the schema validator:\n{error}\n"
    "Output ONLY the corrected verdict JSON object, nothing else."
)


def validate_rw2(verdict: Verdict) -> None:
    """rw2 guard restriction (design S3): quality findings are advisory only.

    rw2 may block only on real defect categories; a quality objection that
    can name a failure scenario belongs under a defect category instead.
    """
    validate_review(verdict)
    for finding in verdict.blockers:
        if finding.category == "quality":
            raise VerdictError(
                "rw2 must not emit blocker findings with category 'quality'; "
                "quality observations are advisory (empty failure_scenario), "
                "or the finding is a real defect and belongs to a defect "
                "category"
            )


T = TypeVar("T")


def request_payload(
    runner: AgentRunner,
    prompt: str,
    cwd: Path,
    parse: Callable[[str], T],
    *,
    resume: str | None = None,
    max_retries: int = 2,
) -> tuple[T, AgentResult]:
    """Run an agent and parse its output, retrying (bounded) on failure.

    The fail-closed core shared by EVERY consumer of untrusted agent output
    (reviewer verdicts, the investigator payload, the explorer text): output
    is only authoritative when the run itself SUCCEEDED. An errored /
    quota'd `claude -p` still returns a payload that can already contain a
    complete, parseable object (e.g. an interrupted or max-turns run) -
    parsing it would let a failed run clear a gate or poison an artifact.
    Any non-"ok" exit consumes a retry and its text is never parsed; a
    ``VerdictError`` from ``parse`` consumes a retry with the error fed back
    (full original prompt re-sent, see RETRY_TEMPLATE); persistence fails
    closed by raising. Quota is surfaced by QuotaAwareRunner (it waits or
    raises QuotaTimeout before we get here); a plain runner reporting
    "quota"/"error" is simply not trusted.
    """
    attempt_prompt = prompt
    session = resume
    last_error = ""
    for _ in range(max_retries + 1):
        result = runner.run(attempt_prompt, cwd, resume=session)
        session = result.session_id or session
        if result.exit_reason != "ok":
            last_error = (
                f"agent run did not complete cleanly "
                f"(exit_reason={result.exit_reason!r}, rc={result.returncode}); "
                "its output is not trustworthy"
            )
            attempt_prompt = RETRY_TEMPLATE.format(original=prompt, error=last_error)
            continue
        try:
            return parse(result.text), result
        except VerdictError as exc:
            last_error = str(exc)
            attempt_prompt = RETRY_TEMPLATE.format(original=prompt, error=last_error)
    raise VerdictError(
        f"output still malformed after {max_retries} retries: {last_error}"
    )


def request_verdict(
    runner: AgentRunner,
    prompt: str,
    cwd: Path,
    resume: str | None = None,
    max_retries: int = 2,
    validate: Callable[[Verdict], None] | None = validate_review,
) -> tuple[Verdict, AgentResult]:
    """Run a reviewer and parse its verdict, retrying (bounded) on malformed output.

    ``validate`` defaults to the code-review consistency rule; report-only
    verify rounds pass ``validate=None``.
    """

    def _parse(text: str) -> Verdict:
        verdict = parse_verdict(text)
        if validate is not None:
            validate(verdict)
        return verdict

    return request_payload(
        runner, prompt, cwd, _parse, resume=resume, max_retries=max_retries
    )
