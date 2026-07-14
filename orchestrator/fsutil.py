"""Filesystem primitives shared across the orchestrator.

One home for the OS-portability footguns the orchestrator hits when it
removes git trees. The loop runs on the Linux VPS, but its unit suite runs
on the Director's Windows box too, where git pack/object files are marked
read-only and a naive ``shutil.rmtree`` refuses to unlink them.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from collections.abc import Callable
from pathlib import Path


def _retry_readonly(func: Callable[..., object], path: str, _exc: object) -> None:
    """rmtree error handler: clear the read-only bit and retry the op.

    Git objects/packs are written read-only; on Windows ``os.unlink`` then
    raises ``PermissionError``. Setting the write bit and retrying the exact
    failed op removes them. On POSIX this never fires (unlink needs only the
    parent dir's write bit), so it is a harmless no-op there.
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_tree(path: Path) -> None:
    """Remove a directory tree, clearing read-only bits along the way.

    A missing path is a no-op. Unlike ``shutil.rmtree(ignore_errors=True)``
    a genuine failure PROPAGATES - a cleanup that cannot clean must raise,
    not silently leave the tree behind and lie that it removed it.
    """
    if not path.exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_retry_readonly)
    else:
        shutil.rmtree(path, onerror=_retry_readonly)
