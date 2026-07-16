"""``project_name`` allowlist guard and the race-free, no-clobber note writer."""

from __future__ import annotations

import os
import re
from pathlib import Path

# Allowlist (NOT a denylist): the authoritative set of legal project names.
# No slashes, dots, whitespace, "..", or anything else - this is the
# path-traversal guard. Empty is rejected (the + quantifier requires >= 1 char).
PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Upper bound on dedup attempts so a pathological folder can't spin forever.
MAX_DEDUP_ATTEMPTS = 10_000


class ProjectNameError(ValueError):
    """``project_name`` failed the allowlist guard."""


class WriteError(OSError):
    """The note could not be written to a fresh file."""


def validate_project_name(name: str) -> bool:
    """Return True iff ``name`` matches ``^[A-Za-z0-9_-]+$``."""
    return PROJECT_NAME_RE.fullmatch(name) is not None


def _candidate_names(project_name: str, limit: int) -> list[str]:
    """``name.md`` then ``name-2.md``, ``name-3.md`` … up to ``limit`` total."""
    names = [f"{project_name}.md"]
    names += [f"{project_name}-{i}.md" for i in range(2, limit + 1)]
    return names


def write_note(
    folder: Path,
    project_name: str,
    content: str,
    *,
    limit: int = MAX_DEDUP_ATTEMPTS,
) -> str:
    """Write ``content`` to a fresh ``.md`` file in ``folder``; return the basename.

    Guards ``project_name`` against the allowlist first (raising
    ``ProjectNameError`` before touching the filesystem), then creates the file
    race-free with ``O_CREAT | O_EXCL``, retrying ``name-2.md``, ``name-3.md`` …
    on collision so an existing file is never overwritten. Raises ``WriteError``
    if the resolved path would escape ``folder`` (defence in depth) or if no free
    name is found within ``limit`` attempts.
    """
    if not validate_project_name(project_name):
        raise ProjectNameError("project_name failed the allowlist guard")

    base = folder.resolve()
    for candidate in _candidate_names(project_name, limit):
        path = (base / candidate).resolve()
        # Defence in depth: the regex already forbids separators, but confirm the
        # resolved write path stays directly inside the configured folder.
        if path.parent != base:
            raise WriteError("resolved note path escaped the notes folder")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        return candidate

    raise WriteError("no free filename found within the dedup limit")
