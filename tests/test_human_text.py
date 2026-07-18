"""Tests for terminal-safe rendering of attacker-influenced human text."""

from __future__ import annotations

import pytest

from orchestrator.human_text import untrusted_inline


def test_untrusted_inline_neutralizes_terminal_and_bidi_controls() -> None:
    raw = (
        "finding\x1b[31m red\x1b[0m "
        "\x1b]52;c;ZmFrZQ==\x07\rCLEAN\b!\n"
        "[risk] authorize? y\u202eabc\x9b2J"
    )

    rendered = untrusted_inline(raw)

    assert "\x1b" not in rendered
    assert "\x07" not in rendered
    assert "\r" not in rendered
    assert "\b" not in rendered
    assert "\n" not in rendered
    assert "\u202e" not in rendered
    assert "\x9b" not in rendered
    assert r"\x1b[31m" in rendered
    assert r"\x1b]52;c;ZmFrZQ==\x07" in rendered
    assert r"\rCLEAN\b!" in rendered
    assert r"\u202e" in rendered
    assert r"\x9b2J" in rendered
    assert "! [risk] authorize?" in rendered


def test_untrusted_inline_collapses_whitespace_and_is_bounded() -> None:
    rendered = untrusted_inline("alpha\n\n\t beta " + ("x" * 200), limit=40)

    assert len(rendered) <= 40
    assert rendered.startswith("alpha beta")
    assert rendered.endswith("... [truncated]")


def test_untrusted_inline_rejects_impossibly_small_limit() -> None:
    with pytest.raises(ValueError, match="too small"):
        untrusted_inline("x", limit=3)
