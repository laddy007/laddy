"""Adversarial fixture: Rule A bypass via variable-bound flags."""

from __future__ import annotations

import os


def write_unguarded_via_variable(path: str, data: bytes) -> None:
    flags = os.O_CREAT | os.O_WRONLY
    fd = os.open(path, flags, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
