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


def test_kickoff_resume_forwards_reason_and_detaches() -> None:
    # --resume must reach `--phase resume` (forwarding --reason via REST) and
    # detach the same way the loop does (nohup, unbuffered, heartbeat) so it
    # survives an SSH drop. Guard the wiring against a silent regression.
    text = (_SCRIPTS / "kickoff.sh").read_text(encoding="utf-8")
    resume = next(ln for ln in text.splitlines() if "--phase resume" in ln)
    assert "nohup" in resume, resume
    assert "LADDY_LOG_HEARTBEAT=1" in resume, resume
    assert " -u " in resume, resume
    assert 'REST[@]' in resume, resume  # forwards --reason "text" verbatim


def test_kickoff_launches_loop_observably() -> None:
    # AC#3: the detached loop must run unbuffered (so a crash before the terminal
    # print does not swallow buffered output into an empty $LOG) with the log
    # heartbeat enabled. Guard the wiring so neither silently regresses.
    text = (_SCRIPTS / "kickoff.sh").read_text(encoding="utf-8")
    loop = next(ln for ln in text.splitlines() if "--phase loop" in ln)
    assert "LADDY_LOG_HEARTBEAT=1" in loop, loop
    assert " -u " in loop, loop  # python -u: unbuffered stdout/stderr


def test_kickoff_forwards_brief_only_to_new() -> None:
    # A brief typed after --new must reach ONLY the --phase new invocation
    # (as --brief) and never clarify/design/loop - the routing bug this spec
    # fixes (a stray positional crashing --phase clarify).
    text = (_SCRIPTS / "kickoff.sh").read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("#")]
    new_line = next(ln for ln in lines if "--phase new" in ln)
    clarify_line = next(ln for ln in lines if "--phase clarify" in ln)
    design_line = next(ln for ln in lines if "--phase design" in ln)
    loop_line = next(ln for ln in lines if "--phase loop" in ln)
    assert "--brief" in new_line, new_line
    assert "BRIEF" in new_line, new_line
    assert "--brief" not in clarify_line, clarify_line
    assert "--brief" not in design_line, design_line
    assert "--brief" not in loop_line, loop_line
    assert "BRIEF" not in clarify_line, clarify_line
    assert "BRIEF" not in design_line, design_line
    assert "BRIEF" not in loop_line, loop_line
