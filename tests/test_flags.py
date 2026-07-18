"""Tests for the event-sourced flag channel (orchestrator.flags)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from orchestrator.artifacts import TaskArtifacts
from orchestrator.flags import (
    FLAG_ACTIONS,
    FLAG_KINDS,
    FLAG_RESOLUTIONS,
    LOOP_FLAG_KINDS,
    ORACLE_ESCAPE,
    derive_flags,
    open_flags,
    raise_flag,
    resolve_flag,
)


def _flag(fid: str, **kw: object) -> dict[str, object]:
    return {"ts": kw.pop("ts", "t"), "action": "flag", "id": fid, "kind": "note",
            "summary": "s", "needs_director": False, **kw}


def _resolved(fid: str, **kw: object) -> dict[str, object]:
    return {"ts": kw.pop("ts", "t"), "action": "flag-resolved", "id": fid,
            "resolution": "resolved", **kw}


# --- derive_flags (pure) -----------------------------------------------------


def test_derive_flags_raise_only_is_open() -> None:
    [flag] = derive_flags([_flag("t#1", kind="blocker", summary="db down")])
    assert flag.id == "t#1"
    assert flag.kind == "blocker"
    assert flag.summary == "db down"
    assert flag.status == "open"
    assert flag.resolution is None and flag.resolved_ts is None and flag.note is None


def test_derive_flags_resolved_carries_note_and_ts() -> None:
    [flag] = derive_flags(
        [
            _flag("t#1", ts="t1"),
            _resolved("t#1", ts="t2", note="Director: approved"),
        ]
    )
    assert flag.status == "resolved"
    assert flag.resolution == "resolved"
    assert flag.note == "Director: approved"
    assert flag.raised_ts == "t1"
    assert flag.resolved_ts == "t2"


def test_derive_flags_dismissed_status() -> None:
    [flag] = derive_flags(
        [_flag("t#1"), _resolved("t#1", resolution="dismissed", note="won't do")]
    )
    assert flag.status == "dismissed"
    assert flag.resolution == "dismissed"


def test_derive_flags_resolve_of_unknown_id_is_ignored() -> None:
    flags = derive_flags([_flag("t#1"), _resolved("t#99")])
    assert [f.id for f in flags] == ["t#1"]
    assert flags[0].status == "open"


def test_derive_flags_double_resolve_ignores_second() -> None:
    [flag] = derive_flags(
        [
            _flag("t#1"),
            _resolved("t#1", note="first"),
            _resolved("t#1", resolution="dismissed", note="second"),
        ]
    )
    assert flag.status == "resolved"
    assert flag.note == "first"


def test_derive_flags_preserves_raise_order_and_needs_director() -> None:
    flags = derive_flags(
        [
            _flag("t#1", needs_director=False),
            _flag("t#2", needs_director=True),
            _flag("t#3", needs_director=False),
        ]
    )
    assert [f.id for f in flags] == ["t#1", "t#2", "t#3"]
    assert [f.needs_director for f in flags] == [False, True, False]


def test_open_flags_puts_needs_director_first_then_raise_order() -> None:
    entries = [
        _flag("t#1", needs_director=False),
        _flag("t#2", needs_director=True),
        _flag("t#3", needs_director=False),
        _flag("t#4", needs_director=True),
        _resolved("t#3"),  # resolved -> excluded
    ]
    assert [f.id for f in open_flags(entries)] == ["t#2", "t#4", "t#1"]


def test_flag_actions_and_sets_are_closed() -> None:
    assert FLAG_ACTIONS == ("flag", "flag-resolved")
    assert set(FLAG_KINDS) == {"deviation", "debt", "blocker", "question", "note", "oracle-escape"}
    assert set(FLAG_RESOLUTIONS) == {"resolved", "dismissed"}


def test_loop_flag_kinds_exclude_oracle_escape() -> None:
    # The loop CLI raises only these: an oracle-escape enters solely through
    # the validated Director channel (oracle.escapes.raise_oracle_escape) -
    # the system under measurement must not write to the measuring instrument.
    assert set(LOOP_FLAG_KINDS) == set(FLAG_KINDS) - {ORACLE_ESCAPE}
    assert ORACLE_ESCAPE == "oracle-escape"


# --- raise_flag / resolve_flag (write helpers) -------------------------------


def _art(tmp_path: Path) -> TaskArtifacts:
    return TaskArtifacts(tmp_path, "mytask", now=lambda: "now")


def test_raise_flag_assigns_sequential_ids(tmp_path: Path) -> None:
    art = _art(tmp_path)
    assert raise_flag(art, "deviation", "one") == "mytask#1"
    assert raise_flag(art, "debt", "two", detail="d", round=2) == "mytask#2"
    assert raise_flag(art, "note", "three", needs_director=True) == "mytask#3"


def test_raise_flag_id_skips_over_a_planted_gap_id(tmp_path: Path) -> None:
    # M-D6-2: a branch pre-plants ONE flag event with a GAP id (mytask#2, no
    # #1). A count-based id would assign mytask#2 again (raised count == 1),
    # colliding with the plant; derive_flags then silently drops the later
    # duplicate, so the genuine flag vanishes from every derived view. Max+1
    # assigns mytask#3 - strictly above every existing numeric id - and the
    # genuine flag survives.
    art = _art(tmp_path)
    art.append_log(action="flag", id="mytask#2", kind="note",
                   summary="planted", needs_director=False)
    fid = raise_flag(art, ORACLE_ESCAPE, "genuine escape", allow_oracle_escape=True)
    assert fid == "mytask#3"  # NOT mytask#2 (the count-based collision)
    derived = derive_flags(art.read_log())
    assert [f.id for f in derived] == ["mytask#2", "mytask#3"]
    assert any(f.kind == ORACLE_ESCAPE for f in derived)  # escape not suppressed


def test_raise_flag_ignores_non_numeric_suffix_ids_for_max(tmp_path: Path) -> None:
    # A hand-written flag with a non-numeric #suffix must not derail max+1: it
    # contributes 0 to the max, so the next numeric id is still assigned.
    art = _art(tmp_path)
    art.append_log(action="flag", id="mytask#legacy", kind="note",
                   summary="odd", needs_director=False)
    assert raise_flag(art, "note", "next") == "mytask#1"


def test_raise_flag_writes_exactly_one_line_and_is_append_only(tmp_path: Path) -> None:
    art = _art(tmp_path)
    art.append_log(action="developer", outcome="ok")
    before = art.read_log()
    raise_flag(art, "blocker", "boom", detail="stack", round=3, needs_director=True)
    after = art.read_log()
    assert after[: len(before)] == before  # prior lines untouched
    assert len(after) == len(before) + 1
    event = after[-1]
    assert event == {
        "ts": "now",
        "action": "flag",
        "id": "mytask#1",
        "kind": "blocker",
        "summary": "boom",
        "needs_director": True,
        "detail": "stack",
        "round": 3,
    }


def test_raise_flag_omits_optional_fields_when_absent(tmp_path: Path) -> None:
    art = _art(tmp_path)
    raise_flag(art, "note", "just a note")
    event = art.read_log()[-1]
    assert "detail" not in event and "round" not in event
    assert event["needs_director"] is False


def test_raise_flag_rejects_bad_kind_and_empty_summary(tmp_path: Path) -> None:
    art = _art(tmp_path)
    with pytest.raises(ValueError):
        raise_flag(art, "nope", "x")
    with pytest.raises(ValueError):
        raise_flag(art, "note", "   ")
    assert art.read_log() == []  # nothing written on rejection


def test_raise_flag_refuses_oracle_escape_outside_director_channel(tmp_path: Path) -> None:
    # LOW: the LIBRARY boundary must refuse a forged oracle-escape itself -
    # argparse's LOOP_FLAG_KINDS choices only guard the CLI layer, and the
    # system under measurement must never write to the measuring instrument's
    # data series through a direct raise_flag call.
    art = _art(tmp_path)
    with pytest.raises(ValueError, match="oracle"):
        raise_flag(art, ORACLE_ESCAPE, "forged escape")
    assert art.read_log() == []  # nothing written on rejection


def test_raise_flag_oracle_escape_via_director_channel(tmp_path: Path) -> None:
    # the validated oracle channel (escapes.raise_oracle_escape) opts in
    art = _art(tmp_path)
    fid = raise_flag(
        art, ORACLE_ESCAPE, "escape", needs_director=True, allow_oracle_escape=True
    )
    [flag] = derive_flags(art.read_log())
    assert fid == "mytask#1" and flag.kind == ORACLE_ESCAPE


def test_resolve_flag_open_writes_event_and_returns_true(tmp_path: Path) -> None:
    art = _art(tmp_path)
    fid = raise_flag(art, "question", "why?")
    assert resolve_flag(art, fid, note="because") is True
    [flag] = derive_flags(art.read_log())
    assert flag.status == "resolved" and flag.note == "because"


def test_resolve_flag_dismissed(tmp_path: Path) -> None:
    art = _art(tmp_path)
    fid = raise_flag(art, "note", "noise")
    assert resolve_flag(art, fid, resolution="dismissed") is True
    assert derive_flags(art.read_log())[0].status == "dismissed"


def test_resolve_flag_unknown_id_returns_false_writes_nothing(tmp_path: Path) -> None:
    art = _art(tmp_path)
    raise_flag(art, "note", "x")
    before = art.read_log()
    assert resolve_flag(art, "mytask#99") is False
    assert art.read_log() == before


def test_resolve_flag_already_resolved_returns_false_writes_nothing(tmp_path: Path) -> None:
    art = _art(tmp_path)
    fid = raise_flag(art, "note", "x")
    resolve_flag(art, fid)
    before = art.read_log()
    assert resolve_flag(art, fid) is False
    assert art.read_log() == before


def test_resolve_flag_missing_log_creates_no_artifact(tmp_path: Path) -> None:
    # resolve on a task with no artifacts yet must return False AND write
    # nothing - not even create an empty iteration-log.jsonl. The lock's
    # O_CREAT would otherwise leave a stray file that commit_all sweeps in.
    art = _art(tmp_path)
    assert resolve_flag(art, "mytask#1") is False
    assert not art.dir.exists()


def test_resolve_flag_rejects_bad_resolution(tmp_path: Path) -> None:
    art = _art(tmp_path)
    fid = raise_flag(art, "note", "x")
    with pytest.raises(ValueError):
        resolve_flag(art, fid, resolution="approved")


def test_resolve_flag_refuses_oracle_escape_outside_director_channel(tmp_path: Path) -> None:
    # An oracle-escape dismissed in-loop would silently vanish from the
    # escape ledger (derive_ledger skips dismissed) with no Director
    # adjudication - the default path must refuse, loudly.
    art = _art(tmp_path)
    fid = raise_flag(
        art, ORACLE_ESCAPE, "escape", needs_director=True, allow_oracle_escape=True
    )
    with pytest.raises(ValueError, match="oracle"):
        resolve_flag(art, fid, resolution="dismissed")
    [flag] = derive_flags(art.read_log())
    assert flag.status == "open"  # nothing written on rejection


def test_resolve_flag_oracle_escape_via_director_channel(tmp_path: Path) -> None:
    art = _art(tmp_path)
    fid = raise_flag(
        art, ORACLE_ESCAPE, "escape", needs_director=True, allow_oracle_escape=True
    )
    assert resolve_flag(
        art, fid, resolution="dismissed", allow_oracle_escape=True
    )
    [flag] = derive_flags(art.read_log())
    assert flag.status == "dismissed"


def test_concurrent_raise_flag_assigns_unique_ids_no_loss(tmp_path: Path) -> None:
    """The read-count-then-append id assignment is lock-serialized: N threads
    racing on one task's log get N distinct ids and no flag is dropped by
    derive_flags. Without the flock this collides on `mytask#1` and loses
    flags (rw2 blocker). flock blocks in-kernel, so no test really sleeps."""
    art = _art(tmp_path)
    n = 12
    ids: list[str] = []
    ids_lock = threading.Lock()
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()  # maximize the race window on the read-then-append
        fid = raise_flag(art, "note", f"flag {i}")
        with ids_lock:
            ids.append(fid)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(ids)) == n  # every id unique, none collided
    assert set(ids) == {f"mytask#{k}" for k in range(1, n + 1)}
    derived = derive_flags(art.read_log())
    assert len(derived) == n  # no flag silently dropped
    assert {f.id for f in derived} == set(ids)
