"""Pure request logic for laddy-phone: validation, question files, argv.

Everything here is directly unit-testable without a socket: the HTTP layer
(``phone.server``) parses the request and calls one of these functions.
The subprocess runner is a plain callable injected into the app so tests
never spawn a real orchestrator.

The question/answer file protocol is owned by ``orchestrator/remote_ask.py``:
  question  <work_root>/questions/<task>.json         {"task","id","question"}
  answer    <work_root>/questions/<task>.answer.json  {"id","answer"}
Files are the whole protocol - this module just reads/writes that directory.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

# Mirrors orchestrator.remote_ask.QUESTIONS_DIR_NAME (not imported: the phone
# server must not drag the whole engine import graph into every request).
QUESTIONS_DIR_NAME = "questions"

# Same shape kickoff.sh enforces; also the path-traversal guard (no /, no ..
# as a path step, no NUL - the id becomes a single filename component).
TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

DEFAULT_LOG_LINES = 200
MAX_LOG_LINES = 2000
RUN_TIMEOUT_SECONDS = 60.0


class BadRequest(ValueError):
    """Client-side input error; the HTTP layer maps it to a 400."""


@dataclass(frozen=True)
class RunResult:
    rc: int
    text: str


Runner = Callable[[Sequence[str]], RunResult]


def require_task_id(task: object) -> str:
    if not isinstance(task, str) or not TASK_ID_RE.fullmatch(task):
        raise BadRequest(f"invalid task id: {task!r}")
    return task


def questions_dir(work_root: Path) -> Path:
    return work_root / QUESTIONS_DIR_NAME


def list_questions(work_root: Path) -> list[dict[str, str]]:
    """Pending questions as [{"task","id","question"}, ...], oldest-name first.

    ``*.answer.json`` files and unparseable/partial files are skipped (the
    gate writes atomically, but a consumer must still tolerate anything).
    """
    qdir = questions_dir(work_root)
    if not qdir.is_dir():
        return []
    out: list[dict[str, str]] = []
    for path in sorted(qdir.glob("*.json")):
        if path.name.endswith(".answer.json"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        task = payload.get("task")
        qid = payload.get("id")
        question = payload.get("question")
        if isinstance(task, str) and isinstance(qid, str) and isinstance(question, str):
            out.append({"task": task, "id": qid, "question": question})
    return out


def write_answer(work_root: Path, task: object, qid: object, answer: object) -> Path:
    """Atomically write ``<task>.answer.json`` for the polling gate.

    tmp + ``os.replace`` so the gate can never observe a half-written file.
    Returns the answer path (for the HTTP layer's response/logging).
    """
    task_id = require_task_id(task)
    if not isinstance(qid, str) or not qid:
        raise BadRequest("missing question id")
    if not isinstance(answer, str) or not answer.strip():
        raise BadRequest("missing answer text")
    qdir = questions_dir(work_root)
    qdir.mkdir(parents=True, exist_ok=True)
    final = qdir / f"{task_id}.answer.json"
    tmp = qdir / f".{task_id}.answer.tmp"
    tmp.write_text(
        json.dumps({"id": qid, "answer": answer}, ensure_ascii=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(tmp, final)
    return final


def tail_log(log_dir: Path, task: object, lines: int) -> str:
    """Last ``lines`` lines of ``<log_dir>/<task>.log``; missing file -> ""."""
    task_id = require_task_id(task)
    lines = max(1, min(lines, MAX_LOG_LINES))
    path = log_dir / f"{task_id}.log"
    if not path.is_file():
        return ""
    with path.open(encoding="utf-8", errors="replace") as fh:
        tail = deque(fh, maxlen=lines)
    return "".join(tail)


def parse_lines(raw: str | None) -> int:
    if raw is None or raw == "":
        return DEFAULT_LOG_LINES
    try:
        return int(raw)
    except ValueError as exc:
        raise BadRequest(f"invalid lines value: {raw!r}") from exc


def status_argv(python_bin: str) -> list[str]:
    return [python_bin, "-m", "orchestrator.run", "--phase", "status"]


def queue_argv(python_bin: str) -> list[str]:
    return [python_bin, "-m", "orchestrator.run", "--phase", "queue-list"]


def enqueue_argv(python_bin: str, tasks: object, chain: object) -> list[str]:
    if not isinstance(tasks, list) or not tasks:
        raise BadRequest("tasks must be a non-empty list")
    task_ids = [require_task_id(task) for task in tasks]
    argv = [python_bin, "-m", "orchestrator.run", "--phase", "enqueue", *task_ids]
    if chain:
        argv.append("--chain")
    argv.append("--skip-clarify")
    return argv


def resume_argv(python_bin: str, task: object, reason: object) -> list[str]:
    task_id = require_task_id(task)
    if not isinstance(reason, str) or not reason.strip():
        raise BadRequest("resume requires a non-empty reason")
    return [python_bin, "-m", "orchestrator.run", task_id, "--phase", "resume", "--reason", reason]


def make_subprocess_runner(engine_dir: Path) -> Runner:
    """Real runner: orchestrator phases in ``engine_dir``, bounded, no shell."""

    def run(argv: Sequence[str]) -> RunResult:
        env = dict(os.environ)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            f"{engine_dir}{os.pathsep}{existing}" if existing else str(engine_dir)
        )
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, never shell=True
                list(argv),
                cwd=engine_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=RUN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return RunResult(124, f"ERROR: timed out after {RUN_TIMEOUT_SECONDS:.0f}s")
        except OSError as exc:
            return RunResult(127, f"ERROR: cannot run {argv[0]}: {exc}")
        text = proc.stdout
        if proc.returncode != 0 and proc.stderr:
            text = f"{text}{proc.stderr}"
        return RunResult(proc.returncode, text)

    return run
