"""Task artifact store under <agent-dir>/tasks/<task>/ (design doc S9).

Git is the source of truth for loop state; the iteration log is
append-only (one JSON line per action, never updated). The loop can
resume purely from these files after a crash.
"""

from __future__ import annotations

import json
import os
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


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Every parseable event, in file order; missing file = [].

    Torn-final-line tolerant: a crash mid-append leaves a torn final line -
    that append never completed, so skipping it (never raising) is correct
    for an append-only log and keeps every reader alive.
    """
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            entries.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return entries


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

    def read_log(self) -> list[dict[str, Any]]:
        return read_jsonl(self.dir / LOG)

    def write_json(self, name: str, obj: Any) -> None:
        path = self._ensure() / name
        path.write_text(
            json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def read_json(self, name: str) -> Any | None:
        path = self.dir / name
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def write_text(self, name: str, text: str) -> None:
        (self._ensure() / name).write_text(text, encoding="utf-8", newline="\n")

    def read_text(self, name: str) -> str | None:
        path = self.dir / name
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def copy_spec(self, src: Path) -> None:
        self._ensure()
        self.spec_path.write_bytes(src.read_bytes())
