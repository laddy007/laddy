"""Typed ``note_server`` configuration from environment variables.

The notes folder, the plain-HTTP bind host/port, and the base32 TOTP secret are
all required with no default: startup fails clearly and non-silently if any is
unset (or the folder is missing, or the secret is not valid base32). Mirrors the
``ConfigError`` / ``from_env`` idiom in ``orchestrator/config.py``.
"""

from __future__ import annotations

import binascii
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from note_server.totp import decode_secret

FOLDER_ENV = "NOTE_SERVER_FOLDER"
HOST_ENV = "NOTE_SERVER_HOST"
PORT_ENV = "NOTE_SERVER_PORT"
SECRET_ENV = "NOTE_SERVER_TOTP_SECRET"


class ConfigError(ValueError):
    """Invalid note_server configuration."""


@dataclass(frozen=True)
class NoteConfig:
    notes_folder: Path
    host: str
    port: int
    # Decoded TOTP key bytes. repr-suppressed so the secret never lands in a
    # traceback, log line, or REPL echo of the config object.
    totp_secret: bytes = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> NoteConfig:
        folder_raw = env.get(FOLDER_ENV)
        if not folder_raw:
            raise ConfigError(
                f"{FOLDER_ENV} is required (no default): the server-side notes folder"
            )
        folder = Path(folder_raw)
        if not folder.is_dir():
            raise ConfigError(
                f"{FOLDER_ENV} must point to an existing directory"
            )

        host = env.get(HOST_ENV)
        if not host:
            raise ConfigError(
                f"{HOST_ENV} is required (no default): the plain-HTTP bind host"
            )

        port_raw = env.get(PORT_ENV)
        if not port_raw:
            raise ConfigError(
                f"{PORT_ENV} is required (no default): the plain-HTTP bind port"
            )
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ConfigError(f"{PORT_ENV} must be an integer: {exc}") from exc
        if not (1 <= port <= 65535):
            raise ConfigError(f"{PORT_ENV} must be in 1..65535")

        secret_raw = env.get(SECRET_ENV)
        if not secret_raw:
            raise ConfigError(
                f"{SECRET_ENV} is required (no default): the base32 TOTP shared secret"
            )
        try:
            totp_secret = decode_secret(secret_raw)
        except binascii.Error as exc:
            raise ConfigError(f"{SECRET_ENV} must be valid base32: {exc}") from exc

        return cls(
            notes_folder=folder, host=host, port=port, totp_secret=totp_secret
        )
