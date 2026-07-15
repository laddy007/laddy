"""Guarded file output for loop-monitor reports.

Once the autonomous loop (not a human at a keyboard) chooses where a report
lands, that destination is untrusted input: a `../` path, an absolute path
elsewhere, or a planted symlink could redirect the write onto `config.toml`,
an `.env`, a lockfile, or any file outside the data dir. This module validates
the destination *before any byte is written* and performs the write without
ever following a symlink, mirroring the ``O_NOFOLLOW`` hardening in
``orchestrator/queue.py`` (commit ``b82394a``).

Two subtleties beyond a single ``O_NOFOLLOW`` open:

* **Parent-directory TOCTOU.** ``O_NOFOLLOW`` only guards the *final* path
  component. Resolving the parent with ``realpath`` and then re-opening that
  string is racy: an attacker who swaps an intermediate directory for a symlink
  between the check and the open redirects the write outside the root. So the
  write walks the parent one component at a time from a root directory fd, each
  step an ``openat`` with ``O_NOFOLLOW`` — no symlink is followed at any depth,
  and the final ``openat`` is relative to a pinned fd, never a re-traversed path.

* **Hard links.** ``--force`` overwrites an existing regular file, but a hard
  link to a sensitive file *is* a regular file and is not a symlink, so
  ``O_NOFOLLOW`` + ``S_ISREG`` alone would truncate it. The force path also
  refuses any target with ``st_nlink != 1``.

``O_NOFOLLOW``/``O_DIRECTORY`` and ``openat`` (``dir_fd=``) are POSIX/Linux; the
monitor is Linux/VPS-only, so there is no portability concern (the queue module
relies on the same guarantee).
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


def _open_parent_dirfd(root: Path, rel_parts: tuple[str, ...], out: Path) -> int:
    """Open the target's parent directory as a fd, walking from ``root``.

    Each component is an ``openat`` (``dir_fd=``) with ``O_DIRECTORY |
    O_NOFOLLOW``, so no symlink is followed at any depth. This is what closes
    the parent-directory TOCTOU: even a component swapped to a symlink *after*
    the confinement check fails ``ELOOP`` here instead of redirecting the write.
    ``root`` itself is the trusted output root (config), not attacker-chosen, so
    only the components beneath it (from the untrusted ``out``) need walking.
    """
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in rel_parts:
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd
            )
            os.close(dir_fd)
            dir_fd = next_fd
    except OSError as exc:
        os.close(dir_fd)
        _refuse(out, exc)
        raise  # pragma: no cover - _refuse always raises
    return dir_fd


def _open_existing_for_force(dir_fd: int, name: str) -> int:
    """Open a pre-existing target (relative to ``dir_fd``) for overwrite.

    Reached only under ``--force``. Checks are on the *fd itself* (``fstat``)
    and the truncate is deferred until after them, so a directory/fifo/symlink
    is refused without ever being truncated and there is no TOCTOU window
    between "is it safe?" and "truncate it". ``O_NOFOLLOW`` turns a swapped-in
    symlink into ELOOP; ``O_NONBLOCK`` stops an ``O_WRONLY`` open of a fifo from
    blocking on a missing reader (it fails ENXIO instead, refused all the same).
    A hard link to a file elsewhere *is* a regular non-symlink file, so
    ``st_nlink != 1`` is refused too — otherwise ``--force`` could truncate an
    arbitrary same-filesystem file through a planted hard link.
    """
    try:
        fd = os.open(
            name, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd
        )
    except OSError as exc:
        _refuse(Path(name), exc)
        raise  # pragma: no cover - _refuse always raises
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ReportPathError(
                f"refusing {name}: existing target is not a regular file"
            )
        if info.st_nlink != 1:
            raise ReportPathError(
                f"refusing {name}: existing target has multiple hard links "
                "(possible hard-link attack) - refusing to overwrite"
            )
        os.ftruncate(fd, 0)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_target(dir_fd: int, name: str, *, force: bool) -> int:
    """Create (or, under force, overwrite) ``name`` relative to ``dir_fd``.

    ``O_CREAT | O_EXCL | O_NOFOLLOW`` is the TOCTOU-free create: a pre-existing
    file (including a planted symlink) fails EEXIST. Drop O_EXCL only under
    ``--force``, and even then refuse anything that is not a single-link
    regular file.
    """
    try:
        return os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=dir_fd,
        )
    except FileExistsError:
        if not force:
            existing = os.lstat(name, dir_fd=dir_fd)
            kind = (
                "file exists (pass --force to overwrite)"
                if stat.S_ISREG(existing.st_mode)
                else "target exists and is not a regular file"
            )
            raise ReportPathError(f"refusing {name}: {kind}") from None
        return _open_existing_for_force(dir_fd, name)
    except OSError as exc:
        _refuse(Path(name), exc)
        raise  # pragma: no cover - _refuse always raises


def write_report(
    text: str, out: Path, out_root: Path, *, force: bool = False
) -> None:
    """Write ``text`` to ``out``, enforcing the path-guard rules.

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

    # 2. Static confinement: resolve the parent and require it beneath the
    # output root. This gives the friendly "outside output root" message and
    # rejects `../`, an absolute path elsewhere, and a statically-present
    # parent symlink that escapes. It is NOT relied on for the write itself
    # (that would be a TOCTOU) - see step 3.
    root = Path(os.path.realpath(out_root))
    real_parent = Path(os.path.realpath(out.parent))
    if not real_parent.is_relative_to(root):
        raise ReportPathError(
            f"refusing {out}: resolves outside output root {root}"
        )

    # 3. Race-free open: walk the parent's components from a root dir-fd with
    # O_NOFOLLOW, then openat the final name. The realpath above only informs
    # the confinement decision; the write never re-traverses a path string, so
    # a component swapped to a symlink after the check cannot redirect it.
    rel_parts = real_parent.relative_to(root).parts
    dir_fd = _open_parent_dirfd(root, rel_parts, out)
    try:
        fd = _open_target(dir_fd, out.name, force=force)
    finally:
        os.close(dir_fd)

    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
