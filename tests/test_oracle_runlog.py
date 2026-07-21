"""Tests for the oracle run log (orchestrator.oracle.runlog)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator.oracle.escapes import EscapeRecord
from orchestrator.oracle.runlog import (
    append_eval,
    append_run,
    escape_rate_series,
    read_evals,
    read_runs,
    run_log_path,
    watermark,
)


def _run_event(repo: Path, *, from_sha: str = "a1", to_sha: str = "b2", **kw: Any) -> None:
    defaults: dict[str, Any] = {
        "reviewed": {"L2": ["t1", "t2"], "L3": ["t3"]},
        "skipped": {"L1": ["t4"]},
        "findings": [{"task": "t1", "flag_id": "t1#1", "grade": "confirmed"}],
        "now": lambda: "2026-07-12T10:00:00Z",
    }
    defaults.update(kw)
    append_run(repo, from_sha=from_sha, to_sha=to_sha, **defaults)


def test_no_log_no_watermark(tmp_path: Path) -> None:
    assert read_runs(tmp_path) == []
    assert watermark(tmp_path) is None


def test_append_is_one_json_line_and_watermark_is_last_to_sha(tmp_path: Path) -> None:
    _run_event(tmp_path)
    _run_event(tmp_path, from_sha="b2", to_sha="c3", findings=[])
    lines = run_log_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    event = json.loads(lines[0])
    assert event["action"] == "oracle-run"
    assert event["mode"] == "calibration"
    assert event["reviewed"]["L2"] == ["t1", "t2"]
    assert event["skipped"]["L1"] == ["t4"]
    assert watermark(tmp_path) == "c3"


def test_read_runs_tolerates_torn_line(tmp_path: Path) -> None:
    _run_event(tmp_path)
    with run_log_path(tmp_path).open("a", encoding="utf-8") as fh:
        fh.write('{"action": "oracle-run", "to_sha": "torn')  # crash mid-append
    assert len(read_runs(tmp_path)) == 1


def test_series_honest_denominator_and_grades(tmp_path: Path) -> None:
    _run_event(
        tmp_path,
        findings=[
            {"task": "t1", "flag_id": "t1#1", "grade": "confirmed"},
            {"task": "t2", "flag_id": "t2#1", "grade": "plausible"},  # open -> pending
            {"task": "t2", "flag_id": "t2#2", "grade": "plausible"},  # dismissed -> gone
            {"task": "t3", "flag_id": "t3#1", "grade": "plausible"},  # resolved -> escape
        ],
    )
    records = [
        EscapeRecord("t1", "t1#1", "regression", "confirmed", "open", "s"),
        EscapeRecord("t2", "t2#1", "edge-case", "plausible", "open", "s"),
        EscapeRecord("t2", "t2#2", "edge-case", "plausible", "dismissed", "s"),
        EscapeRecord("t3", "t3#1", "cross-module", "plausible", "resolved", "s"),
    ]
    series = escape_rate_series(read_runs(tmp_path), records)
    by_bucket = {row["bucket"]: row for row in series}
    # L2 reviewed t1,t2: one confirmed escape + one pending + one dismissed
    assert by_bucket["L2"] == {
        "ts": "2026-07-12T10:00:00Z", "to_sha": "b2", "bucket": "L2",
        "reviewed": 2, "skipped": 0, "escapes": 1, "pending": 1,
    }
    # L3 reviewed t3: plausible upheld (resolved) counts as an escape
    assert by_bucket["L3"]["escapes"] == 1 and by_bucket["L3"]["reviewed"] == 1
    # L1: nothing reviewed, one skipped - recorded, denominator 0
    assert by_bucket["L1"]["reviewed"] == 0 and by_bucket["L1"]["skipped"] == 1


def test_read_runs_skips_non_dict_json_line(tmp_path: Path) -> None:
    # "skip, never raise": a valid-JSON non-dict line (e.g. a bare number)
    # must not crash entry.get(...).
    _run_event(tmp_path)
    with run_log_path(tmp_path).open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("123\n")
    assert len(read_runs(tmp_path)) == 1


def test_watermark_none_when_last_event_missing_to_sha(tmp_path: Path) -> None:
    # A damaged event without to_sha must yield None, not raise KeyError.
    path = run_log_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"action": "oracle-run"}) + "\n")
    assert watermark(tmp_path) is None


def test_append_requires_shas(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        append_run(tmp_path, from_sha="", to_sha="x", reviewed={}, skipped={}, findings=[])


def test_append_and_read_evals(tmp_path: Path) -> None:
    append_eval(
        tmp_path,
        eval_id="e1",
        class_slug="edge-case",
        result="caught",
        caught_by=["rw1"],
        terminal="CAP_REACHED",
        decision=None,
        fix_ref="abc123",
    )
    events = read_evals(tmp_path)
    assert len(events) == 1
    ev = events[0]
    assert ev["action"] == "seeded-eval"
    assert ev["eval"] == "e1"
    assert ev["class"] == "edge-case"
    assert ev["result"] == "caught"
    assert ev["caught_by"] == ["rw1"]
    assert ev["fix_ref"] == "abc123"


def test_append_eval_rejects_unknown_result(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="result"):
        append_eval(
            tmp_path, eval_id="e1", class_slug="edge-case", result="passed",
            caught_by=[], terminal="X", decision=None,
        )


def test_append_and_read_escapes_and_authentic_ids(tmp_path: Path) -> None:
    from orchestrator.oracle.runlog import (
        append_escape,
        authentic_escape_ids,
        read_escapes,
    )

    append_escape(tmp_path, task="t1", flag_id="t1#1", class_slug="regression",
                  grade="confirmed")
    append_escape(tmp_path, task="t2", flag_id="t2#3", class_slug="edge-case",
                  grade="plausible")
    events = read_escapes(tmp_path)
    assert [e["action"] for e in events] == ["escape-raised", "escape-raised"]
    assert events[0]["task"] == "t1" and events[0]["flag_id"] == "t1#1"
    assert authentic_escape_ids(tmp_path) == {("t1", "t1#1"), ("t2", "t2#3")}


def test_append_escape_requires_task_and_flag_id(tmp_path: Path) -> None:
    from orchestrator.oracle.runlog import append_escape

    with pytest.raises(ValueError, match="required"):
        append_escape(tmp_path, task="", flag_id="t1#1", class_slug="c", grade="confirmed")


def test_escape_events_do_not_move_watermark_or_appear_in_read_runs(tmp_path: Path) -> None:
    from orchestrator.oracle.runlog import append_escape

    append_run(tmp_path, from_sha="a" * 40, to_sha="b" * 40,
               reviewed={"L2": ["t1"]}, skipped={}, findings=[])
    append_escape(tmp_path, task="t1", flag_id="t1#1", class_slug="regression",
                  grade="confirmed")
    assert watermark(tmp_path) == "b" * 40  # oracle-run events only
    assert len(read_runs(tmp_path)) == 1


def test_eval_events_do_not_move_watermark_and_are_invisible_to_read_runs(
    tmp_path: Path,
) -> None:
    append_run(
        tmp_path, from_sha="a" * 40, to_sha="b" * 40,
        reviewed={"L2": ["t1"]}, skipped={}, findings=[],
    )
    append_eval(
        tmp_path, eval_id="e1", class_slug="edge-case", result="missed",
        caught_by=[], terminal="MERGE_DECIDED:stop_before_merge",
        decision="stop_before_merge",
    )
    assert watermark(tmp_path) == "b" * 40  # oracle-run events only
    assert len(read_runs(tmp_path)) == 1
    assert len(read_evals(tmp_path)) == 1
