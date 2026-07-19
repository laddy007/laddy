"""Oracle: post-merge, non-blocking measurement of the dev-loop gates.

Design rationale lives in commit history and oracle/classes.md (this
repo carries no docs/ tree by design).

The gates (rw1, rw2, merge-rw) DECIDE go/no-go in the hot path; the oracle
MEASURES them from outside, after merge, with a fresh context. It never
blocks anything - a skipped run degrades to today's status quo, which is
why the trigger is automated but the run stays manual. Its outputs are
``oracle-escape`` flags on already-merged tasks (reusing orchestrator.flags)
and one ``oracle-run`` event per run in the append-only run log
(<agent-dir>/oracle/run-log.jsonl).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from orchestrator import TARGET_DIR_NAME

# Repo-relative home of the oracle's committed data (registry + run log).
ORACLE_DIR = f"{TARGET_DIR_NAME}/oracle"


def run_git(repo: Path, *args: str, check: bool = True) -> tuple[int, str]:
    """Minimal git runner for the oracle's read-mostly git needs.

    Returns (returncode, stripped stdout). With ``check`` (default) a
    non-zero exit raises with stderr in the message.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.returncode, proc.stdout.strip()


def add_detached_worktree(
    repo: Path, ref: str, path: Path, *, check: bool = True
) -> int:
    """Materialize a detached worktree at ``ref`` (prunes stale
    registrations, replaces ``path`` if present). Returns git's exit code
    (nonzero only with ``check=False``). One home for the lifecycle -
    hand-rolled copies of this dance have already drifted once."""
    run_git(repo, "worktree", "prune")
    if path.exists():
        run_git(repo, "worktree", "remove", "--force", str(path), check=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    code, _ = run_git(
        repo, "worktree", "add", "--detach", str(path), ref, check=check
    )
    return code


def remove_worktree(repo: Path, path: Path) -> None:
    """Remove a worktree and prune its registration (idempotent)."""
    run_git(repo, "worktree", "remove", "--force", str(path), check=False)
    run_git(repo, "worktree", "prune")


def commit_exists(repo: Path, ref: str) -> bool:
    """Is ``ref`` resolvable to a commit in this clone? A recorded
    watermark can stop resolving (reset + gc, a fresh clone, a hand-edited
    run log); callers must degrade to an actionable message, not a raw
    git error."""
    code, _ = run_git(
        repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False
    )
    return code == 0
