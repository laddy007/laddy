"""Two-phase oracle prompt assembly.

Phase 1 (FINDING, clean): spec + shipped diff + materialized worktree +
registered class slugs. Its builder cannot take logs or verdicts BY
SIGNATURE - contamination control at the type level, matching the
structural cleanliness of inputs.materialize_phase1. Phase 2
(ATTRIBUTION): only now the iteration log and verdicts enter.

Substitution is plain string replace (not str.format): the template holds
literal braces in JSON/log excerpts, and a diff can contain anything.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from orchestrator import ENGINE_DIR

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

# The template is an ENGINE resource (spec §3 step 0): it ships with the
# installed engine, never through the (untrusted) target worktree.
PROMPT_PATH = ENGINE_DIR / "prompts" / "oracle-task-review.md"
PHASE_SPLIT = "<!-- PHASE-2 -->"


def _sections() -> tuple[str, str]:
    text = PROMPT_PATH.read_text(encoding="utf-8")
    if PHASE_SPLIT not in text:
        raise ValueError(f"{PROMPT_PATH} is missing the {PHASE_SPLIT!r} marker")
    phase1, phase2 = text.split(PHASE_SPLIT, 1)
    return phase1.strip() + "\n", phase2.strip() + "\n"


def _fill(template: str, values: Mapping[str, str]) -> str:
    # Single pass over the TEMPLATE only: sequential str.replace would
    # re-scan already-substituted content, so a literal '{diff}' inside
    # e.g. the spec text would expand into the entire shipped diff.
    pattern = re.compile("|".join("{" + re.escape(k) + "}" for k in values))
    return pattern.sub(lambda m: values[m.group(0)[1:-1]], template)


def build_phase1_prompt(
    *,
    task_id: str,
    spec_text: str,
    diff_text: str,
    worktree: Path,
    class_slugs: Sequence[str],
) -> str:
    phase1, _ = _sections()
    return _fill(phase1, {
        "task": task_id,
        "worktree": str(worktree),
        "class_slugs": ", ".join(class_slugs),
        "spec": spec_text,
        "diff": diff_text,
    })


def build_phase2_prompt(
    *,
    task_id: str,
    log_text: str,
    verdicts_text: str,
    class_slugs: Sequence[str],
) -> str:
    _, phase2 = _sections()
    return _fill(phase2, {
        "task": task_id,
        "class_slugs": ", ".join(class_slugs),
        "log": log_text,
        "verdicts": verdicts_text,
    })
