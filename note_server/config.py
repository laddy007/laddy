"""Typed ``note_server`` configuration from environment variables.

The notes folder and the plain-HTTP bind host/port are all required with no
default: startup fails clearly and non-silently if any is unset (or the folder
is missing). Mirrors the ``ConfigError`` / ``from_env`` idiom in
``orchestrator/config.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

FOLDER_ENV = "NOTE_SERVER_FOLDER"
HOST_ENV = "NOTE_SERVER_HOST"
PORT_ENV = "NOTE_SERVER_PORT"


class ConfigError(ValueError):
    """Invalid note_server configuration."""


@dataclass(frozen=True)
class NoteConfig:
    notes_folder: Path
    host: str
    port: int

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

        return cls(notes_folder=folder, host=host, port=port)
