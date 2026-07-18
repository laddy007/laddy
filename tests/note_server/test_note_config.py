from __future__ import annotations

from pathlib import Path

import pytest

from note_server.config import ConfigError, NoteConfig

# Test-only base32 secret (decodes to b"note-server-test-key"); not a live
# credential - the real value is injected via NOTE_SERVER_TOTP_SECRET at runtime.
TEST_SECRET_B32 = "NZXXIZJNONSXE5TFOIWXIZLTOQWWWZLZ"


def _env(folder: Path, **overrides: str) -> dict[str, str]:
    env = {
        "NOTE_SERVER_FOLDER": str(folder),
        "NOTE_SERVER_HOST": "127.0.0.1",
        "NOTE_SERVER_PORT": "8080",
        "NOTE_SERVER_TOTP_SECRET": TEST_SECRET_B32,
    }
    env.update(overrides)
    return env


def test_from_env_success(tmp_path: Path) -> None:
    cfg = NoteConfig.from_env(_env(tmp_path))
    assert cfg.notes_folder == tmp_path
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8080
    assert cfg.totp_secret == b"note-server-test-key"


def test_from_env_missing_secret(tmp_path: Path) -> None:
    env = _env(tmp_path)
    del env["NOTE_SERVER_TOTP_SECRET"]
    with pytest.raises(ConfigError, match="NOTE_SERVER_TOTP_SECRET"):
        NoteConfig.from_env(env)


def test_from_env_invalid_base32_secret(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="valid base32"):
        NoteConfig.from_env(_env(tmp_path, NOTE_SERVER_TOTP_SECRET="not-base32!!!"))


def test_from_env_missing_folder(tmp_path: Path) -> None:
    env = _env(tmp_path)
    del env["NOTE_SERVER_FOLDER"]
    with pytest.raises(ConfigError, match="NOTE_SERVER_FOLDER"):
        NoteConfig.from_env(env)


def test_from_env_nonexistent_folder(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="existing directory"):
        NoteConfig.from_env(_env(tmp_path / "does-not-exist"))


def test_from_env_missing_host(tmp_path: Path) -> None:
    env = _env(tmp_path)
    del env["NOTE_SERVER_HOST"]
    with pytest.raises(ConfigError, match="NOTE_SERVER_HOST"):
        NoteConfig.from_env(env)


def test_from_env_missing_port(tmp_path: Path) -> None:
    env = _env(tmp_path)
    del env["NOTE_SERVER_PORT"]
    with pytest.raises(ConfigError, match="NOTE_SERVER_PORT"):
        NoteConfig.from_env(env)


def test_from_env_non_integer_port(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="must be an integer"):
        NoteConfig.from_env(_env(tmp_path, NOTE_SERVER_PORT="not-a-port"))


def test_from_env_out_of_range_port(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="1..65535"):
        NoteConfig.from_env(_env(tmp_path, NOTE_SERVER_PORT="70000"))
