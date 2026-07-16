from __future__ import annotations

from pathlib import Path

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
