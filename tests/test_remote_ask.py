"""remote_ask: file+ntfy ask channel for the interactive gates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.remote_ask import RemoteAsk, RemoteAskTimeout, questions_dir
from orchestrator.run import Deps, _stdin_ask, _wire_remote_ask


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def _ask(tmp_path: Path, clock: FakeClock, posts: list[tuple[str, str]]) -> RemoteAsk:
    return RemoteAsk(
        work_root=tmp_path,
        task_id="t1",
        topic="topic-x",
        now=clock.now,
        sleep=clock.sleep,
        post_fn=lambda url, msg: posts.append((url, msg)),
    )


def _answer_on_first_sleep(ask: RemoteAsk, tmp_path: Path, answer: dict[str, str]) -> None:
    inner = ask.sleep

    def sleep(seconds: float) -> None:
        inner(seconds)
        (questions_dir(tmp_path) / "t1.answer.json").write_text(
            json.dumps(answer), encoding="utf-8"
        )

    ask.sleep = sleep


def test_ask_writes_question_notifies_and_returns_answer(tmp_path: Path) -> None:
    clock = FakeClock()
    posts: list[tuple[str, str]] = []
    ask = _ask(tmp_path, clock, posts)

    seen: dict[str, object] = {}
    inner = ask.sleep

    def sleep(seconds: float) -> None:
        inner(seconds)
        q = json.loads(
            (questions_dir(tmp_path) / "t1.json").read_text(encoding="utf-8")
        )
        seen.update(q)
        (questions_dir(tmp_path) / "t1.answer.json").write_text(
            json.dumps({"id": q["id"], "answer": "yes, scope A"}), encoding="utf-8"
        )

    ask.sleep = sleep
    assert ask.ask("Scope?") == "yes, scope A"
    assert seen["task"] == "t1" and seen["question"] == "Scope?"
    assert posts == [("https://ntfy.sh/topic-x", "t1: QUESTION: Scope?")]
    # both files consumed
    assert list(questions_dir(tmp_path).iterdir()) == []


def test_stale_answer_wrong_id_is_dropped_and_ignored(tmp_path: Path) -> None:
    clock = FakeClock()
    ask = _ask(tmp_path, clock, [])
    # pre-seeded stale answer from an earlier question must not be consumed
    questions_dir(tmp_path).mkdir(parents=True)
    (questions_dir(tmp_path) / "t1.answer.json").write_text(
        json.dumps({"id": "old", "answer": "stale"}), encoding="utf-8"
    )
    _answer_on_first_sleep(ask, tmp_path, {"id": "also-wrong", "answer": "nope"})
    with pytest.raises(RemoteAskTimeout):
        ask.ask("Q?")


def test_timeout_raises_and_cleans_up(tmp_path: Path) -> None:
    clock = FakeClock()
    ask = _ask(tmp_path, clock, [])
    ask.timeout_seconds = 10.0
    ask.poll_seconds = 3.0
    with pytest.raises(RemoteAskTimeout):
        ask.ask("Anyone there?")
    assert list(questions_dir(tmp_path).iterdir()) == []


def test_no_topic_means_no_post(tmp_path: Path) -> None:
    clock = FakeClock()
    posts: list[tuple[str, str]] = []
    ask = _ask(tmp_path, clock, posts)
    ask.topic = None
    _answer_on_first_sleep(ask, tmp_path, {"id": "x", "answer": "y"})
    with pytest.raises(RemoteAskTimeout):
        ask.ask("Q?")  # wrong id -> timeout; the point is posts stay empty
    assert posts == []


def _config(tmp_path: Path, ask_remote: bool) -> OrchestratorConfig:
    return OrchestratorConfig.from_env(
        {
            "AGENT_REPO_URL": "file:///dev/null",
            "AGENT_WORK_ROOT": str(tmp_path),
            **({"LADDY_ASK_REMOTE": "1"} if ask_remote else {}),
        }
    )


def test_wire_remote_ask_replaces_default_stdin_only(tmp_path: Path) -> None:
    config = _config(tmp_path, ask_remote=True)
    wired = _wire_remote_ask(config, "t1", Deps())
    assert wired.ask is not _stdin_ask

    custom = Deps(ask=lambda q: "custom")
    assert _wire_remote_ask(config, "t1", custom).ask is custom.ask
    assert _wire_remote_ask(config, None, Deps()).ask is _stdin_ask
    off = _config(tmp_path, ask_remote=False)
    assert _wire_remote_ask(off, "t1", Deps()).ask is _stdin_ask
