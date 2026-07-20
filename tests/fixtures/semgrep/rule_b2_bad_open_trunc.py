"""Anti-pattern fixture for python-open-trunc-without-nlink-check (Rule B2).

The O_TRUNC-on-open idiom of the same force-overwrite hard-link hole Rule B
closes for bare os.ftruncate: no st_nlink check before trusting the truncate,
so a hard link planted at the target is truncated in place. The rule must
fire here (AC1).
"""

from __future__ import annotations

import os


def force_overwrite_via_open_trunc(dir_fd: int, name: str) -> int:
    return os.open(name, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, dir_fd=dir_fd)
