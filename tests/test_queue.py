"""Unit tests for the node-local task queue (FIFO + single-flight lock)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.queue import QueueError, QueueLocked, TaskQueue, is_running, run_lock


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
