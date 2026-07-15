"""Guarded file output for loop-monitor reports.

Once the autonomous loop (not a human at a keyboard) chooses where a report
lands, that destination is untrusted input: a `../` path, an absolute path
elsewhere, or a planted symlink could redirect the write onto `config.toml`,
an `.env`, a lockfile, or any file outside the data dir. This module validates
the destination *before any byte is written* and performs the write without
ever following a symlink, mirroring the ``O_NOFOLLOW`` hardening in
``orchestrator/queue.py`` (commit ``b82394a``).

``O_NOFOLLOW`` is POSIX-only; the monitor is Linux/VPS-only, so there is no
portability concern (the queue module relies on the same guarantee).
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path


class ReportPathError(Exception):
    """A report output path was refused by the guard; nothing was written."""


def render_markdown(body: str) -> str:
    """Wrap a plain-text report body as minimally valid Markdown.

    ``build_report`` joins its lines with single ``\\n``, which Markdown
    collapses into one run-on paragraph. The smallest fix that renders on
    GitHub / in a PR: prepend a ``#`` heading and append a hard line break
    (two trailing spaces) to each non-blank line so single newlines survive,
    leaving blank lines as paragraph separators. The body itself is not
    modified, so stdout output (which never calls this) stays byte-for-byte
    identical.
    """
    rendered = [line + "  " if line.strip() else line for line in body.split("\n")]
    return "# loop-monitor report\n\n" + "\n".join(rendered) + "\n"


def _refuse(target: Path, exc: OSError) -> None:
    """Translate a raw OSError from the open/write path into a clean refusal.

    Nothing raw escapes the guard: every failure becomes a ReportPathError so
    the CLI can print a single-line reason and exit non-zero.
    """
    if exc.errno == errno.ELOOP:
        raise ReportPathError(
            f"refusing {target}: path is a symlink - refusing to follow it "
            "(possible attack or corrupted state)"
        ) from exc
    reason = os.strerror(exc.errno) if exc.errno is not None else str(exc)
    raise ReportPathError(f"refusing {target}: {reason}") from exc


def _open_existing_for_force(target: Path) -> int:
    """Open a pre-existing target for overwrite, refusing anything non-regular.

    Reached only under ``--force``. The check is done on the *fd itself*
    (``fstat``) and the truncate is deferred until after that check, so a
    directory/fifo/symlink is refused without ever being truncated and there
    is no TOCTOU window between "is it regular?" and "truncate it". ``O_NOFOLLOW``
    turns a swapped-in symlink into ELOOP; ``O_NONBLOCK`` stops an ``O_WRONLY``
    open of a fifo from blocking on a missing reader (it fails ENXIO instead,
    which is refused all the same).
    """
    try:
        fd = os.open(target, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        _refuse(target, exc)
        raise  # pragma: no cover - _refuse always raises
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ReportPathError(
                f"refusing {target}: existing target is not a regular file"
            )
        os.ftruncate(fd, 0)
    except BaseException:
        os.close(fd)
        raise
    return fd


def write_report(
    text: str, out: Path, out_root: Path, *, force: bool = False
) -> None:
    """Write ``text`` to ``out``, enforcing the four path-guard rules.

    All checks precede any byte written, so a rejection leaves the filesystem
    untouched. Raises ReportPathError on any refusal.
    """
    out = Path(out)
    out_root = Path(out_root)

    # 1. `.md` suffix required (also rejects a bare ".md" with no stem).
    if out.suffix != ".md" or out.stem == "":
        raise ReportPathError(
            f"refusing {out}: output file name must end in .md"
        )

    # 2/3. Confinement. Resolve the *parent* (not the final component): this
    # normalizes `../` and catches a parent-symlink escape, while leaving the
    # final component for O_NOFOLLOW to guard. A missing parent normalizes fine
    # here and surfaces later as ENOENT at open (mapped to a clean refusal).
    root = Path(os.path.realpath(out_root))
    real_parent = Path(os.path.realpath(out.parent))
    if not real_parent.is_relative_to(root):
        raise ReportPathError(
            f"refusing {out}: resolves outside output root {root}"
        )
    target = real_parent / out.name

    # 4. No clobber. O_CREAT | O_EXCL | O_NOFOLLOW is the TOCTOU-free create:
    # a pre-existing file (including a planted symlink) fails EEXIST; drop
    # O_EXCL only under --force, and even then refuse anything non-regular.
    try:
        fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
    except FileExistsError:
        if not force:
            kind = (
                "file exists (pass --force to overwrite)"
                if stat.S_ISREG(os.lstat(target).st_mode)
                else "target exists and is not a regular file"
            )
            raise ReportPathError(f"refusing {target}: {kind}") from None
        fd = _open_existing_for_force(target)
    except OSError as exc:
        _refuse(target, exc)
        raise  # pragma: no cover - _refuse always raises

    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
