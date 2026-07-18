"""Git provisioning for task branches (design doc S11: gitops.py).

Clone/fetch the base repo, maintain one worktree per task on a bare
`<task>` branch, commit and push that branch. The hub is a closed
namespace where every non-default branch IS a task id (spec: discovery
selector) - so `task_id` doubles as the branch name, and any id equal to
`default_branch` is rejected before any git command runs. Nothing here
can write the default branch: pushes go exclusively to bare `<task>` refs.

Stateless diff sizing/classification lives in orchestrator.gitdiff; the
method forms below are thin binders (worktree + default_branch) kept
because the loop and the policy recheckers call them on the GitOps instance.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from orchestrator import gitdiff
from orchestrator.gitdiff import GitError as GitError  # re-export (one error type)
from orchestrator.gitdiff import git_out

_IDENTITY = (
    "-c",
    "user.name=myapp-agent",
    "-c",
    "user.email=agent@myapp.local",
)



class GitOps:
    def __init__(
        self, repo_url: str, work_root: Path, default_branch: str = "main"
    ) -> None:
        self.repo_url = repo_url
        self.work_root = work_root
        self.default_branch = default_branch

    @property
    def base_dir(self) -> Path:
        return self.work_root / "base"

    def ensure_base(self) -> Path:
        """Clone the repo once; fetch on every later call."""
        if not (self.base_dir / ".git").is_dir():
            self.base_dir.parent.mkdir(parents=True, exist_ok=True)
            git_out(["git", "clone", self.repo_url, str(self.base_dir)])
        else:
            git_out(["git", "-C", str(self.base_dir), "fetch", "origin"])
        return self.base_dir

    def refresh_base(self) -> Path:
        """ensure_base + hard-sync the base working tree to origin/<default>.

        The base clone's checkout is orchestrator-owned scratch (task state
        lives in separate worktrees), so a hard reset is safe here. Needed
        because ensure_base only fetches - its working tree stays at
        clone-time state, and enqueue discovery must read CURRENT specs."""
        base = self.ensure_base()
        git_out(["git", "-C", str(base), "checkout", "-q", self.default_branch])
        git_out(
            [
                "git", "-C", str(base), "reset", "--hard", "-q",
                f"origin/{self.default_branch}",
            ]
        )
        return base

    def _branch(self, task_id: str) -> str:
        if task_id == self.default_branch:
            raise GitError(
                f"task id {task_id!r} is reserved: the hub is a closed namespace "
                "where every non-main branch IS a task (spec: discovery selector)"
            )
        return task_id

    def task_worktree(self, task_id: str) -> Path:
        """Worktree at the bare `<task>` branch: resume the remote branch if
        it exists, otherwise branch off origin/<default_branch>."""
        branch = self._branch(task_id)
        base = self.ensure_base()
        wt = self.work_root / "wt" / task_id
        if (wt / ".git").exists():
            return wt
        git_out(["git", "-C", str(base), "worktree", "prune"])
        remote_ref = f"origin/{branch}"
        has_remote = (
            subprocess.run(
                ["git", "-C", str(base), "rev-parse", "--verify", "--quiet", remote_ref],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        start = remote_ref if has_remote else f"origin/{self.default_branch}"
        wt.parent.mkdir(parents=True, exist_ok=True)
        git_out(
            ["git", "-C", str(base), "worktree", "add", "-B", branch, str(wt), start]
        )
        return wt

    def commit_all(self, wt: Path, message: str) -> str | None:
        """Stage and commit everything; None when the tree is clean."""
        if not git_out(["git", "-C", str(wt), "status", "--porcelain"]):
            return None
        git_out(["git", "-C", str(wt), "add", "-A"])
        git_out(["git", "-C", str(wt), *_IDENTITY, "commit", "-q", "-m", message])
        return self.head_sha(wt)

    def push(self, wt: Path, task_id: str) -> None:
        """Push the task branch. The ONLY remote write this module performs."""
        branch = self._branch(task_id)
        git_out(
            [
                "git",
                "-C",
                str(wt),
                "push",
                "origin",
                f"HEAD:refs/heads/{branch}",
            ]
        )

    # --- diff helpers: thin binders over orchestrator.gitdiff ---------------

    def head_sha(self, wt: Path) -> str:
        return gitdiff.head_sha(wt)

    def code_sha(self, wt: Path, task_id: str) -> str:
        return gitdiff.code_sha(wt, task_id)

    def changed_files(self, wt: Path, task_id: str) -> list[str]:
        return gitdiff.changed_files(wt, task_id, self.default_branch)

    def diff_text(self, wt: Path) -> str:
        return gitdiff.diff_text(wt, self.default_branch)

    def diff_line_count(self, wt: Path, task_id: str) -> int:
        return gitdiff.diff_line_count(wt, task_id, self.default_branch)

    def changed_statuses(self, wt: Path, task_id: str) -> dict[str, str]:
        return gitdiff.changed_statuses(wt, task_id, self.default_branch)
