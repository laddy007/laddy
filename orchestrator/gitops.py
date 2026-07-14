"""Git operations for task branches (design doc S11: gitops.py).

Clone/fetch the base repo, maintain one worktree per task on a bare
`<task>` branch, commit and push that branch. The hub is a closed
namespace where every non-default branch IS a task id (spec: discovery
selector) - so `task_id` doubles as the branch name, and any id equal to
`default_branch` is rejected before any git command runs. Nothing here
can write the default branch: pushes go exclusively to bare `<task>` refs.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from orchestrator import TARGET_DIR_NAME

_IDENTITY = (
    "-c",
    "user.name=myapp-agent",
    "-c",
    "user.email=agent@myapp.local",
)


class GitError(RuntimeError):
    """A git command failed."""


def _run(args: Sequence[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(
        list(args), cwd=cwd, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise GitError(
            f"git command failed ({proc.returncode}): {' '.join(args)}\n{proc.stderr}"
        )
    return proc.stdout.strip()


def policy_pathspec() -> list[str]:
    """The policy view of a diff: everything EXCEPT <agent-dir>/tasks/**.

    Task artifacts (iteration logs, verdicts) ride every branch; the policy
    and the oracle both classify by the PRODUCT diff. One home for
    the exclusion so no caller restates it (convergence R1/R3).
    """
    return ["--", ".", f":(exclude){TARGET_DIR_NAME}/tasks"]


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
            _run(["git", "clone", self.repo_url, str(self.base_dir)])
        else:
            _run(["git", "-C", str(self.base_dir), "fetch", "origin"])
        return self.base_dir

    def refresh_base(self) -> Path:
        """ensure_base + hard-sync the base working tree to origin/<default>.

        The base clone's checkout is orchestrator-owned scratch (task state
        lives in separate worktrees), so a hard reset is safe here. Needed
        because ensure_base only fetches - its working tree stays at
        clone-time state, and enqueue discovery must read CURRENT specs."""
        base = self.ensure_base()
        _run(["git", "-C", str(base), "checkout", "-q", self.default_branch])
        _run(
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
        _run(["git", "-C", str(base), "worktree", "prune"])
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
        _run(
            ["git", "-C", str(base), "worktree", "add", "-B", branch, str(wt), start]
        )
        return wt

    def commit_all(self, wt: Path, message: str) -> str | None:
        """Stage and commit everything; None when the tree is clean."""
        if not _run(["git", "-C", str(wt), "status", "--porcelain"]):
            return None
        _run(["git", "-C", str(wt), "add", "-A"])
        _run(["git", "-C", str(wt), *_IDENTITY, "commit", "-q", "-m", message])
        return self.head_sha(wt)

    def head_sha(self, wt: Path) -> str:
        return _run(["git", "-C", str(wt), "rev-parse", "HEAD"])

    def code_sha(self, wt: Path) -> str:
        """SHA of the last commit touching anything OUTSIDE <agent-dir>/tasks/.

        Gate results are keyed to this, not HEAD: artifact commits (verdicts,
        logs) move HEAD but do not change the reviewed code, so they must not
        invalidate approvals. Any real code commit does.
        """
        out = _run(
            [
                "git",
                "-C",
                str(wt),
                "rev-list",
                "-1",
                "HEAD",
                "--",
                ".",
                f":(exclude){TARGET_DIR_NAME}/tasks",
            ]
        )
        return out or self.head_sha(wt)

    def push(self, wt: Path, task_id: str) -> None:
        """Push the task branch. The ONLY remote write this module performs."""
        branch = self._branch(task_id)
        _run(
            [
                "git",
                "-C",
                str(wt),
                "push",
                "origin",
                f"HEAD:refs/heads/{branch}",
            ]
        )

    # Policy inputs (changed_files / changed_statuses / diff_line_count) share
    # two deliberate flags so the VPS decision and the off-VPS recheck agree
    # AND cannot be gamed:
    #   --no-renames  : a `git mv tests/test_x.py elsewhere` shows as
    #                   D tests/test_x.py + A elsewhere, so a renamed-away
    #                   invariant/sensitive test is still seen by the guards.
    #   :(exclude)<agent-dir>/tasks : every task commits its own artifacts there;
    #                   counting them would (a) inflate size-based risk and
    #                   (b) make the VPS pre-artifact-commit decision differ
    #                   from the CI post-artifact-commit recompute.
    def _range(self) -> str:
        return f"origin/{self.default_branch}...HEAD"

    def _policy_pathspec(self) -> list[str]:
        return policy_pathspec()

    def changed_files(self, wt: Path) -> list[str]:
        out = _run(
            ["git", "-C", str(wt), "diff", "--no-renames", "--name-only", self._range()]
            + self._policy_pathspec()
        )
        return [line for line in out.splitlines() if line]

    def diff_text(self, wt: Path) -> str:
        return _run(["git", "-C", str(wt), "diff", self._range()])

    def diff_line_count(self, wt: Path) -> int:
        """Added+deleted line count for policy sizing (excludes task artifacts)."""
        out = _run(
            ["git", "-C", str(wt), "diff", "--no-renames", "--numstat", self._range()]
            + self._policy_pathspec()
        )
        total = 0
        for line in out.splitlines():
            added, deleted, *_ = line.split("\t")
            total += (0 if added == "-" else int(added)) + (
                0 if deleted == "-" else int(deleted)
            )
        return total

    def changed_statuses(self, wt: Path) -> dict[str, str]:
        """path -> git status letter (A/M/D). Renames disabled (see above)."""
        out = _run(
            ["git", "-C", str(wt), "diff", "--no-renames", "--name-status", self._range()]
            + self._policy_pathspec()
        )
        statuses: dict[str, str] = {}
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                statuses[parts[-1]] = parts[0]
        return statuses
