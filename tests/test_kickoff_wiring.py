"""Guard that the kickoff scripts run the foreground design gate between the
clarify gate and the detached loop, in that order."""
from __future__ import annotations

import os
import subprocess
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
    # detach the same way the loop does: `setsid --fork` (unbuffered, heartbeat)
    # so it survives an SSH drop AND the tmux_wrap session closing when the
    # launcher exits. A plain `nohup ... &` races tmux's killpg on pane teardown
    # (measured: the loop died with an empty $LOG). Guard against a regression.
    text = (_SCRIPTS / "kickoff.sh").read_text(encoding="utf-8")
    resume = next(ln for ln in text.splitlines() if "--phase resume" in ln)
    assert "setsid --fork" in resume, resume
    assert not resume.rstrip().endswith("&"), resume  # no racy background detach
    assert "LADDY_LOG_HEARTBEAT=1" in resume, resume
    assert " -u " in resume, resume
    assert 'REST[@]' in resume, resume  # forwards --reason "text" verbatim


def test_kickoff_launches_loop_observably() -> None:
    # AC#3: the detached loop must run unbuffered (so a crash before the terminal
    # print does not swallow buffered output into an empty $LOG) with the log
    # heartbeat enabled, and detach via `setsid --fork` (NOT `nohup ... &`, which
    # races tmux's killpg when the wrap session closes). Guard against regress.
    text = (_SCRIPTS / "kickoff.sh").read_text(encoding="utf-8")
    loop = next(ln for ln in text.splitlines() if "--phase loop" in ln)
    assert "LADDY_LOG_HEARTBEAT=1" in loop, loop
    assert " -u " in loop, loop  # python -u: unbuffered stdout/stderr
    assert "setsid --fork" in loop, loop
    assert not loop.rstrip().endswith("&"), loop  # no racy background detach


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


def _phase_of(call: list[str]) -> str | None:
    return call[call.index("--phase") + 1] if "--phase" in call else None


def test_kickoff_flag_looking_token_after_new_is_not_swallowed_as_brief(
    tmp_path: Path,
) -> None:
    # AC5: a token after --new that looks like a flag (starts with '-') is a
    # separate flag, not a brief - it must reach REST (and so clarify), and
    # --phase new must NOT get a --brief for it.
    fake_py = tmp_path / "fake_python"
    calls_log = tmp_path / "calls.log"
    fake_py.write_text(
        "#!/usr/bin/env bash\n"
        "{ printf '%s' \"$1\"; shift; "
        "for a in \"$@\"; do printf '\\x1f%s' \"$a\"; done; printf '\\n'; } "
        '>> "$FAKE_PY_LOG"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_py.chmod(0o755)

    env = dict(os.environ)
    env.update(
        PYTHON_BIN=str(fake_py),
        FAKE_PY_LOG=str(calls_log),
        LADDY_NO_TMUX="1",
        AGENT_LOG_DIR=str(tmp_path / "logs"),
    )
    subprocess.run(
        ["bash", str(_SCRIPTS / "kickoff.sh"), "mytask", "--new", "-x", "--skip-clarify"],
        env=env,
        cwd=tmp_path,
        check=True,
        timeout=30,
    )
    calls = [
        line.split("\x1f")
        for line in calls_log.read_text(encoding="utf-8").splitlines()
    ]
    new_call = next(c for c in calls if _phase_of(c) == "new")
    clarify_call = next(c for c in calls if _phase_of(c) == "clarify")
    assert "--brief" not in new_call, new_call
    assert "-x" in clarify_call, clarify_call
