"""Oscillation / repeated-failure fingerprints (design doc S6).

If the same authoritative failure or the same rw2 blocker set repeats
across rounds, the loop short-circuits to the senior reviewer instead of
burning the whole iteration cap.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

from orchestrator.verdict import Verdict

_WS_RE = re.compile(r"\s+")
_FAILURE_TAIL_LINES = 40


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def diff_fingerprint(diff: str) -> str:
    """Stable under whitespace-only changes."""
    lines = [
        _normalize(line)
        for line in diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    return _sha("\n".join(line for line in lines if line))


def verdict_fingerprint(verdict: Verdict) -> str:
    """Fingerprint of the binding content: sorted blocker signatures."""
    signatures = sorted(f"{f.category}|{f.file}|{_normalize(f.summary)}" for f in verdict.blockers)
    return _sha("\n".join(signatures))


def failure_fingerprint(output: str) -> str:
    tail = output.splitlines()[-_FAILURE_TAIL_LINES:]
    return _sha("\n".join(_normalize(line) for line in tail))


def repeats(fingerprints: Sequence[str | None]) -> bool:
    """True when the two most recent recorded fingerprints are identical."""
    seen = [fp for fp in fingerprints if fp]
    return len(seen) >= 2 and seen[-1] == seen[-2]
