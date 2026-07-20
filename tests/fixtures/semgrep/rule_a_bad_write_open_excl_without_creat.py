"""Adversarial fixture: Rule A bypass via O_EXCL without O_CREAT (a POSIX no-op)."""

from __future__ import annotations

import os


def write_unguarded_bare_excl(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_EXCL, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
