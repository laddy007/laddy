"""Anti-pattern fixture for python-ftruncate-without-nlink-check (Rule B).

Reintroduces the second dogfood class: os.ftruncate(fd, 0) inside a function
that never checks st_nlink, so a hard link planted at the target passes
O_NOFOLLOW + S_ISREG and the truncate lands on the linked file (the --force
hard-link overwrite hole report_path.py closes). The rule must fire here (AC1).

Lives under tests/ so semgrep's default ignore keeps it out of the gate scan;
the rule test targets it explicitly.
"""

from __future__ import annotations

import os


def force_overwrite(dir_fd: int, name: str) -> int:
    fd = os.open(name, os.O_WRONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    os.ftruncate(fd, 0)
    return fd
