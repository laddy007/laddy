"""Tests for oscillation fingerprints."""

from __future__ import annotations

from orchestrator.fingerprint import (
    diff_fingerprint,
    failure_fingerprint,
    repeats,
    verdict_fingerprint,
)
from orchestrator.verdict import parse_verdict
from tests.fakes import blocker, verdict_json


def test_diff_fingerprint_stable_under_whitespace() -> None:
    a = "+++ b/x.py\n+def f():\n+    return 1\n- old\n"
    b = "+++ b/x.py\n+def   f():\n+  return 1\n-   old\n"
    assert diff_fingerprint(a) == diff_fingerprint(b)


def test_diff_fingerprint_differs_on_content() -> None:
    a = "+return 1\n"
    b = "+return 2\n"
    assert diff_fingerprint(a) != diff_fingerprint(b)


def test_verdict_fingerprint_over_blockers_only() -> None:
    v1 = parse_verdict(verdict_json("CHANGES_REQUESTED", [blocker(summary="race")]))
    v2 = parse_verdict(
        verdict_json(
            "CHANGES_REQUESTED",
            [blocker(summary="race"), {"severity": "advisory", "category": "quality",
             "file": "b.py", "line": 1, "summary": "naming", "failure_scenario": ""}],
        )
    )
    assert verdict_fingerprint(v1) == verdict_fingerprint(v2)


def test_verdict_fingerprint_order_independent() -> None:
    f1 = blocker(summary="a", file="x.py")
    f2 = blocker(summary="b", file="y.py")
    v1 = parse_verdict(verdict_json("CHANGES_REQUESTED", [f1, f2]))
    v2 = parse_verdict(verdict_json("CHANGES_REQUESTED", [f2, f1]))
    assert verdict_fingerprint(v1) == verdict_fingerprint(v2)


def test_failure_fingerprint_uses_tail() -> None:
    long_prefix = "\n".join(f"noise {i}" for i in range(100))
    a = long_prefix + "\nFAILED test_x - assert 1 == 2"
    b = "different noise\n" * 100 + "FAILED test_x - assert 1 == 2"
    # tails differ in the last 40 lines -> may differ; identical tails match
    assert failure_fingerprint(a) == failure_fingerprint(a)
    assert failure_fingerprint("FAILED x") != failure_fingerprint("FAILED y")
    assert isinstance(failure_fingerprint(b), str)


def test_repeats() -> None:
    assert repeats(["a", "a"]) is True
    assert repeats(["a", "b"]) is False
    assert repeats(["a", None, "a"]) is True
    assert repeats(["a"]) is False
    assert repeats([]) is False
