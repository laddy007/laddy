"""Autonomous agent dev-loop orchestrator.

ENGINE_DIR is where laddy is installed (this package's parent — the repo
root of the standalone engine). TARGET_DIR_NAME is the name of the artifact
dir INSIDE a target repo (specs/, tasks/, docker/, security/) — the two are
distinct on purpose (spec 2026-07-13 §3 step 0): the engine never resolves
its own resources through the target, and vice versa.
"""

import os
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent.parent
TARGET_DIR_NAME = os.environ.get("LADDY_TARGET_DIR", ".laddy")


def default_work_root(repo: Path, purpose: str) -> Path:
    """The ``<repo-parent>/<repo-name>-<purpose>-work`` convention - one
    home (local merge, oracle prepare, eval sandbox), derived from the
    repo directory's actual name so a rename keeps working."""
    return repo.parent / f"{repo.name}-{purpose}-work"
