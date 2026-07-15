"""CLI wiring for `loop-monitor report --out`: file output vs. stdout."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from loop_monitor.cli import main
from loop_monitor.storage import JsonlStore


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data dir seeded with one sample and wired into MonitorConfig.from_env."""
    ts = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc).timestamp()
    store = JsonlStore(tmp_path, retention_days=21)
    store.append("samples", {"timestamp": ts, "time": "12:00:00"}, ts)
    store.close()
    monkeypatch.setenv("LOOP_MONITOR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOOP_MONITOR_SOCKET", str(tmp_path / "events.sock"))
    return tmp_path


def _at_args() -> list[str]:
    return ["--at", "2026-07-15T12:00:00Z", "--window-minutes", "5"]


def test_out_writes_file_and_prints_nothing(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = data_dir / "round.md"
    rc = main(["report", "--out", str(target), *_at_args()])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout stays empty on the --out path
    assert target.exists()
    assert target.read_text(encoding="utf-8").startswith("#")


def test_no_out_stdout_unchanged(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["report", *_at_args()])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("Window:")  # raw body, not Markdown-wrapped
    assert "#" not in captured.out.splitlines()[0]


def test_out_with_json_is_rejected(data_dir: Path) -> None:
    target = data_dir / "round.md"
    with pytest.raises(SystemExit) as excinfo:
        main(["report", "--json", "--out", str(target), *_at_args()])
    assert excinfo.value.code == 2
    assert not target.exists()


def test_out_refusal_exits_one_and_writes_nothing(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = data_dir / "round.txt"  # wrong suffix -> guard refuses
    rc = main(["report", "--out", str(target), *_at_args()])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "must end in .md" in captured.err
    assert not target.exists()


def test_out_force_overwrites_existing(data_dir: Path) -> None:
    target = data_dir / "round.md"
    target.write_text("stale", encoding="utf-8")
    rc = main(["report", "--out", str(target), "--force", *_at_args()])
    assert rc == 0
    assert target.read_text(encoding="utf-8").startswith("#")
