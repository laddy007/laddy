"""Tests for the append-only task artifact store."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import TARGET_DIR_NAME
from orchestrator.artifacts import LOG, TaskArtifacts


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
