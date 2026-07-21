"""Positive control: O_CREAT|O_EXCL is TOCTOU-free, so Rule A must stay silent.

This canary used to live in the corpus (queue.py's exclusive lock-create). The
flow rework replaced that lock with an flock reclaim guard, taking the only
O_CREAT|O_EXCL open in the repo with it - so the "Rule A does not fire on a
legitimate exclusive create" coverage is pinned here instead of depending on
whichever corpus code happens to carry the pattern.

O_EXCL WITH O_CREAT refuses to open an existing path at all, symlink included,
so there is no final-component swap to lose - unlike the bare-O_EXCL bypass in
rule_a_bad_write_open_excl_without_creat.py, where O_EXCL without O_CREAT is a
POSIX no-op and the rule MUST fire.
"""

from __future__ import annotations

import os


def create_exclusively(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
