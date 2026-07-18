"""Node-local FIFO of ready task specs (spec: quota-resume-queue).

Lives under AGENT_WORK_ROOT (runtime state -- never committed to the
repo). One JSON file per item, ordered by a numeric filename prefix.
A per-task .lock file holding the holder's pid makes loop processing
single-flight per node; acquisition is serialized by an flock guard so
concurrent reclaimers cannot both win, and a lock left by a crashed
loop (dead pid) is reclaimed automatically on the next run.
"""

from __future__ import annotations

import errno
import fcntl
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
        """Append a task to the FIFO under the single-flight queue lock.

        The whole dup-check -> next-seq -> write runs while holding ``lock()``,
        so two concurrent enqueues can neither both pass the duplicate-task
        check nor both compute the same sequence number (a duplicate/adjacent-
        seq entry). Raises QueueLocked if a queue runner holds the lock.
        """
        with self.lock():
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
        """Single-flight queue-processing lock, with crash-reclaim.

        A lock left by a crashed queue runner (dead pid) is auto-reclaimed on
        the next call, exactly like run_lock; only a lock held by a LIVE process
        raises QueueLocked. Reuses _acquire_lock's flock-serialized
        read-pid -> decide -> write-pid (and its O_NOFOLLOW symlink refusal), so
        two concurrent reclaimers can never both win.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.dir / ".lock"
        _acquire_lock(self.dir, lock_path, "queue")
        try:
            yield
        finally:
            lock_path.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    """True iff a process with this pid currently exists.

    A dead pid means a crashed loop left the lock behind (stale). PID reuse
    (a recycled pid held by some unrelated process before the next run) is a
    tiny theoretical window on a single-node box and no worse than the prior
    behaviour, which never checked liveness at all and refused unconditionally.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user -> still alive
    return True


def _refuse_symlink(lock_path: Path, exc: OSError) -> None:
    if exc.errno == errno.ELOOP:
        raise QueueLocked(
            f"lock path {lock_path} is a symlink - refusing to follow it "
            "(possible attack or corrupted state; remove it by hand)"
        ) from exc
    raise exc


def _read_lock_pid(lock_path: Path) -> int:
    """Read the pid recorded in lock_path, NEVER following a symlink.

    O_NOFOLLOW makes the open itself fail (ELOOP) if the final path component
    is a symlink, rather than opening whatever it points to - a bare
    Path.read_text()/open() would happily read (and, on the write side,
    truncate) an attacker-planted symlink's target. Returns 0 (treated as "no
    live holder") for a missing, empty, or unparseable file - the caller
    already tolerates a stale/corrupt lock left by a crashed loop.
    """
    try:
        fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return 0
    except OSError as exc:
        _refuse_symlink(lock_path, exc)
        raise  # pragma: no cover - _refuse_symlink always raises
    with os.fdopen(fd) as f:
        data = f.read().strip()
    try:
        return int(data or "0")
    except ValueError:
        return 0  # truncated / garbage content -> treat as no live pid


def _write_lock_pid(lock_path: Path) -> None:
    """Create or overwrite lock_path with our pid, NEVER following a symlink.

    See _read_lock_pid: O_NOFOLLOW is what makes this refuse to write through
    a swapped-in symlink instead of truncating whatever file it points to.
    """
    try:
        fd = os.open(
            lock_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
    except OSError as exc:
        _refuse_symlink(lock_path, exc)
        raise  # pragma: no cover - _refuse_symlink always raises
    with os.fdopen(fd, "w") as f:
        f.write(f"{os.getpid()}\n")


def _acquire_lock(locks: Path, lock_path: Path, task_id: str) -> None:
    """Take the per-task lock, reclaiming one left by a crashed loop.

    Writes our pid into lock_path, or raises QueueLocked if a live process
    already holds it (or if lock_path has been replaced by a symlink - see
    _read_lock_pid/_write_lock_pid).

    Every acquire decision for a task is serialized by an flock on a dedicated
    guard file, so the whole read-pid -> decide -> write-pid sequence is atomic
    across racing loops. Two racers can therefore never both observe the lock
    as free/stale and both take it (the defect a bare unlink-then-O_EXCL-create
    allowed: a loser's unlink could delete the winner's freshly created lock,
    letting both opens succeed -> two live holders for one task). The pid file
    is only ever read or written while the guard is held, so there is also no
    empty-file window a concurrent reclaimer could misread as stale. flock is
    released by the kernel when the fd closes OR the holder dies, so the guard
    itself never goes stale the way a bare pid file can.
    """
    guard_fd = os.open(locks / f"{task_id}.reclaim", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(guard_fd, fcntl.LOCK_EX)  # blocks until sole acquirer
        old = _read_lock_pid(lock_path)
        if _pid_alive(old):
            raise QueueLocked(
                f"task {task_id} already has a running loop (pid {old})"
            )
        # else: no live pid recorded -> free or crashed holder, reclaim it
        _write_lock_pid(lock_path)  # create or reclaim (guard held)
    finally:
        os.close(guard_fd)  # releases the flock


@contextmanager
def run_lock(work_root: Path, task_id: str) -> Iterator[None]:
    """Per-task run lock: exactly one loop (kickoff OR queue) per task.

    A lock left behind by a crashed loop is auto-reclaimed: if the recorded
    pid is no longer alive, the stale lock is taken over. Acquisition is
    serialized per task by an flock (see _acquire_lock), so concurrent
    reclaimers can never both win. Only a lock held by a live process raises
    QueueLocked.
    """
    locks = work_root / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    lock_path = locks / f"{task_id}.lock"
    _acquire_lock(locks, lock_path, task_id)
    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def is_running(work_root: Path, task_id: str) -> bool:
    return (work_root / "locks" / f"{task_id}.lock").exists()
