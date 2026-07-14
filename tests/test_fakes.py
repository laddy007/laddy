"""The shared fakes must fail CLOSED: an exhausted result queue is a test
bug (the code under test ran one more command than the test queued), and it
must raise - not invent a green result. FakeRunner already behaves this way;
these tests pin the same contract for the shell fakes."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fakes import FakeShell, FakeSplitShell


def test_fake_shell_returns_queued_then_raises_when_exhausted(tmp_path: Path) -> None:
    shell = FakeShell(results=[(1, "FAILED test_x")])
    assert shell("pytest", tmp_path) == (1, "FAILED test_x")
    with pytest.raises(AssertionError, match="out of queued results"):
        shell("pytest", tmp_path)


def test_fake_shell_with_no_results_raises_on_first_call(tmp_path: Path) -> None:
    shell = FakeShell()
    with pytest.raises(AssertionError, match="out of queued results"):
        shell("pytest", tmp_path)
    # the call is still recorded, so the failing test can show what ran
    assert shell.calls == [("pytest", tmp_path)]


def test_fake_split_shell_raises_when_exhausted(tmp_path: Path) -> None:
    shell = FakeSplitShell(results=[(0, "out", "err")])
    assert shell("gate", tmp_path) == (0, "out", "err")
    with pytest.raises(AssertionError, match="out of queued results"):
        shell("gate", tmp_path)
