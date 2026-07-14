"""Escape-class registry: registered slugs, not free text (convergence R2).

Recurrence (>= RECURRENCE_THRESHOLD escapes in one class) is only
computable over a stable vocabulary. The registry is an ENGINE resource
(spec §3 step 0) at ``ENGINE_DIR/oracle/classes.md`` - one
``- `slug` — definition`` line per class, extended by commit. The oracle
classifies ONLY into slugs that already exist there; a new class starts
with a registry commit.
"""

from __future__ import annotations

import re

from orchestrator import ENGINE_DIR

CLASSES_PATH = ENGINE_DIR / "oracle" / "classes.md"

# ``- `slug` — one-line definition`` (em-dash or hyphen separator).
_SLUG_LINE = re.compile(r"^-\s+`(?P<slug>[a-z0-9][a-z0-9-]*)`\s+[—-]\s+\S")


def load_class_slugs() -> tuple[str, ...]:
    """Registered slugs in file order; empty when the registry is missing.

    Raises ``ValueError`` on a duplicate slug - the registry is the single
    vocabulary and a duplicate means two competing definitions.
    """
    path = CLASSES_PATH
    if not path.is_file():
        return ()
    slugs: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _SLUG_LINE.match(line.strip())
        if not match:
            continue
        slug = match.group("slug")
        if slug in slugs:
            raise ValueError(f"duplicate class slug {slug!r} in {CLASSES_PATH}")
        slugs.append(slug)
    return tuple(slugs)
