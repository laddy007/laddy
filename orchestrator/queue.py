"""Node-local FIFO of ready task specs (spec: quota-resume-queue).

Lives under AGENT_WORK_ROOT (runtime state -- never committed to the
repo). One JSON file per item, ordered by a numeric filename prefix.
A .lock file created O_EXCL makes queue processing single-flight per
node; surviving a VPS reboot is explicitly out of scope (a stale lock
is removed by hand, the error message says which file).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from orchestrator.artifacts import utc_now


class QueueError(Exception):
    """Invalid queue operation (duplicate task, corrupt item...)."""


class QueueLocked(QueueError):
    """Another queue runner holds the single-flight lock."""


@dataclass(frozen=True)
class QueueItem:
    path: Path
    task_id: str
    enqueued_at: str
    skip_clarify: bool


class TaskQueue:
    """FIFO of task ids under <work_root>/queue/."""

    def __init__(self, work_root: Path) -> None:
        self.dir = work_root / "queue"

    def _next_seq(self) -> int:
        taken = [int(p.name.split("-", 1)[0]) for p in self.dir.glob("[0-9]*-*.json")]
        return max(taken, default=0) + 1

    def enqueue(
        self,
        task_id: str,
        *,
        skip_clarify: bool = False,
        now_fn: Callable[[], str] = utc_now,
    ) -> QueueItem:
        self.dir.mkdir(parents=True, exist_ok=True)
        if any(item.task_id == task_id for item in self.items()):
            raise QueueError(f"task {task_id} is already queued")
        path = self.dir / f"{self._next_seq():04d}-{task_id}.json"
        item = QueueItem(path, task_id, now_fn(), skip_clarify)
        path.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "enqueued_at": item.enqueued_at,
                    "skip_clarify": skip_clarify,
                }
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return item

    def items(self) -> list[QueueItem]:
        if not self.dir.is_dir():
            return []
        out: list[QueueItem] = []
        for path in sorted(
            self.dir.glob("[0-9]*-*.json"),
            key=lambda p: int(p.name.split("-", 1)[0]),
        ):
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append(
                QueueItem(
                    path=path,
                    task_id=str(data["task_id"]),
                    enqueued_at=str(data["enqueued_at"]),
                    skip_clarify=bool(data.get("skip_clarify", False)),
                )
            )
        return out

    def remove(self, item: QueueItem) -> None:
        item.path.unlink(missing_ok=True)

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.dir / ".lock"
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise QueueLocked(
                f"queue already being processed (remove {lock_path} if stale)"
            ) from None
        try:
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()}\n")
            yield
        finally:
            lock_path.unlink(missing_ok=True)


@contextmanager
def run_lock(work_root: Path, task_id: str) -> Iterator[None]:
    """Per-task run lock: exactly one loop (kickoff OR queue) per task."""
    locks = work_root / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    lock_path = locks / f"{task_id}.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise QueueLocked(
            f"task {task_id} already has a running loop "
            f"(remove {lock_path} if stale)"
        ) from None
    try:
        os.write(fd, f"{os.getpid()}\n".encode())
        os.close(fd)
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def is_running(work_root: Path, task_id: str) -> bool:
    return (work_root / "locks" / f"{task_id}.lock").exists()
