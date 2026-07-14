"""The merge-commit subject wire format (frozen; do not change casually).

Every merge local_merge.merge_branch makes into main carries this exact
subject shape, and orchestrator.oracle.scope's range scanner identifies
shipped tasks by parsing it back out - compose and parse live side by
side here so they cannot drift. This is its own leaf module (no other
orchestrator import) so local_merge.py and oracle/scope.py can both
depend on it without an import cycle between them.
"""

from __future__ import annotations

import re

_MERGE_SUBJECT = re.compile(r"^Merge agent/(?P<task>\S+) @ ")


def merge_subject(task_id: str, verified_sha: str) -> str:
    """The subject shape of every agent merge commit."""
    return f"Merge agent/{task_id} @ {verified_sha[:12]} (verified locally)"


def parse_merge_subject(subject: str) -> str | None:
    """The task id if ``subject`` is an agent merge subject, else None
    (the inverse of :func:`merge_subject`)."""
    match = _MERGE_SUBJECT.match(subject)
    return match.group("task") if match else None
