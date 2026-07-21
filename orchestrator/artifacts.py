"""Task artifact store under <agent-dir>/tasks/<task>/ (design doc S9).

Git is the source of truth for loop state; the iteration log is
append-only (one JSON line per action, never updated). The loop can
resume purely from these files after a crash.
"""

from __future__ import annotations

import errno
import json
import os
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # non-POSIX (e.g. Windows): degrade to no cross-process lock
    fcntl = None  # type: ignore[assignment]

from orchestrator import TARGET_DIR_NAME

# In-process serialization of the read-then-append log critical section, one
# lock per log path. ``flock`` defends the CROSS-process race (two flag CLI
# runs on the VPS) but exists only on POSIX; a ``threading.Lock`` defends the
# IN-process race (concurrent role threads raising flags) on every platform.
# Both are needed: on Windows flock is absent, so without this the guarantee
# (no id collision, no lost flag) would hold nowhere in-process.
_LOG_LOCKS: dict[Path, threading.Lock] = {}
_LOG_LOCKS_GUARD = threading.Lock()


def _log_lock_for(path: Path) -> threading.Lock:
    with _LOG_LOCKS_GUARD:
        return _LOG_LOCKS.setdefault(path, threading.Lock())

# Artifact file names (design S9). Referenced by name everywhere else.
LOG = "iteration-log.jsonl"
RW1_VERDICT = "reviewer-a-verdict.json"
RW2_VERDICT = "reviewer-b-verdict.json"
SENIOR_VERDICT = "senior-reviewer-verdict.json"
HUMAN_SUMMARY = "human-summary.md"
HANDBACK = "handback.md"
STATE = "state.json"
AUTHORITATIVE = "test-authoritative.json"
MERGE_DECISION = "merge-decision.json"
ROLE_PLAN = "role-plan.json"
FINDINGS = "findings.json"
FINDINGS_PROPOSED = "findings-proposed.json"
REPORT = "report.md"
EXPLORATION = "exploration.md"
SPEC = "spec.md"


def utc_now() -> str:
    """The shared ``ts`` wire format of every JSONL event log (iteration
    logs, the queue, the oracle run log). One home - a drifted copy breaks
    every reader that strptime-parses the format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_jsonl(path: Path, event: dict[str, Any]) -> None:
    """Append exactly one event line (never rewrites); creates parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


class LogCorruptionError(Exception):
    """A malformed INTERIOR line was found in an append-only JSONL log.

    A completed append is always one whole line, so only the FINAL line can be
    torn by a crash. A malformed interior line is therefore real corruption,
    not a partial write - dropping it would silently hole the replayed state, so
    the reader fails closed (raises) instead of proceeding on a gap."""


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Every event, in file order; missing file = [].

    Torn-final-line tolerant, interior-strict: a crash mid-append can leave a
    torn FINAL line (that append never completed), so a malformed last line is
    skipped and never raises. A malformed INTERIOR line, by contrast, is real
    corruption - a completed append is always a whole line - so it raises
    LogCorruptionError rather than silently dropping the event. Consumers
    replay state from this log and must fail closed on a hole, never proceed.
    """
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    last = len(lines) - 1
    entries: list[dict[str, Any]] = []
    for i, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            entries.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            if i == last:
                continue  # torn final append: tolerate, keep every reader alive
            raise LogCorruptionError(
                f"{path}: malformed interior line {i + 1} - append-only log "
                "corruption (a completed append is always a whole line)"
            ) from exc
    return entries


class ArtifactPathError(Exception):
    """A task-artifact path is a symlink and was refused, not followed.

    The task dir (<agent-dir>/tasks/<task>/) and its files are BRANCH-controlled
    content: a merged branch could ship the dir - or an individual artifact - as
    a symlink pointing anywhere on the trusted merge machine, so a bare write
    would land through the link onto an arbitrary file. Every write below fails
    closed instead. Mirrors queue.py's O_NOFOLLOW lock-file discipline."""


def _refuse_symlink(path: Path, exc: OSError) -> None:
    """Re-raise an O_NOFOLLOW ELOOP as the typed refusal; pass anything else
    through unchanged (same shape as queue.py's guard for the lock file)."""
    if exc.errno == errno.ELOOP:
        raise ArtifactPathError(
            f"artifact path {path} is a symlink - refusing to follow it "
            "(possible attack or corrupted state; remove it by hand)"
        ) from exc
    raise exc


def write_text_nofollow(path: Path, text: str) -> None:
    """Write ``text`` to ``path``, refusing to follow a symlink at the final
    component. O_NOFOLLOW makes the open itself fail (ELOOP) if ``path`` is a
    symlink, rather than truncating whatever it points to - a bare
    Path.write_text() would happily write through an attacker-planted link.
    Callers are expected to have vetted the parent dir (see ``_ensure``)."""
    try:
        fd = os.open(
            path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o644
        )
    except OSError as exc:
        _refuse_symlink(path, exc)
        raise  # pragma: no cover - _refuse_symlink always raises on ELOOP
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def write_bytes_nofollow(path: Path, data: bytes) -> None:
    """Bytes counterpart of :func:`write_text_nofollow` (same O_NOFOLLOW
    refusal); used where the payload is copied verbatim (e.g. a spec file)."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    except OSError as exc:
        _refuse_symlink(path, exc)
        raise  # pragma: no cover - _refuse_symlink always raises on ELOOP
    with os.fdopen(fd, "wb") as f:
        f.write(data)


class TaskArtifacts:
    """Typed accessors for one task's artifact directory."""

    def __init__(
        self,
        repo_root: Path,
        task_id: str,
        now: Callable[[], str] = utc_now,
    ) -> None:
        self.repo_root = repo_root
        self.task_id = task_id
        self._now = now

    @property
    def dir(self) -> Path:
        return self.repo_root / TARGET_DIR_NAME / "tasks" / self.task_id

    @property
    def spec_path(self) -> Path:
        return self.dir / SPEC

    @property
    def log_path(self) -> Path:
        """The iteration log's path. The single owner of the log's location -
        every other module goes through here rather than rebuilding it."""
        return self.dir / LOG

    def _ensure(self) -> Path:
        """Create the task dir, first refusing any symlinked component.

        mkdir(exist_ok=True) silently accepts a path that is a symlink to a
        directory, so later writes would land through the link. The task dir is
        branch-controlled (a merged branch could ship <task>/ - or an ancestor -
        as a symlink), so check every component from repo_root down with lstat
        (is_symlink never follows) and fail closed. Nonexistent components are
        not symlinks; mkdir then creates them as real dirs."""
        cur = self.repo_root
        for part in self.dir.relative_to(self.repo_root).parts:
            cur = cur / part
            if cur.is_symlink():
                raise ArtifactPathError(
                    f"artifact path component {cur} is a symlink - refusing to "
                    "follow it (possible attack or corrupted state; remove by hand)"
                )
        self.dir.mkdir(parents=True, exist_ok=True)
        return self.dir

    @contextmanager
    def log_lock(self) -> Iterator[None]:
        """Serialize a read-then-append critical section on the log.

        Two layers, because the race has two sources:
        * IN-process (concurrent role threads on one loop) - a
          per-path ``threading.Lock``, present on every platform;
        * CROSS-process (two flag CLI runs on the VPS) - a blocking exclusive
          advisory ``flock`` on the log file itself (no extra/committed lock
          file), present only where ``fcntl`` is (POSIX, i.e. the loop node).

        On Windows ``fcntl`` is absent but the thread lock still holds, so the
        no-collision / no-loss guarantee is real for the in-process case the
        tests exercise; cross-process concurrency never arises off the VPS."""
        path = self._ensure() / LOG
        with _log_lock_for(path):
            if fcntl is None:
                yield
                return
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def append_log(self, **fields: Any) -> None:
        """Append exactly one line; never rewrites existing content."""
        append_jsonl(self._ensure() / LOG, {"ts": self._now(), **fields})
        # Heartbeat: mirror a one-line summary so a detached run's $LOG shows
        # progress (loop.py is otherwise silent; run.py prints only the terminal
        # state). stderr = diagnostic stream; env-gated so tests/CLI stay quiet.
        if os.environ.get("LADDY_LOG_HEARTBEAT") == "1":
            rnd = fields.get("round")
            tag = f"r{rnd} " if rnd is not None else ""
            print(
                f"[loop] {tag}{fields.get('action', '?')}: "
                f"{fields.get('outcome', '')}",
                file=sys.stderr,
                flush=True,
            )

    def read_log(self) -> list[dict[str, Any]]:
        return read_jsonl(self.dir / LOG)

    def write_json(self, name: str, obj: Any) -> None:
        write_text_nofollow(
            self._ensure() / name,
            json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
        )

    def read_json(self, name: str) -> Any | None:
        path = self.dir / name
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def write_text(self, name: str, text: str) -> None:
        write_text_nofollow(self._ensure() / name, text)

    def read_text(self, name: str) -> str | None:
        path = self.dir / name
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def copy_spec(self, src: Path) -> None:
        self._ensure()
        write_bytes_nofollow(self.spec_path, src.read_bytes())
