"""Unit tests for the node-local task queue (FIFO + single-flight lock)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from orchestrator.queue import (
    QueueError,
    QueueLocked,
    TaskQueue,
    _pid_alive,
    is_running,
    run_lock,
)


def test_enqueue_items_fifo_order(tmp_path: Path) -> None:
    q = TaskQueue(tmp_path)
    q.enqueue("alpha")
    q.enqueue("beta", skip_clarify=True)
    q.enqueue("gamma")
    items = q.items()
    assert [i.task_id for i in items] == ["alpha", "beta", "gamma"]
    assert [i.skip_clarify for i in items] == [False, True, False]


def test_enqueue_rejects_duplicate_task(tmp_path: Path) -> None:
    q = TaskQueue(tmp_path)
    q.enqueue("alpha")
    with pytest.raises(QueueError):
        q.enqueue("alpha")


def test_remove_pops_item_and_preserves_order(tmp_path: Path) -> None:
    q = TaskQueue(tmp_path)
    q.enqueue("alpha")
    q.enqueue("beta")
    q.remove(q.items()[0])
    assert [i.task_id for i in q.items()] == ["beta"]


def test_fifo_survives_removal_and_reenqueue(tmp_path: Path) -> None:
    q = TaskQueue(tmp_path)
    q.enqueue("alpha")
    q.enqueue("beta")
    q.remove(q.items()[0])
    q.enqueue("alpha")  # re-enqueue after completion goes to the BACK
    assert [i.task_id for i in q.items()] == ["beta", "alpha"]


def test_lock_is_single_flight(tmp_path: Path) -> None:
    q1 = TaskQueue(tmp_path)
    q2 = TaskQueue(tmp_path)
    with q1.lock():
        with pytest.raises(QueueLocked):
            with q2.lock():
                pass
    # released after the context exits
    with q2.lock():
        pass


def test_items_ordered_numerically_not_lexicographically(tmp_path: Path) -> None:
    q = TaskQueue(tmp_path)
    q.dir.mkdir(parents=True)
    for seq, task in ((9999, "older"), (10000, "newer")):
        (q.dir / f"{seq:04d}-{task}.json").write_text(
            json.dumps({"task_id": task, "enqueued_at": "t", "skip_clarify": False})
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    assert [i.task_id for i in q.items()] == ["older", "newer"]  # 9999 before 10000


def test_empty_queue_lists_nothing(tmp_path: Path) -> None:
    assert TaskQueue(tmp_path).items() == []


def test_run_lock_is_exclusive_per_task(tmp_path: Path) -> None:
    with run_lock(tmp_path, "t1"):
        assert is_running(tmp_path, "t1") is True
        with pytest.raises(QueueLocked):
            with run_lock(tmp_path, "t1"):
                pass
        with run_lock(tmp_path, "t2"):  # different task is fine
            pass
    assert is_running(tmp_path, "t1") is False


def test_pid_alive(tmp_path: Path) -> None:
    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(0) is False
    assert _pid_alive(-1) is False
    dead = subprocess.Popen(["true"])  # spawn + reap -> a definitely-dead pid
    dead.wait()
    assert _pid_alive(dead.pid) is False


def test_pid_alive_permission_error_means_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pid owned by another user: os.kill(pid, 0) raises PermissionError, which
    # proves the process EXISTS -> a live holder, so the lock must not reclaim.
    def _deny(pid: int, sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "kill", _deny)
    assert _pid_alive(4242) is True


def test_run_lock_reclaims_stale_lock_from_dead_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A crashed loop left a lock file behind whose pid is no longer alive.
    locks = tmp_path / "locks"
    locks.mkdir()
    (locks / "t1.lock").write_text("424242\n")
    monkeypatch.setattr("orchestrator.queue._pid_alive", lambda pid: False)
    # run_lock must reclaim the stale lock instead of raising QueueLocked.
    with run_lock(tmp_path, "t1"):
        assert is_running(tmp_path, "t1") is True
    assert is_running(tmp_path, "t1") is False


def test_run_lock_refuses_when_holder_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks = tmp_path / "locks"
    locks.mkdir()
    (locks / "t1.lock").write_text("424242\n")
    monkeypatch.setattr("orchestrator.queue._pid_alive", lambda pid: True)
    with pytest.raises(QueueLocked):
        with run_lock(tmp_path, "t1"):
            pass


def test_run_lock_reclaims_corrupt_lock_file(tmp_path: Path) -> None:
    # A truncated/garbage lock file (crash mid-write, or empty) has no parseable
    # pid: run_lock treats old=0 -> not alive -> reclaim, rather than crashing
    # on int(). No monkeypatch: exercises the real _pid_alive(0) path too.
    locks = tmp_path / "locks"
    locks.mkdir()
    (locks / "t1.lock").write_text("not-a-pid")
    with run_lock(tmp_path, "t1"):
        assert is_running(tmp_path, "t1") is True
    assert is_running(tmp_path, "t1") is False


def test_run_lock_concurrent_reclaim_raises_queue_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two loops race to reclaim the same stale lock: the loser's re-open (after
    # its unlink) hits a lock the winner already recreated. That must surface as
    # QueueLocked (clean rc=4), not an uncaught FileExistsError traceback.
    locks = tmp_path / "locks"
    locks.mkdir()
    (locks / "t1.lock").write_text("424242\n")
    monkeypatch.setattr("orchestrator.queue._pid_alive", lambda pid: False)

    real_open = os.open
    seen_excl = {"n": 0}

    def flaky_open(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        # 1st O_EXCL open = the stale lock (real FileExistsError, as normal);
        # 2nd = the reclaim re-open, which we force to lose the race.
        if flags & os.O_EXCL:
            seen_excl["n"] += 1
            if seen_excl["n"] == 2:
                raise FileExistsError
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", flaky_open)
    with pytest.raises(QueueLocked):
        with run_lock(tmp_path, "t1"):
            pass
