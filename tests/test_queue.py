"""Unit tests for the node-local task queue (FIFO + single-flight lock)."""

from __future__ import annotations

import fcntl
import json
import multiprocessing as mp
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from orchestrator.queue import (
    QueueError,
    QueueLocked,
    TaskQueue,
    _pid_alive,
    is_running,
    run_lock,
)


def _race_acquire(
    work_root: str, task_id: str, barrier: Any, results: Any, idx: int
) -> None:
    """Child-process worker: contend for run_lock and record the outcome.

    results[idx] = our pid if we acquired the lock, 0 if we got QueueLocked.
    A short hold keeps the winner's live pid in the lock file so the loser is
    guaranteed to observe a live holder (not an empty/half-written file).
    """
    barrier.wait()  # release both children at the same instant -> a real race
    try:
        with run_lock(Path(work_root), task_id):
            results[idx] = os.getpid()
            time.sleep(0.4)
    except QueueLocked:
        results[idx] = 0


def _acquire_and_signal(work_root: str, task_id: str, acquired: Any) -> None:
    """Child-process worker: run_lock then set an event, so the parent can
    observe whether acquisition proceeded or blocked on the guard flock."""
    with run_lock(Path(work_root), task_id):
        acquired.set()
        time.sleep(0.2)


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


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs POSIX fork")
def test_run_lock_concurrent_reclaim_has_single_winner(tmp_path: Path) -> None:
    # The guard's exact scenario: a stale lock (dead pid) left by a crash, then
    # TWO loops racing to reclaim it after a restart (e.g. an auto-restart
    # supervisor firing at the same moment as a manual kickoff retry). Exactly
    # ONE must acquire; the other MUST get QueueLocked. Never two live holders
    # for one task (which would risk interleaved commits/pushes + corrupt logs).
    locks = tmp_path / "locks"
    locks.mkdir()
    # 999999999 is above Linux pid_max, so it is guaranteed dead and never
    # reused by one of the child processes -> a deterministically stale lock.
    (locks / "t1.lock").write_text("999999999\n")

    ctx = mp.get_context("fork")
    barrier = ctx.Barrier(2)
    results = ctx.Array("q", [-1, -1])  # signed; -1 = worker never wrote
    procs = [
        ctx.Process(
            target=_race_acquire, args=(str(tmp_path), "t1", barrier, results, i)
        )
        for i in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(15)
    for p in procs:
        assert not p.is_alive(), "worker deadlocked"

    outcomes = [results[i] for i in range(2)]
    acquired = [r for r in outcomes if r > 0]
    locked = [r for r in outcomes if r == 0]
    assert len(acquired) == 1, outcomes  # exactly one winner
    assert len(locked) == 1, outcomes  # the other got a clean QueueLocked


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs POSIX fork")
def test_run_lock_acquisition_is_serialized_by_guard_flock(tmp_path: Path) -> None:
    # Deterministic guard for the FIX's mechanism: acquisition is gated by an
    # exclusive flock on the per-task guard file, so no two loops can be
    # mid-acquire at once -- the property that makes concurrent double-reclaim
    # impossible. Hold the guard here; a concurrent run_lock MUST block until we
    # release. If the flock were ever dropped, the child would reclaim the stale
    # lock immediately and this test would fail.
    locks = tmp_path / "locks"
    locks.mkdir()
    (locks / "t1.lock").write_text("999999999\n")  # stale: would otherwise reclaim
    guard_fd = os.open(locks / "t1.reclaim", os.O_CREAT | os.O_RDWR)
    fcntl.flock(guard_fd, fcntl.LOCK_EX)

    ctx = mp.get_context("fork")
    acquired = ctx.Event()
    child = ctx.Process(
        target=_acquire_and_signal, args=(str(tmp_path), "t1", acquired)
    )
    child.start()
    try:
        # While we hold the guard, the child cannot acquire.
        assert not acquired.wait(timeout=0.5), "acquired despite guard held"
        # Release the guard -> the child proceeds.
        fcntl.flock(guard_fd, fcntl.LOCK_UN)
        os.close(guard_fd)
        assert acquired.wait(timeout=5.0), "child never acquired after release"
    finally:
        child.join(10)
