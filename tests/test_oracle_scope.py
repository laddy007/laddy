"""Tests for oracle scope selection (orchestrator.oracle.scope)."""

from __future__ import annotations

from pathlib import Path

from orchestrator import TARGET_DIR_NAME
from orchestrator.oracle.scope import (
    L1_SAMPLE_EVERY,
    MergedTask,
    merged_tasks_in_range,
    select_scope,
)
from orchestrator.target_policy import TargetPolicy
from tests.fakes import git, init_repo, merge_agent_task

_POL = TargetPolicy.myapp()

# changed_files -> bucket, via policy.classify_blast_radius:
L1_FILES = ("docs/notes.md",)                 # all safe-by-construction
L2_FILES = ("myapp/application/games/x.py",)  # ordinary logic
L3_FILES = ("myapp/models.py",)               # SENSITIVE_GLOBS


def _task(i: int, files: tuple[str, ...]) -> MergedTask:
    return MergedTask(f"t{i}", f"sha{i}", files, _POL)


def test_bucket_derives_from_policy() -> None:
    assert _task(1, L1_FILES).bucket == "L1"
    assert _task(2, L2_FILES).bucket == "L2"
    assert _task(3, L3_FILES).bucket == "L3"


def test_calibration_reviews_all_l2_l3_and_samples_l1() -> None:
    l1s = [_task(i, L1_FILES) for i in range(L1_SAMPLE_EVERY + 2)]  # 7 L1 tasks
    tasks = [_task(100, L2_FILES), *l1s, _task(101, L3_FILES)]
    scope = select_scope(tasks)
    reviewed_ids = [t.task_id for t in scope.reviewed]
    assert "t100" in reviewed_ids and "t101" in reviewed_ids  # all L2 + L3
    # deterministic L1 sample: indices 0 and L1_SAMPLE_EVERY among L1s in order
    assert reviewed_ids.count("t0") == 1
    assert f"t{L1_SAMPLE_EVERY}" in reviewed_ids
    assert len(scope.skipped) == L1_SAMPLE_EVERY  # the other L1s, recorded
    assert all(t.bucket == "L1" for t in scope.skipped)
    # same input -> same output (record-run must agree with prepare)
    again = select_scope(tasks)
    assert [t.task_id for t in again.reviewed] == reviewed_ids


def test_by_bucket_views() -> None:
    scope = select_scope([_task(1, L2_FILES), _task(2, L3_FILES)])
    assert scope.reviewed_by_bucket() == {"L2": ["t1"], "L3": ["t2"]}
    assert scope.skipped_by_bucket() == {}


def test_merged_tasks_in_range_reads_agent_merges_only(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    start = git(repo, "rev-parse", "HEAD")
    sha_a = merge_agent_task(repo, "alpha", {"myapp/x.py": "print('a')\n"})
    # a Director's manual merge (different subject) must be ignored
    git(repo, "checkout", "-b", "laddy/manual")
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "m.md").write_text("m\n", encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "manual work")
    git(repo, "checkout", "main")
    git(repo, "merge", "--no-ff", "laddy/manual", "-m", "Merge laddy/manual: docs")
    sha_b = merge_agent_task(repo, "beta", {"docs/b.md": "b\n"})

    tasks = merged_tasks_in_range(repo, start)
    assert [(t.task_id, t.merge_sha) for t in tasks] == [("alpha", sha_a), ("beta", sha_b)]
    assert tasks[0].changed_files == ("myapp/x.py",)
    assert tasks[0].bucket == "L2" and tasks[1].bucket == "L1"


def test_merge_subject_round_trips_through_its_parser() -> None:
    # Compose and parse are co-located in local_merge so they cannot drift;
    # this pins the round trip (the oracle goes blind if it breaks).
    from orchestrator.local_merge import merge_subject, parse_merge_subject

    assert parse_merge_subject(merge_subject("some-task", "a" * 40)) == "some-task"
    # a Director's manual merge subject is NOT loop output
    assert parse_merge_subject("Merge branch 'agent/foo'") is None


def test_task_artifacts_do_not_leak_into_bucket(tmp_path: Path) -> None:
    # A docs-only task still commits its .laddy/tasks/** artifacts (jsonl!);
    # bucket must classify the PRODUCT diff, mirroring gitops' policy view.
    repo = init_repo(tmp_path / "repo")
    start = git(repo, "rev-parse", "HEAD")
    merge_agent_task(repo, "docs-task", {
        "docs/note.md": "n\n",
        f"{TARGET_DIR_NAME}/tasks/docs-task/iteration-log.jsonl": '{"action":"x"}\n',
    })
    [task] = merged_tasks_in_range(repo, start)
    assert task.changed_files == ("docs/note.md",)
    assert task.bucket == "L1"


def test_artifact_only_merge_is_not_oracle_scope(tmp_path: Path) -> None:
    # A report-only task ships everything under <agent-dir>/tasks/ - the
    # policy pathspec excludes it all, so the product diff is EMPTY. There
    # is nothing for the oracle to review, and classify_blast_radius's
    # fail-closed L3 for () must not label it a high-risk merge.
    repo = init_repo(tmp_path / "repo")
    start = git(repo, "rev-parse", "HEAD")
    merge_agent_task(repo, "t-report", {
        f"{TARGET_DIR_NAME}/tasks/t-report/report.md": "# findings\n",
        f"{TARGET_DIR_NAME}/tasks/t-report/iteration-log.jsonl": '{"action":"go"}\n',
    })
    merge_agent_task(repo, "t-code", {"myapp/x.py": "x = 1\n"})
    tasks = merged_tasks_in_range(repo, start)
    assert [t.task_id for t in tasks] == ["t-code"]


def test_merge_sha_for_task_finds_newest_agent_merge(tmp_path: Path) -> None:
    from orchestrator.oracle.scope import merge_sha_for_task

    repo = init_repo(tmp_path / "repo")
    first = merge_agent_task(repo, "t1", {"a.py": "x = 1\n"})
    second = merge_agent_task(repo, "t1", {"a.py": "x = 2\n"})
    assert merge_sha_for_task(repo, "t1") == second != first
    assert merge_sha_for_task(repo, "nope") is None
