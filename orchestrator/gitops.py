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

    def task_worktree(self, task_id: str, base_task: str | None = None) -> Path:
        """Worktree at the bare `<task>` branch: resume the remote branch if
        it exists, otherwise branch off origin/<default_branch>.

        ``base_task`` (chained queue runs) changes ONLY the fresh-start ref:
        a brand-new task branches off the PREDECESSOR task's pushed branch
        instead of the default branch, so chained work builds on the chain's
        code before it lands. An existing worktree or an existing remote
        `<task>` branch wins over the chain base (the task is already under
        way on its own line); a MISSING predecessor branch is a hard error -
        silently basing a chained task on the default branch would ship it
        without the code it was ordered after.
        """
        branch = self._branch(task_id)
        base = self.ensure_base()
        wt = self.work_root / "wt" / task_id
        if (wt / ".git").exists():
            return wt
        git_out(["git", "-C", str(base), "worktree", "prune"])

        def _remote_ref_exists(ref: str) -> bool:
            return (
                subprocess.run(
                    ["git", "-C", str(base), "rev-parse", "--verify", "--quiet", ref],
                    capture_output=True,
                    check=False,
                ).returncode
                == 0
            )

        remote_ref = f"origin/{branch}"
        if _remote_ref_exists(remote_ref):
            start = remote_ref
        elif base_task is not None:
            chain_ref = f"origin/{self._branch(base_task)}"
            if not _remote_ref_exists(chain_ref):
                raise GitError(
                    f"chain base branch {chain_ref} not found on origin - "
                    f"predecessor task {base_task!r} has not pushed; run it "
                    "first (or drop the chain link)"
                )
            start = chain_ref
        else:
            start = f"origin/{self.default_branch}"
        wt.parent.mkdir(parents=True, exist_ok=True)
        git_out(
            ["git", "-C", str(base), "worktree", "add", "-B", branch, str(wt), start]
        )
        return wt

    def sync_worktree_to_origin(self, wt: Path, task_id: str) -> bool:
        """Fast-forward an EXISTING task worktree to the branch tip on origin
        (fetch first). Returns True when a sync happened, False when origin has
        no such branch (a purely local task - nothing to sync onto).

        ``task_worktree`` reuses an existing worktree WITHOUT fetching, so a
        persisted worktree stays at the commit its last run left. A resume must
        build on the branch as the hub has it NOW: the Director may have pushed a
        spec correction from a separate clone since (director-resume's whole
        point). Skip this and the developer reads the pre-correction spec AND the
        resumed run's final push is rejected non-fast-forward, stranding the task.
        A run that reached a (resumable) terminal committed + pushed everything,
        so the worktree is clean and a hard reset onto ``origin/<task>`` is safe -
        the same reasoning ``refresh_base`` uses to reset the base clone."""
        branch = self._branch(task_id)
        git_out(["git", "-C", str(wt), "fetch", "origin"])
        remote_ref = f"origin/{branch}"
        has_remote = (
            subprocess.run(
                ["git", "-C", str(wt), "rev-parse", "--verify", "--quiet", remote_ref],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        if not has_remote:
            return False
        git_out(["git", "-C", str(wt), "reset", "--hard", remote_ref])
        return True

    def commit_all(self, wt: Path, message: str) -> str | None:
        """Stage and commit everything; None when the tree is clean."""
        if not git_out(["git", "-C", str(wt), "status", "--porcelain"]):
            return None
        git_out(["git", "-C", str(wt), "add", "-A"])
        git_out(["git", "-C", str(wt), *_IDENTITY, "commit", "-q", "-m", message])
        return self.head_sha(wt)

    def chain_base_satisfied(self, wt: Path, base_task: str) -> bool:
        """True iff the predecessor's pushed branch tip is contained in this
        worktree's HEAD - i.e. the chained task really builds on it.

        Guards the chained-queue invariant for a REUSED worktree: task_worktree
        only applies the chain base to a fresh worktree, so one created earlier
        (a clarify run, a plain kickoff) may sit on the default branch or on an
        outdated predecessor tip. The caller fetches (ensure_base) first so
        ``origin/<base>`` is current. Containment is read as "no commit is
        reachable from the base tip that HEAD lacks" (rev-list), a read-only
        ancestry question - this module still performs no history-joining
        operation (spec acceptance 6).
        """
        ref = f"origin/{self._branch(base_task)}"
        proc = subprocess.run(
            ["git", "-C", str(wt), "rev-list", "--count", ref, "^HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return False  # unknown ref -> the chain base is not satisfied
        return proc.stdout.strip() == "0"

    def blob_sha(self, wt: Path, rel_path: str) -> str:
        """The git blob SHA of a working-tree file (``git hash-object``).

        Used as a receipt for the spec at director-resume time: recorded so the
        handback shows whether the ask changed. It is a RECORD only - nothing
        branches on it (that would make it a trust input on a VPS-written log).
        """
        return git_out(["git", "-C", str(wt), "hash-object", rel_path])

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
