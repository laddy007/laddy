"""Stateless diff sizing/classification helpers - the policy's view of a diff.

Split out of gitops (files split, they don't grow): GitOps provisions
(clone/fetch/worktree/commit/push) and needs instance state; sizing and
classifying a diff needs only a worktree path, the task id, and the base
branch. One implementation serves the VPS decision, the off-VPS recheck
(merge_check / merge_check_local), and the oracle (convergence R1/R3).

The helpers share two deliberate flags so the VPS decision and the off-VPS
recheck agree AND cannot be gamed:
  --no-renames  : a `git mv tests/test_x.py elsewhere` shows as
                  D tests/test_x.py + A elsewhere, so a renamed-away
                  invariant/sensitive test is still seen by the guards.
  :(exclude)<agent-dir>/tasks/<task> : the task commits its OWN artifacts
                  there; counting them would (a) inflate size-based risk
                  and (b) make the VPS pre-artifact-commit decision differ
                  from the CI post-artifact-commit recompute. Only the
                  task's own lane is exempt (M2): files planted in other
                  tasks' lanes count like any other change.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from orchestrator import TARGET_DIR_NAME


class GitError(RuntimeError):
    """A git command failed."""


def git_out(args: Sequence[str], cwd: Path | None = None) -> str:
    """Run a git command, return stripped stdout; GitError on failure."""
    proc = subprocess.run(
        list(args), cwd=cwd, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise GitError(
            f"git command failed ({proc.returncode}): {' '.join(args)}\n{proc.stderr}"
        )
    return proc.stdout.strip()


def policy_pathspec(task_id: str) -> list[str]:
    """The policy view of a diff: everything EXCEPT the task's OWN artifact
    lane <agent-dir>/tasks/<task_id>/**.

    Task artifacts (iteration logs, verdicts) ride every branch; the policy
    and the oracle both classify by the PRODUCT diff. Only the branch's own
    lane is exempt: content a branch plants under ANY OTHER tasks/ path lands
    in the integrated tree, so it is classified like any other change (M2 -
    a blanket tasks/ exclusion let such plants through unclassified). One
    home for the exclusion so no caller restates it (convergence R1/R3).
    ``literal`` pins the task id as a literal path: a wildcard in a hostile
    branch name must not widen the exclusion.
    """
    return ["--", ".", f":(exclude,literal){TARGET_DIR_NAME}/tasks/{task_id}"]


def _range(base_branch: str) -> str:
    return f"origin/{base_branch}...HEAD"


def head_sha(wt: Path) -> str:
    return git_out(["git", "-C", str(wt), "rev-parse", "HEAD"])


def code_sha(wt: Path, task_id: str) -> str:
    """SHA of the last commit touching anything OUTSIDE the task's own
    <agent-dir>/tasks/<task_id>/ lane.

    Gate results are keyed to this, not HEAD: artifact commits (verdicts,
    logs) move HEAD but do not change the reviewed code, so they must not
    invalidate approvals. Any real code commit does - including a commit
    that plants files in ANOTHER task's lane (M2: that content is part of
    what ships, so it must re-key the approvals like any code change).
    """
    out = git_out(
        ["git", "-C", str(wt), "rev-list", "-1", "HEAD"] + policy_pathspec(task_id)
    )
    return out or head_sha(wt)


def changed_files(wt: Path, task_id: str, base_branch: str) -> list[str]:
    out = git_out(
        ["git", "-C", str(wt), "diff", "--no-renames", "--name-only",
         _range(base_branch)]
        + policy_pathspec(task_id)
    )
    return [line for line in out.splitlines() if line]


def diff_text(wt: Path, base_branch: str) -> str:
    return git_out(["git", "-C", str(wt), "diff", _range(base_branch)])


def diff_line_count(wt: Path, task_id: str, base_branch: str) -> int:
    """Added+deleted line count for policy sizing (excludes own artifacts)."""
    out = git_out(
        ["git", "-C", str(wt), "diff", "--no-renames", "--numstat",
         _range(base_branch)]
        + policy_pathspec(task_id)
    )
    total = 0
    for line in out.splitlines():
        added, deleted, *_ = line.split("\t")
        total += (0 if added == "-" else int(added)) + (
            0 if deleted == "-" else int(deleted)
        )
    return total


def changed_statuses(wt: Path, task_id: str, base_branch: str) -> dict[str, str]:
    """path -> git status letter (A/M/D). Renames disabled (see module doc)."""
    out = git_out(
        ["git", "-C", str(wt), "diff", "--no-renames", "--name-status",
         _range(base_branch)]
        + policy_pathspec(task_id)
    )
    statuses: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            statuses[parts[-1]] = parts[0]
    return statuses
