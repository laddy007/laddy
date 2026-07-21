"""Bounded, fail-closed retry around agent runs (relocated from verdict.py).

The generic core every consumer of untrusted agent output shares: run the
agent, trust its text only when the run itself succeeded, feed a schema
error back as a retry, give up after a bound. The verdict SCHEMA stays in
orchestrator.verdict; this module owns the run/parse/retry plumbing.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from orchestrator.agents import AgentResult, AgentRunner
from orchestrator.verdict import Verdict, VerdictError, parse_verdict, validate_review

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
