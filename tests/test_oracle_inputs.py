"""Tests for the oracle's phase-1 clean input (orchestrator.oracle.inputs).

The design's honesty rule: phase-1 cleanliness is STRUCTURAL, enforced by
these tests - never by prompt prose.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import TARGET_DIR_NAME
from orchestrator.oracle.inputs import materialize_phase1, merge_diff, remove_phase1
from tests.fakes import init_repo, merge_agent_task

CONTAMINATION = "CONTAMINATION-MARKER-rw1-said-go"


@pytest.fixture()
def repo_with_task(tmp_path: Path) -> tuple[Path, str, Path]:
    repo = init_repo(tmp_path / "repo")
    # an OLDER task's artifacts already on main
    merge_agent_task(repo, "older", {
        f"{TARGET_DIR_NAME}/tasks/older/spec.md": "# older\n",
        f"{TARGET_DIR_NAME}/tasks/older/iteration-log.jsonl": '{"action":"seed"}\n',
    })
    sha = merge_agent_task(repo, "t1", {
        "myapp/feature.py": "def f() -> int:\n    return 1\n",
        f"{TARGET_DIR_NAME}/tasks/t1/spec.md": "# t1 spec\nAC: f returns 1\n",
        f"{TARGET_DIR_NAME}/tasks/t1/iteration-log.jsonl": f'{{"note":"{CONTAMINATION}"}}\n',
        f"{TARGET_DIR_NAME}/tasks/t1/reviewer-a-verdict.json": f'{{"v":"{CONTAMINATION}"}}\n',
        f"{TARGET_DIR_NAME}/tasks/t1/human-summary.md": CONTAMINATION + "\n",
    })
    return repo, sha, tmp_path / "work"


def test_worktree_keeps_only_reviewed_spec(repo_with_task) -> None:
    repo, sha, work = repo_with_task
    wt = materialize_phase1(repo, "t1", sha, work)
    try:
        tasks_dir = wt / TARGET_DIR_NAME / "tasks"
        files = sorted(
            p.relative_to(wt).as_posix() for p in tasks_dir.rglob("*") if p.is_file()
        )
        # THE structural cleanliness assertion: exactly one file survives.
        assert files == [f"{TARGET_DIR_NAME}/tasks/t1/spec.md"]
        assert "t1 spec" in (tasks_dir / "t1" / "spec.md").read_text(encoding="utf-8")
        # the product code is present and runnable
        assert (wt / "myapp" / "feature.py").is_file()
        # nothing contaminated anywhere in the worktree
        for path in wt.rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                assert CONTAMINATION not in path.read_text(
                    encoding="utf-8", errors="ignore"
                ), path
    finally:
        remove_phase1(repo, "t1", work)
    assert not wt.exists()


def test_missing_spec_fails_loudly_and_cleans_up(repo_with_task) -> None:
    repo, sha, work = repo_with_task
    with pytest.raises(FileNotFoundError, match="spec"):
        materialize_phase1(repo, "no-such-task", sha, work)
    assert not (work / "oracle-no-such-task").exists()


def test_skip_clarify_task_falls_back_to_specs_dir(tmp_path: Path) -> None:
    # --skip-clarify tasks never get tasks/<id>/spec.md committed (copy_spec
    # runs only in the clarify phase); the spec the loop actually ran from
    # is specs/<id>.md at the same sha - the oracle must accept it as the bar.
    repo = init_repo(tmp_path / "repo")
    sha = merge_agent_task(repo, "t-skip", {
        "myapp/feature.py": "def f() -> int:\n    return 1\n",
        f"{TARGET_DIR_NAME}/specs/t-skip.md": "# t-skip\nAC: f returns 1\n",
        f"{TARGET_DIR_NAME}/tasks/t-skip/iteration-log.jsonl": '{"action":"go"}\n',
    })
    wt = materialize_phase1(repo, "t-skip", sha, tmp_path / "work")
    try:
        spec = wt / TARGET_DIR_NAME / "tasks" / "t-skip" / "spec.md"
        assert spec.is_file()
        assert "AC: f returns 1" in spec.read_text(encoding="utf-8")
        # cleanliness holds on the fallback path too
        files = sorted(
            p.relative_to(wt).as_posix()
            for p in (wt / TARGET_DIR_NAME / "tasks").rglob("*") if p.is_file()
        )
        assert files == [f"{TARGET_DIR_NAME}/tasks/t-skip/spec.md"]
    finally:
        remove_phase1(repo, "t-skip", tmp_path / "work")


def test_merge_diff_shows_product_and_excludes_artifacts(repo_with_task) -> None:
    repo, sha, _ = repo_with_task
    diff = merge_diff(repo, sha, "t1")
    assert "def f() -> int:" in diff
    # contamination control #2: the diff fed to phase 1 must not carry the
    # iteration log / verdicts that ride the same merge commit
    assert CONTAMINATION not in diff
    assert "iteration-log.jsonl" not in diff


def test_materialize_is_rerunnable(repo_with_task) -> None:
    repo, sha, work = repo_with_task
    materialize_phase1(repo, "t1", sha, work)
    wt = materialize_phase1(repo, "t1", sha, work)  # second run: no crash
    assert (wt / TARGET_DIR_NAME / "tasks" / "t1" / "spec.md").is_file()
    remove_phase1(repo, "t1", work)
