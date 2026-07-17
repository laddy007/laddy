"""Anti-pattern fixture for python-open-without-nofollow-or-excl (Rule A).

Reintroduces the first dogfood class: a write-open on a caller-supplied path
whose flags carry NEITHER O_NOFOLLOW NOR O_EXCL, so a final-component symlink
swapped in after any check is followed and the write lands on its target
(parent-directory TOCTOU). The rule must fire here (AC1).

Lives under tests/ so semgrep's built-in default ignore keeps it out of the
gate's `semgrep ... .` scan; the rule test targets it explicitly (an explicit
target overrides the ignore).
"""

from __future__ import annotations

import os


def write_unguarded(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
