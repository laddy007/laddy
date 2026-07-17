"""Launcher tests for scripts/merge-verified.sh --local forwarding/softening.

The launcher is a thin bash wrapper; these tests run it as a real subprocess
against a recording PYTHON_BIN stub (so no orchestrator import / no gate runs)
and assert only the launcher's own two --local behaviours: it forwards
``--local <ref>`` verbatim, and it softens the missing-remote *die* to a *warn*
when --local is present (the mode must run with no hub configured).

The launcher is copied into a throwaway ENGINE_DIR so it never sources the real
env.local (which would override PYTHON_BIN) - the test stays hermetic.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

_LAUNCHER = Path(__file__).resolve().parents[1] / "scripts" / "merge-verified.sh"


def _engine_copy(tmp_path: Path) -> Path:
    """Copy the launcher into a temp engine dir with NO env.local."""
    engine = tmp_path / "engine"
    (engine / "scripts").mkdir(parents=True)
    dst = engine / "scripts" / "merge-verified.sh"
    shutil.copy(_LAUNCHER, dst)
    dst.chmod(0o755)
    return dst


def _recording_stub(tmp_path: Path) -> tuple[Path, Path]:
    """A python-shaped stub that records its argv and exits 0."""
    argv_file = tmp_path / "argv.txt"
    stub = tmp_path / "pystub.sh"
    stub.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{argv_file}"\nexit 0\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub, argv_file


def _repo_without_branch_remote(tmp_path: Path) -> Path:
    """A target repo with NO 'ghost' remote configured."""
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True
    )
    return repo


def test_launcher_local_forwards_ref_and_warns_on_missing_remote(
    tmp_path: Path,
) -> None:
    launcher = _engine_copy(tmp_path)
    stub, argv_file = _recording_stub(tmp_path)
    repo = _repo_without_branch_remote(tmp_path)

    env = {
        **os.environ,
        "PYTHON_BIN": str(stub),
        "AGENT_BRANCH_REMOTE": "ghost",  # not configured on the repo
    }
    proc = subprocess.run(
        [str(launcher), "t1", "--local", "../fix"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
    )
    # missing remote under --local -> WARN, not die; the stub ran and exited 0
    assert proc.returncode == 0, proc.stderr
    assert "WARN" in proc.stderr
    assert "ERROR" not in proc.stderr
    argv = argv_file.read_text(encoding="utf-8").splitlines()
    # --local <ref> and the task id are forwarded verbatim to the module
    assert "--local" in argv
    assert "../fix" in argv
    assert "t1" in argv
    assert argv[argv.index("--local") + 1] == "../fix"


def test_launcher_without_local_still_dies_on_missing_remote(
    tmp_path: Path,
) -> None:
    launcher = _engine_copy(tmp_path)
    stub, argv_file = _recording_stub(tmp_path)
    repo = _repo_without_branch_remote(tmp_path)

    env = {
        **os.environ,
        "PYTHON_BIN": str(stub),
        "AGENT_BRANCH_REMOTE": "ghost",
    }
    proc = subprocess.run(
        [str(launcher), "t1"],  # no --local
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
    )
    # default path: a missing branch remote is still a hard die, and the python
    # module (stub) is never reached
    assert proc.returncode != 0
    assert "ERROR" in proc.stderr
    assert not argv_file.exists()
