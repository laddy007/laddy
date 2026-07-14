"""Guard that the kickoff scripts run the foreground design gate between the
clarify gate and the detached loop, in that order."""
from __future__ import annotations

from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _order_ok(text: str) -> bool:
    c = text.find("--phase clarify")
    d = text.find("--phase design")
    l = text.find("--phase loop")
    return -1 < c < d < l


def test_kickoff_runs_design_between_clarify_and_loop() -> None:
    assert _order_ok((_SCRIPTS / "kickoff.sh").read_text(encoding="utf-8"))


def test_local_task_runs_design_between_clarify_and_loop() -> None:
    assert _order_ok((_SCRIPTS / "local-task.sh").read_text(encoding="utf-8"))
