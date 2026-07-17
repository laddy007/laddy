"""Tests for git operations (clone/branch/commit/push - explicitly NO merge)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator import TARGET_DIR_NAME
from orchestrator.gitops import GitError, GitOps


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


IDENTITY = (
    "-c",
    "user.name=test",
    "-c",
    "user.email=test@example.com",
)


@pytest.fixture()
def remote(tmp_path: Path) -> Path:
    """Bare repo with one commit on main - stands in for GitHub."""
    bare = tmp_path / "remote.git"
    _git("init", "--bare", "--initial-branch=main", str(bare))
    seed = tmp_path / "seed"
    _git("clone", str(bare), str(seed))
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    _git("-C", str(seed), "add", "README.md")
    _git("-C", str(seed), *IDENTITY, "commit", "-m", "init")
    _git("-C", str(seed), "push", "origin", "HEAD:main")
    return bare


@pytest.fixture()
def gitops(remote: Path, tmp_path: Path) -> GitOps:
    return GitOps(repo_url=remote.as_uri(), work_root=tmp_path / "work")


def test_ensure_base_clones_then_fetches(gitops: GitOps) -> None:
    base = gitops.ensure_base()
    assert (base / ".git").is_dir()
    # second call must fetch, not fail
    assert gitops.ensure_base() == base


def test_branch_is_bare_task_id(tmp_path: Path) -> None:
    ops = GitOps("unused", tmp_path)
    assert ops._branch("fix-42") == "fix-42"


def test_main_task_id_rejected(tmp_path: Path) -> None:
    ops = GitOps("unused", tmp_path)
    with pytest.raises(GitError, match="reserved"):
        ops.task_worktree("main")
    with pytest.raises(GitError, match="reserved"):
        ops.push(tmp_path, "main")


def test_task_worktree_branches_off_origin_main(gitops: GitOps, remote: Path) -> None:
    wt = gitops.task_worktree("t1")
    assert _git("-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD") == "t1"
    assert (wt / "README.md").is_file()


def test_task_worktree_resumes_existing_remote_branch(
    gitops: GitOps, remote: Path, tmp_path: Path
) -> None:
    # push a t1 branch with an extra commit, as a crashed prior run would
    seed2 = tmp_path / "seed2"
    _git("clone", str(remote), str(seed2))
    _git("-C", str(seed2), "checkout", "-b", "t1")
    (seed2 / "work.txt").write_text("wip\n", encoding="utf-8")
    _git("-C", str(seed2), "add", "work.txt")
    _git("-C", str(seed2), *IDENTITY, "commit", "-m", "wip")
    _git("-C", str(seed2), "push", "origin", "t1")

    wt = gitops.task_worktree("t1")
    assert (wt / "work.txt").is_file(), "worktree must resume the pushed branch"


def test_sync_worktree_to_origin_fast_forwards_to_pushed_tip(
    gitops: GitOps, remote: Path, tmp_path: Path
) -> None:
    # A worktree finishes and pushes t1; then a SEPARATE clone pushes a newer
    # commit on top. sync must fast-forward the (reused, stale) worktree to it.
    wt = gitops.task_worktree("t1")
    (wt / "work.txt").write_text("v1\n", encoding="utf-8")
    gitops.commit_all(wt, "v1")
    gitops.push(wt, "t1")

    clone = tmp_path / "other"
    _git("clone", str(remote), str(clone))
    _git("-C", str(clone), "checkout", "t1")
    (clone / "work.txt").write_text("v2\n", encoding="utf-8")
    _git("-C", str(clone), "add", "work.txt")
    _git("-C", str(clone), *IDENTITY, "commit", "-m", "v2")
    _git("-C", str(clone), "push", "origin", "t1")
    tip = _git("-C", str(remote), "rev-parse", "t1")

    assert (wt / "work.txt").read_text(encoding="utf-8") == "v1\n"  # stale before sync
    assert gitops.sync_worktree_to_origin(wt, "t1") is True
    assert (wt / "work.txt").read_text(encoding="utf-8") == "v2\n"  # synced
    assert gitops.head_sha(wt) == tip


def test_sync_worktree_to_origin_is_noop_when_branch_absent_on_origin(
    gitops: GitOps,
) -> None:
    # a purely local task the branch of which was never pushed: nothing to sync
    # onto, so the function does nothing (no crash, worktree untouched) and
    # reports False.
    wt = gitops.task_worktree("t1")  # branched off origin/main; t1 not on origin
    (wt / "local.txt").write_text("local\n", encoding="utf-8")
    gitops.commit_all(wt, "local only")
    head_before = gitops.head_sha(wt)
    assert gitops.sync_worktree_to_origin(wt, "t1") is False
    assert gitops.head_sha(wt) == head_before  # untouched
    assert (wt / "local.txt").is_file()


def test_commit_all_returns_sha_and_skips_clean_tree(gitops: GitOps) -> None:
    wt = gitops.task_worktree("t1")
    assert gitops.commit_all(wt, "nothing to do") is None
    (wt / "new.txt").write_text("x\n", encoding="utf-8")
    sha = gitops.commit_all(wt, "add new.txt")
    assert sha is not None
    assert gitops.head_sha(wt) == sha


def test_push_updates_remote_agent_ref(gitops: GitOps, remote: Path) -> None:
    wt = gitops.task_worktree("t1")
    (wt / "new.txt").write_text("x\n", encoding="utf-8")
    gitops.commit_all(wt, "add new.txt")
    gitops.push(wt, "t1")
    remote_sha = _git("-C", str(remote), "rev-parse", "refs/heads/t1")
    assert remote_sha == gitops.head_sha(wt)


def test_changed_files_lists_paths_vs_origin_main(gitops: GitOps) -> None:
    wt = gitops.task_worktree("t1")
    (wt / "pkg").mkdir()
    (wt / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (wt / "README.md").write_text("changed\n", encoding="utf-8")
    gitops.commit_all(wt, "changes")
    assert sorted(gitops.changed_files(wt)) == ["README.md", "pkg/mod.py"]


def test_diff_text_contains_patch(gitops: GitOps) -> None:
    wt = gitops.task_worktree("t1")
    (wt / "README.md").write_text("changed\n", encoding="utf-8")
    gitops.commit_all(wt, "change readme")
    assert "+changed" in gitops.diff_text(wt)


def test_changed_files_shows_renamed_away_test_as_deletion(
    gitops: GitOps, remote: Path, tmp_path: Path
) -> None:
    # a `git mv tests/x.py elsewhere` must surface the OLD tests/ path (as a
    # deletion) so policy guards on invariant/sensitive tests still see it.
    # First put the test file at the BASE (origin/main).
    seed = tmp_path / "seed-arch"
    _git("clone", str(remote), str(seed))
    (seed / "tests").mkdir()
    (seed / "tests" / "test_arch.py").write_text("x = 1\n", encoding="utf-8")
    _git("-C", str(seed), "add", "-A")
    _git("-C", str(seed), *IDENTITY, "commit", "-m", "add arch test")
    _git("-C", str(seed), "push", "origin", "HEAD:main")

    wt = gitops.task_worktree("t1")
    subprocess.run(["git", "-C", str(wt), "mv", "tests/test_arch.py", "notes.py"], check=True)
    gitops.commit_all(wt, "move test out")

    statuses = gitops.changed_statuses(wt)
    files = gitops.changed_files(wt)
    # with --no-renames the move is D(old) + A(new), so the old tests/ path is
    # visible to deleted_test_files / touches_invariant_tests / sensitive_paths
    assert "tests/test_arch.py" in files
    assert statuses.get("tests/test_arch.py") == "D"
    assert "notes.py" in files


def test_changed_files_excludes_task_artifacts(gitops: GitOps) -> None:
    # <agent-dir>/tasks/** must not count toward policy inputs (VPS/CI diff drift)
    wt = gitops.task_worktree("t1")
    (wt / "real.py").write_text("x = 1\n", encoding="utf-8")
    art_dir = wt / TARGET_DIR_NAME / "tasks" / "t1"
    art_dir.mkdir(parents=True)
    (art_dir / "iteration-log.jsonl").write_text('{"a":1}\n', encoding="utf-8")
    gitops.commit_all(wt, "code + artifacts")
    files = gitops.changed_files(wt)
    assert "real.py" in files
    assert not any(f.startswith(f"{TARGET_DIR_NAME}/tasks/") for f in files)
    assert gitops.diff_line_count(wt) == 1  # only real.py's one line counts


def test_refresh_base_syncs_working_tree_to_remote_head(
    gitops: GitOps, remote: Path, tmp_path: Path
) -> None:
    base = gitops.ensure_base()
    assert not (base / TARGET_DIR_NAME / "specs" / "late.md").exists()

    # push a NEW spec file to the remote from a second clone
    seed = tmp_path / "seed-late"
    _git("clone", str(remote), str(seed))
    (seed / TARGET_DIR_NAME / "specs").mkdir(parents=True, exist_ok=True)
    (seed / TARGET_DIR_NAME / "specs" / "late.md").write_text("# late\n", encoding="utf-8")
    _git("-C", str(seed), "add", "-A")
    _git("-C", str(seed), *IDENTITY, "commit", "-m", "add late spec")
    _git("-C", str(seed), "push", "origin", "HEAD:main")

    # ensure_base alone only fetches - the working tree stays stale
    gitops.ensure_base()
    assert not (base / TARGET_DIR_NAME / "specs" / "late.md").exists()

    result = gitops.refresh_base()
    assert result == base
    assert (base / TARGET_DIR_NAME / "specs" / "late.md").exists()


def test_gitops_module_has_no_merge_operation() -> None:
    """Spec acceptance 6: no merge logic anywhere in gitops (grep-verifiable)."""
    import orchestrator.gitops as gitops_module

    source = Path(gitops_module.__file__).read_text(encoding="utf-8")
    assert "merge" not in source.lower()
