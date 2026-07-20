"""Adversarial fixture: Rule B bypass via a no-op st_nlink mention."""

from __future__ import annotations

import os


def force_overwrite_fake_guard(dir_fd: int, name: str) -> int:
    fd = os.open(name, os.O_WRONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    info = os.fstat(fd)
    print("nlink is", info.st_nlink)
    os.ftruncate(fd, 0)
    return fd
