"""Guards on the enriched SPEC_AUTHOR_PROMPT (shared by create-spec and
kickoff --new via _phase_new). The prompt must guide the agent to the house
spec format so authored specs come out consistently structured."""

from __future__ import annotations

from orchestrator.run import SPEC_AUTHOR_PROMPT


def test_prompt_names_front_matter_fields() -> None:
    # AC#7: front-matter fields, incl. risk and the optional draft-proposal.
    for field in ("type", "roles", "risk", "status: draft-proposal"):
        assert field in SPEC_AUTHOR_PROMPT, field
    # the note on what a draft means (the loop refuses to run it).
    assert "draft" in SPEC_AUTHOR_PROMPT.lower()
    assert "refuse" in SPEC_AUTHOR_PROMPT.lower()


def test_prompt_names_section_structure() -> None:
    # AC#7: Goal / Scope In-Out / numbered testable Acceptance criteria / Notes.
    for section in ("Goal", "Scope", "In:", "Out:", "Acceptance criteria", "Notes"):
        assert section in SPEC_AUTHOR_PROMPT, section
    assert "NUMBERED" in SPEC_AUTHOR_PROMPT or "numbered" in SPEC_AUTHOR_PROMPT
    assert "TESTABLE" in SPEC_AUTHOR_PROMPT or "testable" in SPEC_AUTHOR_PROMPT


def test_prompt_names_slice_discipline() -> None:
    # AC#7: keep specs small/testable; slice a big task (S0, S1, ...).
    assert "small" in SPEC_AUTHOR_PROMPT.lower()
    assert "S0" in SPEC_AUTHOR_PROMPT and "S1" in SPEC_AUTHOR_PROMPT


def test_prompt_has_no_stale_myapp_naming() -> None:
    # AC#8: the stale "myapp agent" wording is gone; naming is target-generic.
    assert "myapp" not in SPEC_AUTHOR_PROMPT


def test_prompt_is_ascii_and_format_safe() -> None:
    # CLAUDE.md invariant (ASCII source) + the .format(spec_rel=...) footgun:
    # {spec_rel} must be the SOLE replacement field, no stray unescaped braces.
    assert SPEC_AUTHOR_PROMPT.isascii()
    rendered = SPEC_AUTHOR_PROMPT.format(spec_rel=".laddy/specs/demo.md")
    assert ".laddy/specs/demo.md" in rendered
    assert "{" not in rendered and "}" not in rendered
