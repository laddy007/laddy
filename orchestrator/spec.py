"""Task-spec front matter + role composition (design doc S3, S5 step 3).

The spec is a Markdown file, optionally starting with a minimal
``---``-delimited front-matter block (``key: value`` lines plus
``roles: [a, b]`` inline lists - deliberately not full YAML, no dep).

Composition is deterministic: declared in the spec, resolved by table.
No router agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

TASK_TYPES = ("feature", "bug", "spike", "audit", "investigate")

# Deterministic role composition per task type (design S3; decision D10).
COMPOSITIONS: dict[str, tuple[str, ...]] = {
    "feature": ("developer", "rw1", "rw2"),
    "bug": ("explorer", "developer", "debugger", "rw1", "rw2"),
    "spike": ("explorer", "developer", "rw1"),
    "audit": ("investigator", "verify"),
    "investigate": ("investigator", "verify"),
}

REPORT_ONLY_TYPES = ("audit", "investigate")

DRAFT_STATUS = "draft-proposal"
DONE_STATUS = "done"


class SpecError(ValueError):
    """Malformed or unrunnable task spec."""


@dataclass(frozen=True)
class TaskSpec:
    task_type: str
    roles: tuple[str, ...]
    status: str | None
    risk: str | None

    @property
    def report_only(self) -> bool:
        return self.task_type in REPORT_ONLY_TYPES

    @property
    def is_draft(self) -> bool:
        return self.status == DRAFT_STATUS

    @property
    def is_done(self) -> bool:
        return self.status == DONE_STATUS

    def role_plan(self, task_id: str) -> dict[str, object]:
        return {"task": task_id, "type": self.task_type, "roles": list(self.roles)}


_LIST_RE = re.compile(r"^\[(.*)\]$")


def _parse_front_matter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise SpecError(f"front matter line is not 'key: value': {line!r}")
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    raise SpecError("front matter block not closed with '---'")


def parse_spec(path: Path) -> TaskSpec:
    return parse_spec_text(path.read_text(encoding="utf-8"))


def parse_spec_text(text: str) -> TaskSpec:
    fields = _parse_front_matter(text)

    task_type = fields.get("type", "feature")
    if task_type not in TASK_TYPES:
        raise SpecError(f"unknown task type {task_type!r}; allowed: {TASK_TYPES}")

    roles_raw = fields.get("roles", "")
    if roles_raw:
        m = _LIST_RE.match(roles_raw)
        if not m:
            raise SpecError(f"roles must be an inline list [a, b]: {roles_raw!r}")
        roles = tuple(r.strip() for r in m.group(1).split(",") if r.strip())
        if not roles:
            raise SpecError("roles list is empty")
    else:
        roles = COMPOSITIONS[task_type]

    risk = (fields.get("risk") or "").strip().lower() or None

    return TaskSpec(
        task_type=task_type, roles=roles, status=fields.get("status"), risk=risk
    )
