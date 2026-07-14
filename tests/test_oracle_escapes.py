"""Tests for oracle-escape raising + ledger (orchestrator.oracle.escapes)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator.artifacts import TaskArtifacts
from orchestrator.flags import FLAG_KINDS, derive_flags, raise_flag, resolve_flag
from orchestrator.oracle.escapes import (
    GRADES,
    ORACLE_ESCAPE,
    RECURRENCE_THRESHOLD,
    UNCLASSIFIED,
    EscapeRecord,
    derive_ledger,
    iter_escapes,
    raise_oracle_escape,
)

# The class registry is now an ENGINE resource (orchestrator.oracle.classes
# .CLASSES_PATH reads the real committed ENGINE_DIR/oracle/classes.md
# directly), not something a fixture can point at a fake tmp repo. Its real
# seed registry already carries `edge-case` and `regression` - these tests
# use those, no fixture file needed.


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return tmp_path


def _raise(repo: Path, **kw: Any) -> str:
    art = TaskArtifacts(repo, "t1")
    defaults: dict[str, Any] = {
        "class_slug": "regression",
        "grade": "confirmed",
        "summary": "breaks placement radius on update",
        "evidence": "pytest tests/x.py::test_radius fails: expected 5 got 0",
    }
    defaults.update(kw)
    return raise_oracle_escape(art, **defaults)


def test_kind_is_registered() -> None:
    assert ORACLE_ESCAPE in FLAG_KINDS
    assert GRADES == ("confirmed", "plausible")


def test_raise_writes_flag_with_json_detail(repo: Path) -> None:
    flag_id = _raise(repo, gate="test", attribution_note="AC had no radius test")
    art = TaskArtifacts(repo, "t1")
    [flag] = derive_flags(art.read_log())
    assert flag.id == flag_id == "t1#1"
    assert flag.kind == ORACLE_ESCAPE
    assert flag.status == "open"
    assert flag.needs_director is True
    payload = json.loads(flag.detail or "")
    assert payload["class"] == "regression"
    assert payload["grade"] == "confirmed"
    assert payload["evidence"].startswith("pytest")
    assert payload["attribution"] == {"gate": "test", "note": "AC had no radius test"}


def test_attribution_is_optional(repo: Path) -> None:
    _raise(repo)
    art = TaskArtifacts(repo, "t1")
    [flag] = derive_flags(art.read_log())
    assert "attribution" not in json.loads(flag.detail or "")


def test_unregistered_slug_is_rejected(repo: Path) -> None:
    with pytest.raises(ValueError, match="unregistered class slug"):
        _raise(repo, class_slug="totally-new-class")


def test_bad_grade_gate_and_empty_evidence_rejected(repo: Path) -> None:
    with pytest.raises(ValueError, match="grade"):
        _raise(repo, grade="maybe")
    with pytest.raises(ValueError, match="gate"):
        _raise(repo, gate="rw9")
    with pytest.raises(ValueError, match="evidence"):
        _raise(repo, evidence="   ")


def test_rejected_raise_writes_nothing(repo: Path) -> None:
    with pytest.raises(ValueError):
        _raise(repo, class_slug="nope")
    assert TaskArtifacts(repo, "t1").read_log() == []


def test_iter_escapes_scans_all_tasks_and_parses_detail(repo: Path) -> None:
    _raise(repo)  # t1, regression, confirmed
    art2 = TaskArtifacts(repo, "t2")
    raise_oracle_escape(
        art2, class_slug="edge-case", grade="plausible",
        summary="zero-radius placement accepted", evidence="see models.py:120 guard",
    )
    # a non-oracle flag must not appear in the ledger scan
    raise_flag(art2, "note", "unrelated note")
    records = iter_escapes(repo)
    assert [(r.task_id, r.class_slug, r.grade, r.status) for r in records] == [
        ("t1", "regression", "confirmed", "open"),
        ("t2", "edge-case", "plausible", "open"),
    ]


def test_iter_escapes_tolerates_unparseable_detail(repo: Path) -> None:
    art = TaskArtifacts(repo, "t3")
    # hand-written event with a non-JSON detail (defensive: never crash)
    art.append_log(action="flag", id="t3#1", kind=ORACLE_ESCAPE,
                   summary="legacy", detail="not json", needs_director=True)
    [record] = iter_escapes(repo)
    assert record.class_slug is None and record.grade is None


def test_derive_ledger_counts_recurrence_and_skips_dismissed() -> None:
    def rec(task: str, slug: str | None, status: str = "open") -> EscapeRecord:
        return EscapeRecord(task, f"{task}#1", slug, "confirmed", status, "s")

    entries = derive_ledger([
        rec("a", "regression"),
        rec("b", "regression", status="resolved"),
        rec("c", "edge-case"),
        rec("d", "edge-case", status="dismissed"),  # not an escape
        rec("e", None),  # unparseable payload -> unclassified bucket
    ])
    by_slug = {e.class_slug: e for e in entries}
    assert by_slug["regression"].total == 2
    assert by_slug["regression"].open == 1
    assert by_slug["regression"].recurrent is True  # >= RECURRENCE_THRESHOLD
    assert RECURRENCE_THRESHOLD == 2
    assert by_slug["edge-case"].total == 1 and by_slug["edge-case"].recurrent is False
    assert by_slug[UNCLASSIFIED].total == 1
    assert entries[0].class_slug == "regression"  # sorted by total desc


def test_resolved_flag_status_reaches_ledger(repo: Path) -> None:
    flag_id = _raise(repo)
    # allow_oracle_escape = the Director channel (oracle CLI resolve)
    resolve_flag(TaskArtifacts(repo, "t1"), flag_id,
                 note="fixed in abc123; distilled to tests/test_radius.py",
                 allow_oracle_escape=True)
    [record] = iter_escapes(repo)
    assert record.status == "resolved"
