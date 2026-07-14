"""Tests for the oracle escape-class registry (orchestrator.oracle.classes)."""

from __future__ import annotations

import pytest

from orchestrator import ENGINE_DIR
from orchestrator.oracle.classes import CLASSES_PATH, load_class_slugs


def test_classes_path_is_engine_resource() -> None:
    assert CLASSES_PATH == ENGINE_DIR / "oracle" / "classes.md"


def test_seed_registry_parses_nonempty() -> None:
    # CLASSES_PATH is the committed registry at the engine repo root -
    # load_class_slugs reads it directly, no fixture needed.
    slugs = load_class_slugs()
    assert "regression" in slugs
    assert "design-approach" in slugs


def test_parses_slug_lines_in_order_and_skips_prose(monkeypatch, tmp_path) -> None:
    path = tmp_path / "classes.md"
    path.write_text(
        "# registry\n\nprose line\n"
        "- `edge-case` — boundary input inside the changed feature\n"
        "- `regression` — broke existing behavior elsewhere\n",
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr("orchestrator.oracle.classes.CLASSES_PATH", path)
    assert load_class_slugs() == ("edge-case", "regression")


def test_missing_registry_is_empty(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "orchestrator.oracle.classes.CLASSES_PATH", tmp_path / "no-such-file.md"
    )
    assert load_class_slugs() == ()


def test_duplicate_slug_raises(monkeypatch, tmp_path) -> None:
    path = tmp_path / "classes.md"
    path.write_text("- `edge-case` — a\n- `edge-case` — b\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr("orchestrator.oracle.classes.CLASSES_PATH", path)
    with pytest.raises(ValueError, match="duplicate"):
        load_class_slugs()


def test_slug_must_be_kebab_case(monkeypatch, tmp_path) -> None:
    # Free text / uppercase never parses as a slug (convergence R2).
    path = tmp_path / "classes.md"
    path.write_text(
        "- `Edge Case` — not a slug\n- `ok-slug` — fine\n", encoding="utf-8", newline="\n"
    )
    monkeypatch.setattr("orchestrator.oracle.classes.CLASSES_PATH", path)
    assert load_class_slugs() == ("ok-slug",)
