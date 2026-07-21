"""Tests for the append-only task artifact store."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import TARGET_DIR_NAME
from orchestrator.artifacts import (
    LOG,
    SPEC,
    ArtifactPathError,
    LogCorruptionError,
    TaskArtifacts,
)


def _artifacts(tmp_path: Path) -> TaskArtifacts:
    ticks = iter(range(100))
    return TaskArtifacts(tmp_path, "t1", now=lambda: f"2026-07-05T00:00:{next(ticks):02d}Z")


def test_dir_layout_and_autocreate(tmp_path: Path) -> None:
    art = _artifacts(tmp_path)
    art.append_log(action="developer", outcome="ok")
    assert art.dir == tmp_path / TARGET_DIR_NAME / "tasks" / "t1"
    assert (art.dir / LOG).is_file()


def test_append_log_is_append_only(tmp_path: Path) -> None:
    art = _artifacts(tmp_path)
    art.append_log(action="developer", outcome="ok", round=1)
    art.append_log(action="fast_tests", outcome="fail", round=1)
    two_entries = (art.dir / LOG).read_bytes()
    art.append_log(action="developer", outcome="ok", round=2)
    three_entries = (art.dir / LOG).read_bytes()
    # existing bytes are never rewritten - strictly appended
    assert three_entries.startswith(two_entries)
    assert three_entries.count(b"\n") == 3


def test_append_log_heartbeat_env_gated(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    art = _artifacts(tmp_path)
    # off by default: no stderr noise (keeps the rest of the suite quiet)
    monkeypatch.delenv("LADDY_LOG_HEARTBEAT", raising=False)
    art.append_log(action="fast_tests", outcome="pass", round=1)
    assert capsys.readouterr().err == ""
    # on: one line per entry to stderr, so a detached run's $LOG shows progress
    monkeypatch.setenv("LADDY_LOG_HEARTBEAT", "1")
    art.append_log(action="rw1", outcome="approved", round=2)
    assert "[loop] r2 rw1: approved" in capsys.readouterr().err
    # entry without a round still prints (no rN tag)
    art.append_log(action="terminal", outcome="PUSHED")
    assert "[loop] terminal: PUSHED" in capsys.readouterr().err


def test_read_log_roundtrip_with_ts(tmp_path: Path) -> None:
    art = _artifacts(tmp_path)
    art.append_log(action="rw1", outcome="approved", session_id="s9")
    entries = art.read_log()
    assert entries == [
        {
            "ts": "2026-07-05T00:00:00Z",
            "action": "rw1",
            "outcome": "approved",
            "session_id": "s9",
        }
    ]


def test_read_log_empty_when_missing(tmp_path: Path) -> None:
    assert _artifacts(tmp_path).read_log() == []


def test_read_log_tolerates_truncated_final_line(tmp_path: Path) -> None:
    # A crash (OOM/SSH drop) mid-append can leave a truncated final JSON line.
    # read_log must skip it and return the intact entries, never raise - it
    # feeds resume, --phase status, and the "always exit 0" flags reporter.
    art = _artifacts(tmp_path)
    art.append_log(action="developer", outcome="ok")
    with (tmp_path / TARGET_DIR_NAME / "tasks" / "t1" / LOG).open(
        "a", encoding="utf-8"
    ) as fh:
        fh.write('{"ts": "2026-')  # torn, no newline
    entries = art.read_log()
    assert [e["action"] for e in entries] == ["developer"]


def test_read_log_raises_on_malformed_interior_line(tmp_path: Path) -> None:
    # L-D4-3: a completed append is always a whole line, so a malformed INTERIOR
    # line is real corruption, not a torn append. Silently dropping it would hole
    # the replayed state; read_log fails closed (raises) instead.
    art = _artifacts(tmp_path)
    art.append_log(action="developer", outcome="ok")
    log = tmp_path / TARGET_DIR_NAME / "tasks" / "t1" / LOG
    with log.open("a", encoding="utf-8") as fh:
        fh.write("{not json}\n")  # corrupt interior line, fully terminated
    art.append_log(action="rw1", outcome="approved")  # a later, valid line
    with pytest.raises(LogCorruptionError):
        art.read_log()


def test_write_read_json(tmp_path: Path) -> None:
    art = _artifacts(tmp_path)
    art.write_json("reviewer-a-verdict.json", {"verdict": "APPROVED"})
    assert art.read_json("reviewer-a-verdict.json") == {"verdict": "APPROVED"}
    assert art.read_json("missing.json") is None


def test_write_text(tmp_path: Path) -> None:
    art = _artifacts(tmp_path)
    art.write_text("human-summary.md", "# Summary\n")
    assert (art.dir / "human-summary.md").read_text(encoding="utf-8") == "# Summary\n"


def test_copy_spec(tmp_path: Path) -> None:
    src = tmp_path / "myspec.md"
    src.write_text("# Spec\nbody\n", encoding="utf-8")
    art = _artifacts(tmp_path)
    art.copy_spec(src)
    assert art.spec_path.read_text(encoding="utf-8") == "# Spec\nbody\n"


# --- symlink safety: task-dir artifacts are branch-controlled -----------------
# The task dir and its files can arrive from a merged (untrusted) branch. A
# planted symlink must never redirect a write onto an arbitrary file on the
# trusted merge machine - every writer fails closed (O_NOFOLLOW / lstat guard)
# rather than following the link. See ArtifactPathError.


def test_write_text_refuses_a_symlinked_file(tmp_path: Path) -> None:
    art = _artifacts(tmp_path)
    art.append_log(action="developer", outcome="ok")  # materialize the real dir
    outside = tmp_path / "outside.txt"
    outside.write_text("original\n", encoding="utf-8")
    (art.dir / "merge-advisory.md").symlink_to(outside)
    with pytest.raises(ArtifactPathError):
        art.write_text("merge-advisory.md", "advisory\n")
    assert outside.read_text(encoding="utf-8") == "original\n"  # not written through


def test_write_json_refuses_a_symlinked_file(tmp_path: Path) -> None:
    art = _artifacts(tmp_path)
    art.append_log(action="developer", outcome="ok")
    outside = tmp_path / "outside.json"
    outside.write_text("original\n", encoding="utf-8")
    (art.dir / "findings.json").symlink_to(outside)
    with pytest.raises(ArtifactPathError):
        art.write_json("findings.json", {"x": 1})
    assert outside.read_text(encoding="utf-8") == "original\n"


def test_copy_spec_refuses_a_symlinked_target(tmp_path: Path) -> None:
    art = _artifacts(tmp_path)
    art.append_log(action="developer", outcome="ok")
    outside = tmp_path / "outside_spec.md"
    outside.write_text("original\n", encoding="utf-8")
    (art.dir / SPEC).symlink_to(outside)
    src = tmp_path / "src.md"
    src.write_text("# new\n", encoding="utf-8")
    with pytest.raises(ArtifactPathError):
        art.copy_spec(src)
    assert outside.read_text(encoding="utf-8") == "original\n"


def test_write_refuses_a_symlinked_task_dir(tmp_path: Path) -> None:
    # The whole <task>/ dir shipped as a symlink: mkdir(exist_ok=True) would
    # silently accept it, so _ensure's lstat walk must reject it up front.
    art = _artifacts(tmp_path)
    tasks = tmp_path / TARGET_DIR_NAME / "tasks"
    tasks.mkdir(parents=True)
    evil = tmp_path / "evil"
    evil.mkdir()
    (tasks / "t1").symlink_to(evil, target_is_directory=True)
    with pytest.raises(ArtifactPathError):
        art.write_text("merge-advisory.md", "advisory\n")
    assert not (evil / "merge-advisory.md").exists()


def test_write_text_overwrites_a_regular_file(tmp_path: Path) -> None:
    # O_NOFOLLOW only blocks symlinks: a branch may legitimately ship a regular
    # merge-advisory.md, and overwriting a real file must still work.
    art = _artifacts(tmp_path)
    art.write_text("merge-advisory.md", "first\n")
    art.write_text("merge-advisory.md", "second\n")
    assert (art.dir / "merge-advisory.md").read_text(encoding="utf-8") == "second\n"
