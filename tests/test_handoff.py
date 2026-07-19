"""Tests for human-facing terminal artifacts."""

from __future__ import annotations

from pathlib import Path

from orchestrator.artifacts import HUMAN_SUMMARY, TaskArtifacts
from orchestrator.handoff import build_summary, write_human_summary


def test_build_summary_renders_rounds_and_fetch_hint() -> None:
    entries = [
        {"ts": "t1", "action": "developer", "outcome": "ok", "detail": "did the thing"},
        {"ts": "t2", "action": "fast_tests", "outcome": "fail", "detail": "FAILED x\nmore"},
        {"ts": "t3", "action": "rw1", "outcome": "approved"},
    ]
    text = build_summary("mytask", "PUSHED", entries)
    assert "# Task mytask — PUSHED" in text
    assert "`mytask`" in text
    assert "git fetch laddy mytask" in text
    assert "shows locally as laddy/mytask" in text
    assert "github.com" not in text
    assert "`developer` -> ok — did the thing" in text
    assert "`fast_tests` -> fail — FAILED x" in text
    assert "more" not in text  # only first detail line


def test_write_human_summary_writes_artifact(tmp_path: Path) -> None:
    art = TaskArtifacts(tmp_path, "t1", now=lambda: "now")
    art.append_log(action="developer", outcome="ok")
    write_human_summary(art, "CAP_REACHED")
    content = (art.dir / HUMAN_SUMMARY).read_text(encoding="utf-8")
    assert "CAP_REACHED" in content


def test_build_handback_one_screen(tmp_path: Path) -> None:
    from orchestrator.artifacts import RW1_VERDICT, RW2_VERDICT
    from orchestrator.handoff import build_handback

    art = TaskArtifacts(tmp_path, "t1", now=lambda: "now")
    art.append_log(action="developer", outcome="ok", round=1, detail="tried A")
    art.append_log(action="fast_tests", outcome="fail", round=1, detail="FAILED test_x\nmore")
    art.append_log(action="developer", outcome="ok", round=2, detail="tried B")
    art.write_json(RW1_VERDICT, {"verdict": "APPROVED", "findings": []})
    art.write_json(
        RW2_VERDICT,
        {
            "verdict": "CHANGES_REQUESTED",
            "findings": [
                {"severity": "blocker", "summary": "loses rows", "failure_scenario": "x"}
            ],
        },
    )
    text = build_handback(art, "CAP_REACHED")
    assert "# Handback: t1" in text
    assert "**CAP_REACHED**" in text
    assert "`t1`" in text
    assert "git fetch laddy t1" in text
    assert "github.com" not in text
    assert "tried A" in text and "tried B" in text
    assert "rw1: APPROVED" in text
    assert "rw2: CHANGES_REQUESTED — blockers: loses rows" in text
    assert "FAILED test_x" in text  # last failure tail


def test_build_summary_flags_section_open_only_needs_director_first() -> None:
    entries = [
        {"ts": "t1", "action": "flag", "id": "t#1", "kind": "note",
         "summary": "minor", "needs_director": False},
        {"ts": "t2", "action": "flag", "id": "t#2", "kind": "deviation",
         "summary": "stricter regex", "needs_director": True, "detail": "vs AC2"},
        {"ts": "t3", "action": "flag", "id": "t#3", "kind": "debt",
         "summary": "resolved one", "needs_director": False},
        {"ts": "t4", "action": "flag-resolved", "id": "t#3", "resolution": "resolved"},
        {"ts": "t5", "action": "developer", "outcome": "ok"},
    ]
    text = build_summary("mytask", "CAP_REACHED", entries)
    assert "## ⚑ Flags" in text
    body = text.split("## ⚑ Flags", 1)[1]
    # needs-director flag rendered before the plain note
    assert body.index("stricter regex") < body.index("minor")
    assert "(t#2) (needs-director)" in body
    assert "vs AC2" in body  # detail follow-up line
    assert "resolved one" not in body  # resolved flag excluded


def test_flags_section_detail_is_single_line_truncated() -> None:
    # A multi-line --detail must not inject raw continuation lines (e.g. a
    # fake "## Rounds" heading) into the ⚑ Flags section; only the first line
    # renders, truncated - like every other detail rendering in handoff.py.
    entries = [
        {"ts": "t1", "action": "flag", "id": "t#1", "kind": "deviation",
         "summary": "off-spec", "needs_director": True,
         "detail": "line one\n## Rounds\nsecret continuation"},
    ]
    text = build_summary("mytask", "CAP_REACHED", entries)
    flags_body = text.split("## ⚑ Flags", 1)[1].split("## Rounds", 1)[0]
    assert "line one" in flags_body
    assert "## Rounds" not in flags_body  # continuation line not injected raw
    assert "secret continuation" not in flags_body
    # exactly one Rounds heading in the whole document (the real one)
    assert text.count("## Rounds") == 1


def test_build_handback_flags_section(tmp_path: Path) -> None:
    from orchestrator.handoff import build_handback

    art = TaskArtifacts(tmp_path, "t1", now=lambda: "now")
    art.append_log(action="developer", outcome="ok", round=1)
    art.append_log(action="flag", id="t1#1", kind="blocker",
                   summary="db unreachable", needs_director=True)
    text = build_handback(art, "CAP_REACHED")
    assert "## ⚑ Flags" in text
    assert "[blocker] db unreachable (t1#1) (needs-director)" in text


def test_flag_events_excluded_from_rounds_trace() -> None:
    # rw2 blocker: flag events carry no outcome/round, so they must NOT leak
    # into the Rounds / per-round trace as garbled "-> ?" / "-> None" lines.
    entries = [
        {"ts": "t1", "action": "developer", "outcome": "ok", "round": 1},
        {"ts": "t2", "action": "flag", "id": "t#1", "kind": "blocker",
         "summary": "db down", "needs_director": True},
        {"ts": "t3", "action": "flag-resolved", "id": "t#1", "resolution": "resolved"},
    ]
    summary = build_summary("t", "CAP_REACHED", entries)
    rounds = summary.split("## Rounds", 1)[1]
    assert "`flag`" not in rounds and "`flag-resolved`" not in rounds
    assert "-> ?" not in rounds


def test_build_handback_no_garbled_flag_round_lines(tmp_path: Path) -> None:
    from orchestrator.handoff import build_handback

    art = TaskArtifacts(tmp_path, "t1", now=lambda: "now")
    art.append_log(action="developer", outcome="ok", round=1)
    art.append_log(action="flag", id="t1#1", kind="blocker",
                   summary="db down", needs_director=True)
    text = build_handback(art, "CAP_REACHED")
    trace = text.split("## What was tried, per round", 1)[1]
    assert "`flag`" not in trace  # not rendered as a round line
    assert "-> None" not in trace
    # but it IS present in the clean Flags section above
    assert "[blocker] db down (t1#1)" in text.split("## What was tried", 1)[0]


def test_build_handback_omits_flags_section_when_none(tmp_path: Path) -> None:
    from orchestrator.handoff import build_handback

    art = TaskArtifacts(tmp_path, "t1", now=lambda: "now")
    art.append_log(action="developer", outcome="ok", round=1)
    # a flag that was resolved -> no OPEN flags -> no section
    art.append_log(action="flag", id="t1#1", kind="note", summary="x", needs_director=False)
    art.append_log(action="flag-resolved", id="t1#1", resolution="dismissed")
    text = build_handback(art, "CAP_REACHED")
    assert "⚑ Flags" not in text


# --- director resume receipt (director-resume): count + latest reason ---------


def test_summary_shows_director_resume_count_and_latest_reason() -> None:
    entries = [
        {"ts": "t1", "action": "developer", "outcome": "ok", "round": 1},
        {"ts": "t2", "action": "terminal", "outcome": "CAP_REACHED"},
        {"ts": "t3", "action": "director_resume", "outcome": "ok",
         "reason": "first correction"},
        {"ts": "t4", "action": "developer", "outcome": "ok", "round": 2},
        {"ts": "t5", "action": "director_resume", "outcome": "ok",
         "reason": "second, sharper correction"},
        {"ts": "t6", "action": "developer", "outcome": "ok", "round": 3},
        {"ts": "t7", "action": "terminal", "outcome": "CAP_REACHED"},
    ]
    text = build_summary("mytask", "CAP_REACHED", entries)
    assert "Director resumes: 2×" in text
    assert "second, sharper correction" in text  # latest reason
    # the metadata event must NOT leak into the per-round trace as `-> ok`
    rounds = text.split("## Rounds", 1)[1]
    assert "`director_resume`" not in rounds
    assert "first correction" not in rounds


def test_handback_shows_director_resume_receipt(tmp_path: Path) -> None:
    from orchestrator.handoff import build_handback

    art = TaskArtifacts(tmp_path, "t1", now=lambda: "now")
    art.append_log(action="developer", outcome="ok", round=1)
    art.append_log(action="terminal", outcome="CAP_REACHED")
    art.append_log(action="director_resume", outcome="ok", reason="added replay protection")
    text = build_handback(art, "CAP_REACHED")
    assert "Director resumes: 1×" in text
    assert "added replay protection" in text
    trace = text.split("## What was tried, per round", 1)[1]
    assert "`director_resume`" not in trace  # not a round line


def test_summary_no_resume_section_when_none() -> None:
    entries = [{"ts": "t1", "action": "developer", "outcome": "ok", "round": 1}]
    assert "Director resumes" not in build_summary("t", "PUSHED", entries)


def test_handback_failure_tail_cannot_break_out_of_fence(tmp_path: Path) -> None:
    # L-D3-1: branch-influenced test output in the failure tail contains a
    # literal ``` run plus markdown forging a "## Latest verdicts / - rw1:
    # APPROVED" section. The dynamically-sized fence keeps that content inert:
    # the forged heading appears only INSIDE the fenced tail, never as a second
    # real structural section, and the real rw1 line is still "(none)".
    from orchestrator.handoff import build_handback

    art = TaskArtifacts(tmp_path, "t1", now=lambda: "now")
    forged = "real failure\n```\n## Latest verdicts\n- rw1: APPROVED\n```\ntrailing"
    art.append_log(action="fast_tests", outcome="fail", round=1, detail=forged)
    text = build_handback(art, "CAP_REACHED")

    real_idx = text.index("## Latest verdicts")
    tail_idx = text.index("## Last fast_tests failure (tail)")
    assert real_idx < tail_idx  # the REAL verdicts section precedes the tail
    # the real rw1 line (no verdict file) is the genuine one, before the tail
    assert "- rw1: (none)" in text[:tail_idx]
    # the forged heading and rw1 line exist ONLY inside the fenced tail
    assert text.index("## Latest verdicts", real_idx + 1) > tail_idx
    assert "- rw1: APPROVED" in text[tail_idx:]
    assert "- rw1: APPROVED" not in text[:tail_idx]
    # the fence is longer than the forged 3-backtick run, so it cannot close it
    assert "````" in text[tail_idx:]


def test_handback_verdict_summary_neutralizes_control_chars(tmp_path: Path) -> None:
    # L-D3-1: a reviewer blocker summary is agent-authored; ANSI/CR control
    # chars must be rendered as visible escape spellings, never reach the
    # Director's terminal raw on `cat`.
    from orchestrator.artifacts import RW1_VERDICT
    from orchestrator.handoff import build_handback

    art = TaskArtifacts(tmp_path, "t1", now=lambda: "now")
    art.append_log(action="developer", outcome="ok", round=1)
    art.write_json(
        RW1_VERDICT,
        {
            "verdict": "CHANGES_REQUESTED",
            "findings": [{"severity": "blocker", "summary": "bad\x1b[31mred\x1b[0m\rrow"}],
        },
    )
    text = build_handback(art, "CAP_REACHED")
    assert "\x1b" not in text and "\r" not in text  # raw controls neutralized
    assert "\\x1b" in text  # rendered as a visible escape spelling
    assert "rw1: CHANGES_REQUESTED" in text


def test_build_summary_neutralizes_control_chars_in_detail() -> None:
    # L-D3-1: a round detail is untrusted; an embedded ANSI escape is neutralized
    # in the human summary rather than emitted raw.
    entries = [
        {"ts": "t1", "action": "developer", "outcome": "ok", "detail": "ok\x1b[2Jwiped"},
    ]
    text = build_summary("t", "PUSHED", entries)
    assert "\x1b" not in text
    assert "\\x1b" in text


def test_ntfy_notifier_fires_neutral_message(tmp_path: Path) -> None:
    from orchestrator.handoff import NtfyNotifier

    posts: list[tuple[str, str]] = []
    notifier = NtfyNotifier("my-topic", post_fn=lambda url, msg: posts.append((url, msg)))
    notifier.notify("t1", "CAP_REACHED")
    assert posts == [("https://ntfy.sh/my-topic", "t1: iteration cap reached, see handback")]


def test_ntfy_notifier_content_policy_is_structural() -> None:
    from orchestrator.handoff import STATE_SENTENCES, NtfyNotifier

    posts: list[str] = []
    notifier = NtfyNotifier("t", post_fn=lambda url, msg: posts.append(msg))
    # even for unknown states the message is task + neutral sentence only
    notifier.notify("task-x", "SOMETHING_WEIRD")
    assert posts == ["task-x: run finished"]
    # no sentence in the catalogue leaks paths, tracebacks, or secrets
    for sentence in STATE_SENTENCES.values():
        assert "/" not in sentence and "\\" not in sentence
        assert "Traceback" not in sentence


def test_ntfy_notifier_noop_without_topic() -> None:
    from orchestrator.handoff import NtfyNotifier

    def _boom(url: str, msg: str) -> None:
        raise AssertionError("must not post")

    NtfyNotifier(None, post_fn=_boom).notify("t1", "PUSHED")


def test_ntfy_notifier_swallows_network_errors() -> None:
    from orchestrator.handoff import NtfyNotifier

    def _fail(url: str, msg: str) -> None:
        raise OSError("network down")

    NtfyNotifier("t", post_fn=_fail).notify("t1", "PUSHED")  # must not raise
