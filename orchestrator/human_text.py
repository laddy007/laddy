"""Safe rendering helpers for untrusted text shown to a human."""

from __future__ import annotations

import unicodedata

UNTRUSTED_INLINE_LIMIT = 500
_TRUNCATION_MARKER = "... [truncated]"


def untrusted_inline(text: str, *, limit: int = UNTRUSTED_INLINE_LIMIT) -> str:
    """Render attacker-influenced text as one bounded terminal-safe line.

    Ordinary whitespace, including newlines, collapses to one ASCII space.
    Controls and Unicode format characters remain visible as ASCII escape
    spellings, so ANSI/CSI/OSC, carriage-return, backspace, and bidi controls
    cannot alter the Director's terminal. The returned string never exceeds
    ``limit`` characters.
    """
    if limit < len(_TRUNCATION_MARKER):
        raise ValueError("untrusted text limit is too small")

    tokens: list[str] = []
    pending_space = False
    for char in text:
        if char in "\n\t\v\f" or (char.isspace() and char != "\r"):
            pending_space = bool(tokens)
            continue

        codepoint = ord(char)
        category = unicodedata.category(char)
        if char == "\r":
            token = r"\r"
        elif char == "\b":
            token = r"\b"
        elif category in {"Cc", "Cf", "Cs"}:
            if codepoint <= 0xFF:
                token = f"\\x{codepoint:02x}"
            elif codepoint <= 0xFFFF:
                token = f"\\u{codepoint:04x}"
            else:
                token = f"\\U{codepoint:08x}"
        else:
            token = char

        if pending_space:
            tokens.append(" ")
            pending_space = False
        tokens.append(token)

    rendered = "".join(tokens).strip()
    if len(rendered) <= limit:
        return rendered

    prefix_limit = limit - len(_TRUNCATION_MARKER)
    prefix: list[str] = []
    used = 0
    for token in tokens:
        if used + len(token) > prefix_limit:
            break
        prefix.append(token)
        used += len(token)
    return "".join(prefix).rstrip() + _TRUNCATION_MARKER
