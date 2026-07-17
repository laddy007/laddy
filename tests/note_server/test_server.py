from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from note_server.config import NoteConfig
from note_server.server import build_server, handle_save_note
from note_server.totp import decode_secret, totp

FIXED_NOW = 1_000_000_000.0
# Test-only key (not a live credential); the real secret is injected via env.
TEST_KEY_B32 = "NZXXIZJNONSXE5TFOIWXIZLTOQWWWZLZ"
KEY = decode_secret(TEST_KEY_B32)


def _cfg(folder: Path) -> NoteConfig:
    return NoteConfig(
        notes_folder=folder, host="127.0.0.1", port=8080, totp_secret=KEY
    )


def _valid_token() -> str:
    return totp(KEY, FIXED_NOW)


# --- AC1: tool surface ----------------------------------------------------


def test_registers_exactly_one_save_note_tool(tmp_path: Path) -> None:
    server = build_server(_cfg(tmp_path))
    tools = asyncio.run(server.list_tools())
    assert [t.name for t in tools] == ["save_note"]
    schema = tools[0].inputSchema
    assert set(schema["required"]) == {"token", "project_name", "content"}
    for param in ("token", "project_name", "content"):
        assert schema["properties"][param]["type"] == "string"


# --- AC4: happy-path write ------------------------------------------------


def test_happy_path_writes_verbatim_and_returns_filename(tmp_path: Path) -> None:
    result = handle_save_note(
        _cfg(tmp_path), _valid_token(), "myproject", "body text", now=FIXED_NOW
    )
    assert "myproject.md" in result
    assert (tmp_path / "myproject.md").read_text(encoding="utf-8") == "body text"


# --- AC5: no-clobber dedup ------------------------------------------------


def test_dedup_returns_new_name_and_keeps_original(tmp_path: Path) -> None:
    (tmp_path / "myproject.md").write_text("ORIGINAL", encoding="utf-8")
    result = handle_save_note(
        _cfg(tmp_path), _valid_token(), "myproject", "new body", now=FIXED_NOW
    )
    assert "myproject-2.md" in result
    assert (tmp_path / "myproject-2.md").read_text(encoding="utf-8") == "new body"
    assert (tmp_path / "myproject.md").read_text(encoding="utf-8") == "ORIGINAL"


# --- AC2: auth rejection writes nothing -----------------------------------


def test_bad_token_rejected_and_writes_nothing(tmp_path: Path) -> None:
    result = handle_save_note(
        _cfg(tmp_path), "000000", "myproject", "body", now=FIXED_NOW
    )
    assert "authentication failed" in result
    assert list(tmp_path.iterdir()) == []


def test_two_step_drift_token_rejected(tmp_path: Path) -> None:
    stale = totp(KEY, FIXED_NOW - 60)  # window -2
    result = handle_save_note(
        _cfg(tmp_path), stale, "myproject", "body", now=FIXED_NOW
    )
    assert "authentication failed" in result
    assert list(tmp_path.iterdir()) == []


# --- AC3: project_name guard rejects & writes nothing ---------------------


@pytest.mark.parametrize("name", ["", "foo.md", "a/b", "../x", "a b"])
def test_invalid_project_name_rejected_and_writes_nothing(
    tmp_path: Path, name: str
) -> None:
    result = handle_save_note(
        _cfg(tmp_path), _valid_token(), name, "body", now=FIXED_NOW
    )
    assert "invalid project_name" in result
    assert list(tmp_path.iterdir()) == []


# --- AC6: error responses never leak the folder path ----------------------


def test_error_messages_never_contain_folder_path(tmp_path: Path) -> None:
    folder = str(tmp_path)
    auth_err = handle_save_note(_cfg(tmp_path), "000000", "p", "b", now=FIXED_NOW)
    validation_err = handle_save_note(
        _cfg(tmp_path), _valid_token(), "../x", "b", now=FIXED_NOW
    )
    for message in (auth_err, validation_err):
        assert folder not in message


def test_write_error_response_is_clean_and_hides_folder_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A genuine OS write failure (read-only folder / full disk) must yield a
    # clean write-error response that names the failing check and never contains
    # the folder's absolute path - the raw errno string leaks it (AC6).
    def boom(path: object, *args: object, **kwargs: object) -> int:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr("note_server.writer.os.open", boom)
    result = handle_save_note(
        _cfg(tmp_path), _valid_token(), "proj", "body", now=FIXED_NOW
    )
    assert "write" in result.lower()
    assert str(tmp_path) not in result


def test_call_tool_write_failure_never_leaks_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end guard: even through FastMCP, an OS write failure must not
    # surface a ToolError disclosing the absolute path. The handler returns a
    # clean string, so call_tool completes with no exception.
    def boom(path: object, *args: object, **kwargs: object) -> int:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr("note_server.writer.os.open", boom)
    server = build_server(_cfg(tmp_path))
    token = totp(KEY, time.time())
    result = asyncio.run(
        server.call_tool(
            "save_note",
            {"token": token, "project_name": "proj", "content": "body"},
        )
    )
    assert str(tmp_path) not in repr(result)


def test_success_message_names_which_check_and_only_the_basename(
    tmp_path: Path,
) -> None:
    result = handle_save_note(
        _cfg(tmp_path), _valid_token(), "proj", "b", now=FIXED_NOW
    )
    assert str(tmp_path) not in result
    assert "proj.md" in result


# --- End-to-end: drive the registered tool through FastMCP (real clock) ----


def test_call_tool_end_to_end_round_trip(tmp_path: Path) -> None:
    server = build_server(_cfg(tmp_path))
    # The tool's transport wrapper uses time.time(); mint a token for the same.
    token = totp(KEY, time.time())
    # call_tool -> registered save_note wrapper -> handle_save_note -> write_note.
    # The file side-effect proves the whole wiring; the returned message content
    # is asserted by the handle_save_note unit tests above. (call_tool's declared
    # return is a Sequence|dict union, so we assert the observable side-effect.)
    asyncio.run(
        server.call_tool(
            "save_note",
            {"token": token, "project_name": "e2e", "content": "round trip"},
        )
    )
    assert (tmp_path / "e2e.md").read_text(encoding="utf-8") == "round trip"
