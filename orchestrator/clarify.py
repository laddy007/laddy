"""Clarify gate (design doc S5 step 2, Appendix D point 1).

A headless Claude pass reads the spec against the fresh clone and returns
a JSON list of questions. Python asks the Director interactively and
appends the answers to the spec as a ``## Clarifications`` section, so
every later role reads them for free. No questions -> proceed immediately.

A malformed question list retries once and then proceeds with zero
questions: a broken clarify must never block kickoff (decision D6).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from orchestrator.agents import AgentRunner
from orchestrator.artifacts import TaskArtifacts
from orchestrator.verdict import VerdictError, extract_json

AskFn = Callable[[str], str]

CLARIFY_PROMPT = """\
You are the clarify gate of an autonomous dev loop.

Read the task spec at {spec_rel} and inspect the repository code it
touches. Identify questions that MUST be answered by the human Director
before an autonomous implementation can start: ambiguous scope, missing
acceptance criteria, product decisions, conflicting constraints.

Do NOT ask about things the code or the spec already answers.
If the spec is clear enough to implement, return an empty list.

Output ONLY a JSON object, no other text:
{{"questions": ["...", "..."]}}
"""

_MAX_QUESTIONS = 10


def _parse_questions(text: str) -> list[str]:
    payload = json.loads(extract_json(text))
    if not isinstance(payload, dict) or not isinstance(payload.get("questions"), list):
        raise VerdictError("clarify output must be {\"questions\": [...]}")
    questions = payload["questions"]
    for q in questions:
        if not isinstance(q, str):
            raise VerdictError("clarify questions must be strings")
    return list(questions[:_MAX_QUESTIONS])


def _append_clarifications(spec_path: Path, pairs: list[tuple[str, str]]) -> None:
    lines = ["", "## Clarifications", ""]
    for i, (question, answer) in enumerate(pairs, start=1):
        lines.append(f"**Q{i}:** {question}")
        lines.append(f"**A{i}:** {answer}")
        lines.append("")
    existing = spec_path.read_text(encoding="utf-8")
    spec_path.write_text(existing + "\n".join(lines), encoding="utf-8", newline="\n")


def run_clarify_gate(
    runner: AgentRunner,
    wt: Path,
    spec_rel: str,
    ask: AskFn,
    artifacts: TaskArtifacts,
) -> int:
    """Run the gate; returns the number of questions asked."""
    prompt = CLARIFY_PROMPT.format(spec_rel=spec_rel)
    questions: list[str] = []
    detail = ""
    for attempt in range(2):
        result = runner.run(prompt, wt)
        if result.exit_reason != "ok":
            # An errored/timed-out run can still return a complete, parseable
            # payload; trusting it would let a failed run inject forged
            # questions into the spec. Consume the retry without parsing,
            # matching agent_retry.request_payload's fail-closed contract.
            detail = (
                f"clarify run did not complete cleanly (attempt {attempt + 1}): "
                f"exit_reason={result.exit_reason!r}, rc={result.returncode}"
            )
            prompt = (
                CLARIFY_PROMPT.format(spec_rel=spec_rel)
                + "\nYour previous run did not complete. Output ONLY the JSON object."
            )
            continue
        try:
            questions = _parse_questions(result.text)
            break
        except (VerdictError, json.JSONDecodeError) as exc:
            detail = f"malformed clarify output (attempt {attempt + 1}): {exc}"
            prompt = (
                CLARIFY_PROMPT.format(spec_rel=spec_rel)
                + f"\nYour previous output was invalid: {exc}\n"
                "Output ONLY the JSON object."
            )
    if not questions:
        artifacts.append_log(action="clarify", outcome="no_questions", detail=detail)
        return 0

    pairs = [(q, ask(q)) for q in questions]
    _append_clarifications(wt / spec_rel, pairs)
    artifacts.append_log(action="clarify", outcome="answered", questions=len(pairs))
    return len(pairs)


def has_clarify(entries: list[dict[str, object]]) -> bool:
    return any(e.get("action") == "clarify" for e in entries)
