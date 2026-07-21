"""Launcher tests for scripts/create-spec.sh.

create-spec.sh is a thin bash wrapper that runs ONLY the interactive spec
authoring phase (orchestrator.run --phase new) on the Director's local machine,
sourcing env.local (never env.vps / vps.conf). These tests run it as a real
subprocess against a recording PYTHON_BIN stub (so no orchestrator import) and
assert the launcher's own behaviour: env.local sourcing (required), task-name
validation, the single --phase new invocation, exit-code propagation, and the
follow-up hint.

The launcher is copied into a throwaway ENGINE_DIR (with its env_guard lib) so
it never sources the real env.local and the test stays hermetic - the same
pattern as tests/test_merge_verified_launcher.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_LAUNCHER = _SCRIPTS / "create-spec.sh"

_SEP = "\x1f"


def _engine_copy(tmp_path: Path) -> Path:
    """Copy the launcher + its env_guard lib into a temp engine dir (NO
    env.local unless the test writes one). Returns the ENGINE_DIR."""
    engine = tmp_path / "engine"
    (engine / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(_LAUNCHER, engine / "scripts" / "create-spec.sh")
    (engine / "scripts" / "create-spec.sh").chmod(0o755)
    shutil.copy(
        _SCRIPTS / "lib" / "env_guard.sh", engine / "scripts" / "lib" / "env_guard.sh"
    )
    return engine


def _recording_stub(engine: Path) -> Path:
    """A python-shaped stub that APPENDS one \\x1f-joined argv line per call to
    a log file and exits 0, so every invocation (if any) is captured."""
    log = engine / "calls.log"
    stub = engine / "pystub.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        '{ for a in "$@"; do printf "%s\\x1f" "$a"; done; printf "\\n"; } '
        f'>> "{log}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return log


def _failing_stub(engine: Path, code: int) -> Path:
    """A python-shaped stub that records its call then exits `code` - models a
    _phase_new refusal (spec already exists / authoring added nothing)."""
    log = engine / "calls.log"
    stub = engine / "pystub.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        '{ for a in "$@"; do printf "%s\\x1f" "$a"; done; printf "\\n"; } '
        f'>> "{log}"\n'
        f"exit {code}\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return log


def _write_env_local(engine: Path, python_bin: str) -> None:
    (engine / "env.local").write_text(f"PYTHON_BIN={python_bin}\n", encoding="utf-8")


def _calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [
        [tok for tok in line.split(_SEP) if tok != ""]
        for line in log.read_text(encoding="utf-8").splitlines()
    ]


def _phase_of(call: list[str]) -> str | None:
    return call[call.index("--phase") + 1] if "--phase" in call else None


def _run(
    engine: Path, *args: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    # PYTHON_BIN is deliberately stripped from the inherited env so it can ONLY
    # come from the env.local the launcher sources (that is what we assert).
    env = {k: v for k, v in os.environ.items() if k != "PYTHON_BIN"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [str(engine / "scripts" / "create-spec.sh"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_runs_only_phase_new_exactly_once(tmp_path: Path) -> None:
    # AC#2: exactly one orchestrator.run invocation, phase == new, task id
    # forwarded, and NO clarify/design/loop phase anywhere.
    engine = _engine_copy(tmp_path)
    log = _recording_stub(engine)
    _write_env_local(engine, str(engine / "pystub.sh"))

    proc = _run(engine, "mytask")
    assert proc.returncode == 0, proc.stderr

    calls = _calls(log)
    assert len(calls) == 1, calls
    call = calls[0]
    assert _phase_of(call) == "new"
    assert "mytask" in call
    assert "-m" in call and "orchestrator.run" in call
    for banned in ("clarify", "design", "loop"):
        assert banned not in call, (banned, call)


def test_runs_with_only_env_local_no_vps(tmp_path: Path) -> None:
    # AC#1: runs from env.local alone - no vps.conf, no env.vps. The env.vps we
    # plant points PYTHON_BIN at a poison stub; a green run proves env.local
    # (not env.vps) was sourced.
    engine = _engine_copy(tmp_path)
    log = _recording_stub(engine)
    _write_env_local(engine, str(engine / "pystub.sh"))
    poison = engine / "poison.sh"
    poison.write_text("#!/usr/bin/env bash\nexit 7\n", encoding="utf-8")
    poison.chmod(0o755)
    (engine / "env.vps").write_text(f"PYTHON_BIN={poison}\n", encoding="utf-8")

    proc = _run(engine, "mytask")
    assert proc.returncode == 0, proc.stderr
    assert not (engine / "vps.conf").exists()
    calls = _calls(log)
    assert len(calls) == 1 and _phase_of(calls[0]) == "new"


def test_missing_env_local_fails_hard(tmp_path: Path) -> None:
    # AC#4: a missing env.local is a clear error + non-zero exit, never a silent
    # fall back to orchestrator defaults; python is never reached.
    engine = _engine_copy(tmp_path)
    log = _recording_stub(engine)  # present, but env.local won't point at it
    # NOTE: no env.local written.

    proc = _run(engine, "mytask")
    assert proc.returncode != 0
    assert "env.local" in proc.stderr
    assert "local-onboard.sh" in proc.stderr or "env.local.example" in proc.stderr
    assert _calls(log) == []


def test_empty_task_refused(tmp_path: Path) -> None:
    # AC#3: empty task id -> non-zero + message, python never reached.
    engine = _engine_copy(tmp_path)
    log = _recording_stub(engine)
    _write_env_local(engine, str(engine / "pystub.sh"))

    proc = _run(engine)  # no task arg
    assert proc.returncode != 0
    assert "Usage" in proc.stderr
    assert _calls(log) == []


def test_invalid_char_task_refused(tmp_path: Path) -> None:
    # AC#3: a task id with an illegal character -> non-zero + message.
    engine = _engine_copy(tmp_path)
    log = _recording_stub(engine)
    _write_env_local(engine, str(engine / "pystub.sh"))

    proc = _run(engine, "bad/name")
    assert proc.returncode != 0
    assert "Invalid task name" in proc.stderr
    assert _calls(log) == []


def test_reserved_main_task_refused(tmp_path: Path) -> None:
    # AC#3: the reserved 'main' id -> non-zero + message (hub closed namespace).
    engine = _engine_copy(tmp_path)
    log = _recording_stub(engine)
    _write_env_local(engine, str(engine / "pystub.sh"))

    proc = _run(engine, "main")
    assert proc.returncode != 0
    assert "reserved" in proc.stderr
    assert _calls(log) == []


def test_success_prints_kickoff_hint(tmp_path: Path) -> None:
    # AC#5: on success the follow-up hint names `kickoff <task>` (no --new).
    engine = _engine_copy(tmp_path)
    _recording_stub(engine)
    _write_env_local(engine, str(engine / "pystub.sh"))

    proc = _run(engine, "mytask")
    assert proc.returncode == 0, proc.stderr
    assert "kickoff mytask" in proc.stdout
    assert "--new" in proc.stdout  # "(no --new)"


def test_refusal_propagates_and_suppresses_hint(tmp_path: Path) -> None:
    # AC#5/#6: a _phase_new refusal (exit 2) propagates unchanged AND the
    # success hint is NOT printed (set -e aborts before it).
    engine = _engine_copy(tmp_path)
    log = _failing_stub(engine, 2)
    _write_env_local(engine, str(engine / "pystub.sh"))

    proc = _run(engine, "mytask")
    assert proc.returncode == 2
    assert "kickoff mytask" not in proc.stdout
    # phase new WAS attempted (the refusal comes from python, not the launcher).
    calls = _calls(log)
    assert len(calls) == 1 and _phase_of(calls[0]) == "new"


def test_launcher_text_targets_env_local_not_vps() -> None:
    # AC#1 (static): the launcher sources env.local and, in its EXECUTABLE lines
    # (comments may mention them to document the contrast), never touches
    # env.vps / vps.conf; it runs exactly one phase, `new`.
    text = _LAUNCHER.read_text(encoding="utf-8")
    assert "env.local" in text
    code = "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "env.vps" not in code
    assert "vps.conf" not in code
    assert code.count("--phase new") == 1
    for banned in ("--phase clarify", "--phase design", "--phase loop"):
        assert banned not in code, banned
