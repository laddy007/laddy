"""FastMCP entrypoint and the pure ``save_note`` handler.

``handle_save_note`` is the testable core (auth -> validate -> write, with the
clock injected). ``build_server`` wraps it in a FastMCP ``@tool`` whose thin
transport layer supplies ``now=time.time()``. The process binds plain HTTP on
the configured host/port; a reverse proxy terminates TLS in front on 8443 (see
``note_server/README.md``).
"""

from __future__ import annotations

import os
import time

from mcp.server.fastmcp import FastMCP

from note_server.config import NoteConfig
from note_server.totp import SECRET_B32, decode_secret, verify
from note_server.writer import WriteError, validate_project_name, write_note

# Decoded once at import; the secret is a module constant, not per-request state.
_SECRET_KEY = decode_secret(SECRET_B32)

SERVER_NAME = "note-server"


def handle_save_note(
    cfg: NoteConfig,
    token: str,
    project_name: str,
    content: str,
    *,
    now: float,
) -> str:
    """Auth -> validate -> write. Returns a user-facing result string.

    Error strings name which check failed and never contain the notes folder's
    absolute path. No file is written unless auth AND validation both pass.
    """
    if not verify(token, _SECRET_KEY, now=now):
        return "Error: authentication failed - TOTP token invalid for the current time window."
    if not validate_project_name(project_name):
        return (
            "Error: invalid project_name - must match ^[A-Za-z0-9_-]+$ "
            "(letters, digits, underscore, hyphen only; no dots, slashes, or spaces)."
        )
    try:
        filename = write_note(cfg.notes_folder, project_name, content)
    except WriteError:
        return "Error: write failed - the note could not be saved."
    return f"Saved note as {filename}"


def build_server(cfg: NoteConfig) -> FastMCP:
    """Build the FastMCP app with the single ``save_note`` tool registered."""
    mcp = FastMCP(SERVER_NAME, host=cfg.host, port=cfg.port)

    @mcp.tool()
    def save_note(token: str, project_name: str, content: str) -> str:
        """Save a note to a server-side folder after verifying a TOTP token.

        Args:
            token: current TOTP code (RFC 6238, 6 digits).
            project_name: base filename; must match ^[A-Za-z0-9_-]+$.
            content: note body, written verbatim.
        """
        return handle_save_note(cfg, token, project_name, content, now=time.time())

    return mcp


def main() -> None:
    cfg = NoteConfig.from_env(os.environ)
    server = build_server(cfg)
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
