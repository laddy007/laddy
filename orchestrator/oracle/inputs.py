"""Phase-1 clean input: contamination control is STRUCTURAL, not prose.

Task artifacts (iteration-log.jsonl, reviewer verdicts, human-summary) are
committed on the branch and, after merge, sit right next to the spec - one
``cat`` away from pumping the oracle full of the gates' blind spot. So
phase 1 never runs in the live tree: it gets a materialized worktree at
the shipped merge commit from which <agent-dir>/tasks/** is REMOVED except
the reviewed task's own spec (the bar). The same applies to the diff shown
to phase 1: policy pathspec, task artifacts excluded. Both properties are
enforced by tests/agent_orchestrator/test_oracle_inputs.py, per the
design's honesty rule ("cleanliness is tested, never declared").
"""

from __future__ import annotations

import shutil
from pathlib import Path

from orchestrator import TARGET_DIR_NAME
from orchestrator.artifacts import SPEC
from orchestrator.gitops import policy_pathspec
from orchestrator.oracle import add_detached_worktree, remove_worktree, run_git


def merge_diff(repo: Path, merge_sha: str) -> str:
    """The shipped product diff: merge commit vs first parent, artifacts
    excluded (what main actually gained in product terms)."""
    _, out = run_git(
        repo, "diff", "--no-renames", f"{merge_sha}^1", merge_sha, *policy_pathspec()
    )
    return out


def _worktree_path(task_id: str, work_root: Path) -> Path:
    return work_root / f"oracle-{task_id}"


def materialize_phase1(
    repo: Path, task_id: str, merge_sha: str, work_root: Path
) -> Path:
    """Detached worktree at the merge sha, stripped of every task artifact
    except the reviewed task's spec. Returns the worktree path. Re-running
    for the same task replaces the previous worktree."""
    wt = _worktree_path(task_id, work_root)
    add_detached_worktree(repo, merge_sha, wt)
    tasks_dir = wt / TARGET_DIR_NAME / "tasks"
    spec_src = tasks_dir / task_id / SPEC
    if not spec_src.is_file():
        # --skip-clarify tasks never get tasks/<id>/spec.md committed
        # (copy_spec runs only in the clarify phase); the spec the loop
        # actually ran from is specs/<id>.md at the same sha.
        spec_src = wt / TARGET_DIR_NAME / "specs" / f"{task_id}.md"
    if not spec_src.is_file():
        remove_worktree(repo, wt)
        raise FileNotFoundError(
            f"{task_id} has no committed spec at {merge_sha[:12]} (neither "
            f"tasks/{task_id}/{SPEC} nor specs/{task_id}.md) - the spec is "
            "the oracle's bar; phase 1 cannot run without it"
        )
    spec_text = spec_src.read_text(encoding="utf-8")
    if tasks_dir.is_dir():
        shutil.rmtree(tasks_dir)
    keep = tasks_dir / task_id
    keep.mkdir(parents=True)
    (keep / SPEC).write_text(spec_text, encoding="utf-8", newline="\n")
    return wt


def remove_phase1(repo: Path, task_id: str, work_root: Path) -> None:
    """Remove the phase-1 worktree (idempotent)."""
    remove_worktree(repo, _worktree_path(task_id, work_root))
