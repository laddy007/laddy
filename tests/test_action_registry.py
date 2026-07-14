"""Invariant: every iteration-log `action` the loop writes is consciously
classified as progress or non-progress. Guards the `_derive_status`
allowlist-gap bug class (a new action silently mis-deriving task status)."""

from __future__ import annotations

import re
from pathlib import Path

from orchestrator.run import _NON_PROGRESS_ACTIONS

_SRC = Path(__file__).resolve().parents[1] / "orchestrator"
# matches append_log(action="x") kwarg form AND {"action": "x"} dict form
_ACTION_RE = re.compile(r"""["']?action["']?\s*[:=]\s*["']([a-z][a-z0-9_-]*)["']""")

# Actions that mean the loop did real work (task is in-progress once present).
_PROGRESS_ACTIONS = frozenset({
    "developer", "fast_tests", "rw1", "rw2", "authoritative", "senior", "push",
    "explorer", "investigator", "verify", "quota_exhausted", "quota_resumed",
    "path_guard",
})
# Terminal marker (handled before the in-progress check in _derive_status).
_TERMINAL_ACTIONS = frozenset({"terminal"})


def _scan_actions() -> set[str]:
    found: set[str] = set()
    # loop.py / clarify.py / flags.py are the only modules that call
    # append_log; run.py's argparse action="store_true" lives elsewhere and
    # is intentionally out of scope.
    for name in ("loop.py", "clarify.py", "flags.py"):
        found.update(_ACTION_RE.findall((_SRC / name).read_text(encoding="utf-8")))
    return found


def test_every_log_action_is_consciously_classified() -> None:
    scanned = _scan_actions()
    classified = _NON_PROGRESS_ACTIONS | _PROGRESS_ACTIONS | _TERMINAL_ACTIONS
    unclassified = scanned - classified
    assert not unclassified, (
        f"unclassified log action(s): {sorted(unclassified)}. Add each to "
        "_PROGRESS_ACTIONS/_TERMINAL_ACTIONS in this test AND, if it must NOT "
        "flip a task to in-progress, to run._NON_PROGRESS_ACTIONS."
    )


def test_progress_and_non_progress_are_disjoint() -> None:
    assert not (_PROGRESS_ACTIONS & _NON_PROGRESS_ACTIONS)


def test_scan_finds_the_known_core_actions() -> None:
    # sanity: the regex actually matches the source (guards against a silent
    # scan that finds nothing and so never fails)
    scanned = _scan_actions()
    assert {"developer", "flag", "clarify", "terminal", "rw1", "rw2"} <= scanned
