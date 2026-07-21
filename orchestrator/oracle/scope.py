"""Which merged tasks one oracle run reviews (sampling policy).

Reuses policy.classify_blast_radius - NO parallel risk classifier. The
current mode is CALIBRATION (low volume): every L3 and every L2 in the
range is reviewed - L2 auto-merges are the core of what the oracle
measures ("the gates decided alone and nobody watched"), severity lives
at L3, escape probability at L2 - and L1 gets a symbolic deterministic
sample. Switching L2 to a sample is a later, data-driven Director
decision once the calibrated escape rate stabilizes; each run event
records its mode so the time series stays interpretable.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from orchestrator.gitdiff import policy_pathspec
from orchestrator.merge_subject import parse_merge_subject
from orchestrator.oracle import run_git
from orchestrator.policy import L1, classify_blast_radius
from orchestrator.target_policy import TargetPolicy, load_target_policy

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

# Every Nth L1 merge in range order is reviewed (symbolic sample). Purely
# positional, therefore deterministic: the same range always selects the
# same tasks, so record-run agrees with what prepare showed.
L1_SAMPLE_EVERY = 5

CALIBRATION = "calibration"


@dataclass(frozen=True)
class MergedTask:
    """One shipped task: its merge commit and the product files it changed."""

    task_id: str
    merge_sha: str
    changed_files: tuple[str, ...]
    policy: TargetPolicy

    @cached_property
    def bucket(self) -> str:
        # cached: select_scope, the by-bucket folds and trigger.check all
        # read it, and classify_blast_radius scans every pattern per call
        return classify_blast_radius(self.policy, self.changed_files)


@dataclass(frozen=True)
class OracleScope:
    reviewed: tuple[MergedTask, ...]
    skipped: tuple[MergedTask, ...]

    @staticmethod
    def _by_bucket(tasks: Sequence[MergedTask]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for task in tasks:
            out.setdefault(task.bucket, []).append(task.task_id)
        return out

    def reviewed_by_bucket(self) -> dict[str, list[str]]:
        return self._by_bucket(self.reviewed)

    def skipped_by_bucket(self) -> dict[str, list[str]]:
        return self._by_bucket(self.skipped)


def select_scope(tasks: Sequence[MergedTask]) -> OracleScope:
    """Calibration policy (pure): all L2 + L3; every L1_SAMPLE_EVERY-th L1."""
    reviewed: list[MergedTask] = []
    skipped: list[MergedTask] = []
    l1_seen = 0
    for task in tasks:
        if task.bucket == L1:
            if l1_seen % L1_SAMPLE_EVERY == 0:
                reviewed.append(task)
            else:
                skipped.append(task)
            l1_seen += 1
        else:
            reviewed.append(task)
    return OracleScope(tuple(reviewed), tuple(skipped))


def merged_tasks_in_range(
    repo: Path, from_sha: str, to_ref: str = "main"
) -> list[MergedTask]:
    """Agent-task merge commits in ``from_sha..to_ref``, oldest first.

    First-parent walk of main: each ``Merge agent/<task>`` merge is one
    shipped task; its shipped diff = merge commit vs first parent, viewed
    through the policy pathspec (task artifacts excluded - the bucket must
    classify the product change, exactly as the merge gate did). Merges
    with other subjects (the Director's manual work) are not loop output
    and are not oracle scope.
    """
    tasks: list[MergedTask] = []
    # Trusted policy from the repo (the Director's local main checkout), loaded
    # once for the whole range so every task classifies against the same rules.
    policy = load_target_policy(repo)
    for sha, task_id in _iter_task_merges(repo, "--reverse", f"{from_sha}..{to_ref}"):
        _, files = run_git(
            repo, "diff", "--no-renames", "--name-only", f"{sha}^1", sha,
            *policy_pathspec(task_id),
        )
        changed = tuple(files.splitlines())
        if not changed:
            # An artifact-only merge (report-only task: everything under the
            # task's own <agent-dir>/tasks/<id>/ is pathspec-excluded) ships
            # no product diff - nothing for the oracle to review. Letting it
            # through would hit classify_blast_radius's fail-closed L3 for () -
            # right for the merge gate, wrong here: it would fire the
            # high-risk trigger and skew the L3 denominator on every
            # report task.
            continue
        tasks.append(MergedTask(task_id, sha, changed, policy))
    return tasks


def _iter_task_merges(repo: Path, *log_args: str) -> Iterator[tuple[str, str]]:
    """(sha, task_id) per agent merge on the first-parent line, in log order."""
    _, out = run_git(
        repo, "log", "--first-parent", "--merges", "--format=%H%x09%s", *log_args
    )
    for line in out.splitlines():
        sha, _, subject = line.partition("\t")
        task_id = parse_merge_subject(subject)
        if task_id is not None:
            yield sha, task_id


def merge_sha_for_task(repo: Path, task_id: str, to_ref: str = "main") -> str | None:
    """Newest merge commit of agent/<task_id> on ``to_ref``'s first-parent
    line, or None. Same walk merged_tasks_in_range uses."""
    for sha, tid in _iter_task_merges(repo, to_ref):
        if tid == task_id:
            return sha
    return None
