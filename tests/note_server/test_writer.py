from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import pytest

from note_server.writer import (
    ProjectNameError,
    WriteError,
    validate_project_name,
    write_note,
)


@pytest.mark.parametrize("name", ["a", "Project_1", "my-note", "ABC-123_x"])
def test_validate_accepts_allowlisted_names(name: str) -> None:
    assert validate_project_name(name) is True


@pytest.mark.parametrize(
    "name",
    ["", "foo.md", "a/b", "../x", "a b", "a\tb", "naïve", "a.b", "..", "a\\b"],
)
def test_validate_rejects_everything_else(name: str) -> None:
    assert validate_project_name(name) is False


def test_write_note_happy_path(tmp_path: Path) -> None:
    result = write_note(tmp_path, "notes", "hello world")
    assert result == "notes.md"
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "hello world"


def test_write_note_dedups_without_clobbering(tmp_path: Path) -> None:
    original = tmp_path / "notes.md"
    original.write_text("ORIGINAL", encoding="utf-8")

    result = write_note(tmp_path, "notes", "second")
    assert result == "notes-2.md"
    assert (tmp_path / "notes-2.md").read_text(encoding="utf-8") == "second"
    # The pre-existing file is left completely untouched.
    assert original.read_text(encoding="utf-8") == "ORIGINAL"

    third = write_note(tmp_path, "notes", "third")
    assert third == "notes-3.md"


def test_write_note_rejects_invalid_name_and_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(ProjectNameError):
        write_note(tmp_path, "../escape", "payload")
    # Nothing at all was created in the folder.
    assert list(tmp_path.iterdir()) == []


def test_write_note_exhausts_dedup_limit(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("a", encoding="utf-8")
    (tmp_path / "notes-2.md").write_text("b", encoding="utf-8")
    with pytest.raises(WriteError):
        write_note(tmp_path, "notes", "c", limit=2)


def test_write_note_wraps_os_error_as_path_free_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A genuine OS failure at create time (read-only folder / full disk) must be
    # re-raised as a WriteError whose message never contains the absolute path -
    # not leak the raw errno string (which does) to the caller. See AC6.
    def boom(path: object, *args: object, **kwargs: object) -> int:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr("note_server.writer.os.open", boom)
    with pytest.raises(WriteError) as excinfo:
        write_note(tmp_path, "notes", "body")
    assert str(tmp_path) not in str(excinfo.value)


def test_write_note_wraps_write_time_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The file is created but the write itself fails (e.g. ENOSPC): still a
    # path-free WriteError, not a raw OSError escaping the writer.
    import os

    def boom_fdopen(fd: int, *args: object, **kwargs: object) -> NoReturn:
        os.close(fd)  # release the descriptor write_note handed us
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("note_server.writer.os.fdopen", boom_fdopen)
    with pytest.raises(WriteError) as excinfo:
        write_note(tmp_path, "notes", "body")
    assert str(tmp_path) not in str(excinfo.value)
    # The empty file O_CREAT placed at the slot must be cleaned up, not left as
    # derelict debris that permanently occupies the name.
    assert list(tmp_path.iterdir()) == []


def test_write_note_failed_write_does_not_shift_retry_to_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # After a write-time failure, the slot is free again: a retry of the same
    # project_name must reuse {name}.md, not silently spill onto {name}-2.md.
    import os

    def boom_fdopen(fd: int, *args: object, **kwargs: object) -> NoReturn:
        os.close(fd)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("note_server.writer.os.fdopen", boom_fdopen)
    with pytest.raises(WriteError):
        write_note(tmp_path, "notes", "first")

    monkeypatch.undo()  # let the retry actually write
    assert write_note(tmp_path, "notes", "second") == "notes.md"
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "second"
    assert not (tmp_path / "notes-2.md").exists()
