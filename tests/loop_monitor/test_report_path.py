"""Unit tests for the report output path guard, in isolation from the CLI."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from loop_monitor.report_path import (
    ReportPathError,
    render_markdown,
    write_report,
)


def test_happy_path_writes_file(tmp_path: Path) -> None:
    target = tmp_path / "report.md"
    write_report("hello", target, tmp_path)
    assert target.read_text(encoding="utf-8") == "hello"


@pytest.mark.parametrize("name", ["report.txt", "config.toml", "report", ".md"])
def test_non_md_suffix_is_refused_and_writes_nothing(
    tmp_path: Path, name: str
) -> None:
    target = tmp_path / name
    with pytest.raises(ReportPathError, match="must end in .md"):
        write_report("x", target, tmp_path)
    assert not target.exists()


def test_planted_symlink_is_refused_without_force(tmp_path: Path) -> None:
    decoy = tmp_path / "victim.txt"
    decoy.write_text("do not touch\n")
    target = tmp_path / "report.md"
    target.symlink_to(decoy)

    with pytest.raises(ReportPathError):
        write_report("x", target, tmp_path)

    assert decoy.read_text() == "do not touch\n"
    assert target.is_symlink()  # left alone, not unlinked


def test_planted_symlink_is_refused_even_with_force(tmp_path: Path) -> None:
    decoy = tmp_path / "victim.txt"
    decoy.write_text("do not touch\n")
    target = tmp_path / "report.md"
    target.symlink_to(decoy)

    with pytest.raises(ReportPathError, match="symlink"):
        write_report("x", target, tmp_path, force=True)

    assert decoy.read_text() == "do not touch\n"
    assert target.is_symlink()


def test_confinement_refuses_dotdot_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / ".." / "escape.md"
    with pytest.raises(ReportPathError, match="outside output root"):
        write_report("x", target, root)
    assert not (tmp_path / "escape.md").exists()


def test_confinement_refuses_absolute_path_elsewhere(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = elsewhere / "escape.md"
    with pytest.raises(ReportPathError, match="outside output root"):
        write_report("x", target, root)
    assert not target.exists()


def test_confinement_refuses_parent_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    # A symlinked subdirectory inside root that points outside it.
    (root / "link").symlink_to(outside)
    target = root / "link" / "report.md"
    with pytest.raises(ReportPathError, match="outside output root"):
        write_report("x", target, root)
    assert not (outside / "report.md").exists()


def test_path_inside_explicit_out_root_is_accepted(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "report.md"
    write_report("ok", target, root)
    assert target.read_text() == "ok"


def test_existing_regular_file_refused_without_force(tmp_path: Path) -> None:
    target = tmp_path / "report.md"
    target.write_text("old")
    with pytest.raises(ReportPathError, match="--force"):
        write_report("new", target, tmp_path)
    assert target.read_text() == "old"


def test_existing_regular_file_overwritten_with_force(tmp_path: Path) -> None:
    target = tmp_path / "report.md"
    target.write_text("old-and-longer")
    write_report("new", target, tmp_path, force=True)
    assert target.read_text() == "new"  # fully replaced, not partially truncated


def test_existing_directory_refused_with_and_without_force(tmp_path: Path) -> None:
    target = tmp_path / "report.md"
    target.mkdir()
    with pytest.raises(ReportPathError):
        write_report("x", target, tmp_path)
    # With --force the open itself fails EISDIR before any fstat/truncate; the
    # directory is refused all the same and never written into.
    with pytest.raises(ReportPathError):
        write_report("x", target, tmp_path, force=True)
    assert target.is_dir()


def test_existing_fifo_refused_with_and_without_force(tmp_path: Path) -> None:
    target = tmp_path / "report.md"
    os.mkfifo(target)
    with pytest.raises(ReportPathError):
        write_report("x", target, tmp_path)
    with pytest.raises(ReportPathError):
        write_report("x", target, tmp_path, force=True)
    assert not target.is_file()


def test_fifo_with_reader_refused_on_the_fd_itself(tmp_path: Path) -> None:
    # A reader keeps the O_WRONLY|O_NONBLOCK open from failing, so the write
    # open succeeds and only the fstat-on-fd check refuses it - the branch that
    # guarantees a non-regular target is never truncated even under --force.
    target = tmp_path / "report.md"
    os.mkfifo(target)
    reader = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(ReportPathError, match="not a regular file"):
            write_report("x", target, tmp_path, force=True)
    finally:
        os.close(reader)
    assert not target.is_file()


def test_missing_parent_directory_is_a_clean_refusal(tmp_path: Path) -> None:
    target = tmp_path / "nope" / "report.md"
    with pytest.raises(ReportPathError):
        write_report("x", target, tmp_path)
    assert not target.exists()


def test_force_refuses_hard_link_to_outside_file(tmp_path: Path) -> None:
    # A hard link at the target pointing at a sensitive file IS a regular,
    # non-symlink file: O_NOFOLLOW + S_ISREG alone would truncate it. --force
    # must refuse it (st_nlink != 1) and leave the linked file untouched.
    victim = tmp_path / "victim.secret"
    victim.write_text("do not touch\n")
    target = tmp_path / "report.md"
    os.link(victim, target)  # hard link: same inode, not a symlink

    with pytest.raises(ReportPathError, match="hard link"):
        write_report("pwned", target, tmp_path, force=True)

    assert victim.read_text() == "do not touch\n"  # inode never truncated
    assert target.read_text() == "do not touch\n"  # still the same inode


def test_parent_symlink_swapped_in_after_check_is_caught_at_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate the parent-directory TOCTOU: the confinement check (realpath) is
    # forced to report a benign in-root parent, as if the symlink were swapped
    # in only AFTER the check. The dir-fd walk (O_NOFOLLOW) must still catch the
    # escape at open time, so nothing is written outside the root.
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)  # the "raced-in" symlink parent
    target = root / "link" / "report.md"

    real_realpath = os.path.realpath

    def fake_realpath(path: object, *args: object, **kwargs: object) -> str:
        # Pretend root/link resolves to a real directory under root (the state
        # the attacker showed the checker), while the filesystem still has it
        # as a symlink for the actual open to trip over.
        if os.fspath(path) == os.fspath(root / "link"):  # type: ignore[arg-type]
            return os.fspath(root / "link")
        return real_realpath(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os.path, "realpath", fake_realpath)

    with pytest.raises(ReportPathError):
        write_report("x", target, root)

    assert not (outside / "report.md").exists()  # escape never happened


def test_render_markdown_starts_with_heading_and_keeps_lines_separate() -> None:
    body = "Window: a .. b\nPeak CPU: 1.0%\n\nNearest sample: c"
    rendered = render_markdown(body)
    assert rendered.startswith("#")
    # Each non-blank body line gets a hard break (two trailing spaces) so the
    # two adjacent lines do not collapse into one Markdown paragraph.
    assert "Window: a .. b  \nPeak CPU: 1.0%  \n" in rendered
    # Blank lines stay blank (paragraph separators), not hard-broken.
    assert "  \n\nNearest sample" in rendered
