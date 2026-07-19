"""Remote ask channel: answer clarify/design gate questions from a phone.

The interactive gates ask through ``Deps.ask`` (stdin by default). With
``LADDY_ASK_REMOTE=1`` the launcher swaps in ``RemoteAsk``: each question is
written as a JSON file under ``<work_root>/questions/`` and announced via the
existing ntfy topic; any authorized writer (the laddy-phone PWA, an ntfy
action, or plain ssh + echo) drops the matching ``*.answer.json`` next to it
and the gate resumes. Files are the whole protocol - no daemon, no socket;
consumers just read/write this directory. A side effect: the gate no longer
dies with the SSH session, because it blocks on a file, not a TTY.

Protocol (one outstanding question per task):
  question  <work_root>/questions/<task>.json
            {"task", "id", "question", "asked_at"}
  answer    <work_root>/questions/<task>.answer.json
            {"id", "answer"}

An answer whose ``id`` does not match the outstanding question is a stale
leftover from an earlier question: it is deleted and ignored. Both files are
removed once an answer is consumed, so the directory holds only live
questions.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.handoff import PostFn, _urllib_post

QUESTIONS_DIR_NAME = "questions"


class RemoteAskTimeout(RuntimeError):
    """No answer arrived within the wait budget."""


def questions_dir(work_root: Path) -> Path:
    return work_root / QUESTIONS_DIR_NAME


@dataclass
class RemoteAsk:
    """File-backed ask: write the question, notify, poll for the answer.

    Clock and sleep are injected (engine invariant) so tests never wait.
    """

    work_root: Path
    task_id: str
    topic: str | None = None
    poll_seconds: float = 3.0
    timeout_seconds: float = 7200.0
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = field(default=time.sleep)
    post_fn: PostFn = field(default=_urllib_post)

    def ask(self, question: str) -> str:
        qdir = questions_dir(self.work_root)
        qdir.mkdir(parents=True, exist_ok=True)
        qid = uuid.uuid4().hex
        question_path = qdir / f"{self.task_id}.json"
        answer_path = qdir / f"{self.task_id}.answer.json"
        answer_path.unlink(missing_ok=True)  # never consume a pre-seeded stale answer
        question_path.write_text(
            json.dumps(
                {"task": self.task_id, "id": qid, "question": question},
                ensure_ascii=True,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        self._notify(question)
        deadline = self.now() + self.timeout_seconds
        try:
            while True:
                answer = self._read_answer(answer_path, qid)
                if answer is not None:
                    return answer
                if self.now() >= deadline:
                    raise RemoteAskTimeout(
                        f"no answer for {self.task_id} within "
                        f"{self.timeout_seconds:.0f}s: {question!r}"
                    )
                self.sleep(self.poll_seconds)
        finally:
            question_path.unlink(missing_ok=True)
            answer_path.unlink(missing_ok=True)

    def _read_answer(self, answer_path: Path, qid: str) -> str | None:
        if not answer_path.is_file():
            return None
        try:
            payload = json.loads(answer_path.read_text(encoding="utf-8"))
            matches = payload.get("id") == qid
            answer = payload.get("answer")
        except (OSError, ValueError):
            return None  # partially written; next poll re-reads
        if not matches or not isinstance(answer, str):
            answer_path.unlink(missing_ok=True)  # stale leftover, drop it
            return None
        return answer

    def _notify(self, question: str) -> None:
        if not self.topic:
            return
        try:
            self.post_fn(
                f"https://ntfy.sh/{self.topic}",
                f"{self.task_id}: QUESTION: {question}",
            )
        except OSError:
            pass  # notification loss must never fail the gate
