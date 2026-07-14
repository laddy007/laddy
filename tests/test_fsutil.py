"""Filesystem primitives (orchestrator.fsutil)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from orchestrator.fsutil import remove_tree


def test_remove_tree_missing_path_is_noop(tmp_path: Path) -> None:
    remove_tree(tmp_path / "does-not-exist")  # must not raise


def test_remove_tree_removes_nested_dirs(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "f.txt").write_text("x", encoding="utf-8")
    remove_tree(root)
    assert not root.exists()


def test_remove_tree_clears_readonly_files(tmp_path: Path) -> None:
    # Git pack/object files are written read-only; on Windows a naive rmtree
    # then refuses to unlink them. remove_tree must clear the bit and succeed.
    root = tmp_path / "bare"
    root.mkdir()
    ro = root / "pack.idx"
    ro.write_text("x", encoding="utf-8")
    os.chmod(ro, stat.S_IREAD)
    remove_tree(root)
    assert not root.exists()
