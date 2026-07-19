"""laddy-phone server: auth, question files, log tail, injected runner."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from phone.handlers import RunResult
from phone.server import (
    ConfigError,
    PhoneApp,
    PhoneServer,
    config_from_env,
)

TOKEN = "test-token-123"


@dataclass
class FakeRunner:
    """Records every argv; returns a canned result. No real subprocess."""

    result: RunResult = field(default_factory=lambda: RunResult(0, "fake output\n"))
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str]) -> RunResult:
        self.calls.append(list(argv))
        return self.result


@dataclass
class Client:
    base: str
    runner: FakeRunner
    work_root: Path
    log_dir: Path

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, object] | None = None,
        token: str | None = TOKEN,
    ) -> tuple[int, dict[str, object]]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if token is not None:
            req.add_header("Authorization", f"Bearer {token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())


@pytest.fixture
def client(tmp_path: Path) -> Iterator[Client]:
    work_root = tmp_path / "work"
    log_dir = tmp_path / "logs"
    runner = FakeRunner()
    app = PhoneApp(
        token=TOKEN,
        work_root=work_root,
        log_dir=log_dir,
        python_bin="fakepy",
        runner=runner,
    )
    server = PhoneServer(("127.0.0.1", 0), app)  # port 0: safe under -n auto
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.02), daemon=True
    )
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield Client(f"http://{host}:{port}", runner, work_root, log_dir)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _seed_question(work_root: Path, task: str = "t1", qid: str = "q-1") -> None:
    qdir = work_root / "questions"
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / f"{task}.json").write_text(
        json.dumps({"task": task, "id": qid, "question": "Proceed?"}),
        encoding="utf-8",
    )


# -- auth --------------------------------------------------------------------


def test_api_without_token_is_401(client: Client) -> None:
    status, body = client.request("/api/questions", token=None)
    assert status == 401
    assert body == {"error": "unauthorized"}


def test_api_with_wrong_token_is_401(client: Client) -> None:
    status, _ = client.request("/api/questions", token="wrong-token")
    assert status == 401


def test_token_accepted_via_query_param(client: Client) -> None:
    # ntfy action URLs cannot set headers; ?token= must work too.
    status, body = client.request(f"/api/questions?token={TOKEN}", token=None)
    assert status == 200
    assert body == {"questions": []}


def test_static_index_needs_no_token(client: Client) -> None:
    with urllib.request.urlopen(client.base + "/") as res:
        assert res.status == 200
        assert b"laddy" in res.read()


# -- questions / answer ------------------------------------------------------


def test_questions_lists_pending_and_skips_answer_files(client: Client) -> None:
    _seed_question(client.work_root, "t1", "q-1")
    (client.work_root / "questions" / "t2.answer.json").write_text(
        json.dumps({"id": "old", "answer": "stale"}), encoding="utf-8"
    )
    status, body = client.request("/api/questions")
    assert status == 200
    assert body == {"questions": [{"task": "t1", "id": "q-1", "question": "Proceed?"}]}


def test_answer_writes_the_gate_file(client: Client) -> None:
    _seed_question(client.work_root, "t1", "q-1")
    status, body = client.request(
        "/api/answer",
        method="POST",
        body={"task": "t1", "id": "q-1", "answer": "yes, ship it"},
    )
    assert status == 200
    assert body == {"ok": True}
    answer_path = client.work_root / "questions" / "t1.answer.json"
    payload = json.loads(answer_path.read_text(encoding="utf-8"))
    assert payload == {"id": "q-1", "answer": "yes, ship it"}
    # atomic write leaves no tmp file behind
    leftovers = [
        p.name
        for p in (client.work_root / "questions").iterdir()
        if p.name.endswith(".tmp")
    ]
    assert leftovers == []


def test_answer_rejects_invalid_task_id(client: Client) -> None:
    status, body = client.request(
        "/api/answer",
        method="POST",
        body={"task": "../../etc/cron.d/evil", "id": "q-1", "answer": "x"},
    )
    assert status == 400
    assert "invalid task id" in str(body["error"])
    assert not (client.work_root / "questions").exists()  # nothing written


def test_answer_rejects_empty_answer(client: Client) -> None:
    status, _ = client.request(
        "/api/answer", method="POST", body={"task": "t1", "id": "q-1", "answer": "  "}
    )
    assert status == 400


# -- log tail ----------------------------------------------------------------


def test_log_rejects_traversal_task_id(client: Client) -> None:
    status, body = client.request("/api/log?task=..%2F..%2Fetc%2Fpasswd")
    assert status == 400
    assert "invalid task id" in str(body["error"])


def test_log_tails_last_n_lines(client: Client) -> None:
    client.log_dir.mkdir(parents=True, exist_ok=True)
    (client.log_dir / "t1.log").write_text(
        "".join(f"line {i}\n" for i in range(10)), encoding="utf-8"
    )
    status, body = client.request("/api/log?task=t1&lines=3")
    assert status == 200
    assert body == {"text": "line 7\nline 8\nline 9\n"}


def test_log_missing_file_is_empty_text(client: Client) -> None:
    status, body = client.request("/api/log?task=nope")
    assert status == 200
    assert body == {"text": ""}


def test_log_lines_capped_at_max(client: Client) -> None:
    client.log_dir.mkdir(parents=True, exist_ok=True)
    (client.log_dir / "t1.log").write_text("only line\n", encoding="utf-8")
    status, body = client.request("/api/log?task=t1&lines=999999")
    assert status == 200
    assert body == {"text": "only line\n"}


# -- injected runner: status / queue / enqueue / resume ----------------------


def test_status_uses_injected_runner(client: Client) -> None:
    status, body = client.request("/api/status")
    assert status == 200
    assert body == {"text": "fake output\n"}
    assert client.runner.calls == [
        ["fakepy", "-m", "orchestrator.run", "--phase", "status"]
    ]


def test_queue_uses_injected_runner(client: Client) -> None:
    status, body = client.request("/api/queue")
    assert status == 200
    assert body == {"text": "fake output\n"}
    assert client.runner.calls == [
        ["fakepy", "-m", "orchestrator.run", "--phase", "queue-list"]
    ]


def test_enqueue_passes_chain_and_skip_clarify(client: Client) -> None:
    client.runner.result = RunResult(0, "queued\n")
    status, body = client.request(
        "/api/enqueue", method="POST", body={"tasks": ["a1", "b2"], "chain": True}
    )
    assert status == 200
    assert body == {"rc": 0, "text": "queued\n"}
    assert client.runner.calls == [
        [
            "fakepy", "-m", "orchestrator.run", "--phase", "enqueue",
            "a1", "b2", "--chain", "--skip-clarify",
        ]
    ]


def test_enqueue_without_chain_omits_flag(client: Client) -> None:
    status, _ = client.request(
        "/api/enqueue", method="POST", body={"tasks": ["a1"], "chain": False}
    )
    assert status == 200
    assert client.runner.calls == [
        ["fakepy", "-m", "orchestrator.run", "--phase", "enqueue", "a1", "--skip-clarify"]
    ]


def test_enqueue_rejects_bad_task_id(client: Client) -> None:
    status, _ = client.request(
        "/api/enqueue", method="POST", body={"tasks": ["ok", "bad/../id"], "chain": False}
    )
    assert status == 400
    assert client.runner.calls == []  # nothing ran


def test_enqueue_rejects_empty_tasks(client: Client) -> None:
    status, _ = client.request(
        "/api/enqueue", method="POST", body={"tasks": [], "chain": False}
    )
    assert status == 400


def test_resume_runs_with_reason(client: Client) -> None:
    client.runner.result = RunResult(0, "resumed\n")
    status, body = client.request(
        "/api/resume", method="POST", body={"task": "t1", "reason": "hub updated"}
    )
    assert status == 200
    assert body == {"rc": 0, "text": "resumed\n"}
    assert client.runner.calls == [
        ["fakepy", "-m", "orchestrator.run", "t1", "--phase", "resume",
         "--reason", "hub updated"]
    ]


def test_resume_requires_reason(client: Client) -> None:
    status, _ = client.request(
        "/api/resume", method="POST", body={"task": "t1", "reason": ""}
    )
    assert status == 400
    assert client.runner.calls == []


# -- config ------------------------------------------------------------------


def test_config_requires_token() -> None:
    with pytest.raises(ConfigError, match="LADDY_PHONE_TOKEN"):
        config_from_env({})


def test_config_defaults() -> None:
    config = config_from_env({"LADDY_PHONE_TOKEN": "tok"})
    assert (config.host, config.port) == ("127.0.0.1", 8787)
    assert config.python_bin == "python3"
    assert config.work_root.name == "agent-work"
    assert config.log_dir.name == "agent-logs"


def test_config_rejects_bad_bind() -> None:
    with pytest.raises(ConfigError, match="LADDY_PHONE_BIND"):
        config_from_env({"LADDY_PHONE_TOKEN": "tok", "LADDY_PHONE_BIND": "no-port"})
