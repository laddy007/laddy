"""Adversarial fixture: Rule B bypass via an inverted/vacuous st_nlink guard."""

from __future__ import annotations

import os


def force_overwrite_inverted_guard(dir_fd: int, name: str) -> int:
    fd = os.open(name, os.O_WRONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    info = os.fstat(fd)
    if info.st_nlink == 1:
        raise ValueError("backwards on purpose - should not suppress the rule")
    os.ftruncate(fd, 0)
    return fd
