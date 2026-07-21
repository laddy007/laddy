"""Reviewer verdict schema + validation (design doc S9, Appendix E rule 3).

Hand-rolled typed validation instead of a jsonschema dependency: the
load-bearing rule (advisory findings must have an empty failure_scenario)
is a cross-field constraint, and the loop needs precise, promptable error
messages for the bounded retry.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

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


def _last_json_object(text: str) -> str | None:
    """Return the LAST valid top-level ``{...}`` JSON object in ``text``.

    A greedy ``raw_decode`` walk: try the JSON parser at each ``{`` not
    already consumed by a previous successful decode, and keep the last
    success. Only ``{`` anchors are tried, so every success is an object
    (asserted for safety), and a success skips PAST its end - a nested object
    inside a findings/claims array can never be returned as the payload.
    Letting the parser decide validity replaces a brace-depth scan that
    tracked in-string state by quote parity: a lone ``"`` in surrounding
    prose desynced that scan, making the real final verdict's braces read as
    string content so an earlier planted object won (H6 bypass). Prose, an
    unbalanced ``{``/``"``, or a ```json fence between objects simply fails
    to decode at that anchor and is stepped over.

    LAST, not first (H6): agent output is untrusted-input-adjacent - a
    reviewer routinely QUOTES branch content before concluding, so a
    schema-valid APPROVED object planted in the branch and echoed early in
    the transcript must never be mistaken for the verdict. Every payload
    prompt ends with "output ONLY the JSON object", so the model's actual
    answer is the final object in the text; anything before it is narration
    or quotation.
    """
    decoder = json.JSONDecoder()
    pos = 0
    last: str | None = None
    while (idx := text.find("{", pos)) != -1:
        try:
            value, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            pos = idx + 1
            continue
        assert isinstance(value, dict)  # anchored at "{" - always an object
        last = text[idx:end]
        pos = end
    return last


def extract_json(text: str) -> str:
    """Pull the payload JSON object out of agent output (fenced or raw).

    Takes the LAST balanced object - see :func:`_last_json_object` for why
    (H6: quoted/planted objects earlier in the output must not win). Shared
    by every untrusted-output parser (verdicts, investigator, clarify), so
    the anti-spoofing rule is uniform across retry and non-retry paths.
    """
    obj = _last_json_object(text)
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

# A non-"ok" run's text is agent-controlled and lands in a report a human reads
# (via local_merge._abstention_blocker, bounded there at 300). Bound the snippet
# so a runaway/multi-line blob cannot bury the rest of the digest; <=196 leaves
# the abstention prefix room to fit both bounds without a second truncation. The
# text is quoted as a diagnostic only - never parsed, never trusted.
_ERROR_TEXT_MAX = 180


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

    A failed run's text is QUOTED into ``last_error`` (bounded,
    whitespace-collapsed) so the reason the agent itself gave reaches the
    human-facing report - an expired login reads differently from a rejected
    ``--model`` flag. That quote is a diagnostic only: it is never parsed,
    never checked for a verdict, and a non-"ok" run's output still never
    reaches ``parse``. Making a failure legible does not make it trusted.
    """
    attempt_prompt = prompt
    session = resume
    last_error = ""
    for _ in range(max_retries + 1):
        result = runner.run(attempt_prompt, cwd, resume=session)
        session = result.session_id or session
        if result.exit_reason != "ok":
            base = (
                f"agent run did not complete cleanly "
                f"(exit_reason={result.exit_reason!r}, rc={result.returncode})"
            )
            # Collapse every whitespace run (incl. newlines) to one space and
            # bound it: quote what the agent SAID as a diagnostic, but keep it
            # one readable digest item. Empty text keeps the standing clause so
            # the message never trails a dangling separator.
            snippet = " ".join(result.text.split())[:_ERROR_TEXT_MAX]
            last_error = (
                f"{base}: {snippet}"
                if snippet
                else f"{base}; its output is not trustworthy"
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
