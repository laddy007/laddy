from __future__ import annotations

import json
from typing import Any

from loop_monitor.docker_api import DockerCollector, _safe_event


class FakeDockerAPI:
    def __init__(self) -> None:
        self.socket_path = None
        self.inspect_calls = 0

    def request_json(self, path: str, timeout: float = 3.0) -> Any:
        if path == "/containers/json?all=1":
            return [{"Id": "a" * 64, "State": "exited"}]
        if path.endswith("/json"):
            self.inspect_calls += 1
            return {
                "Name": "/db",
                "RestartCount": 2,
                "Config": {
                    "Image": "postgres:16",
                    "Env": ["TOKEN=do-not-store"],
                    "Labels": {"secret": "do-not-store"},
                },
                "State": {
                    "Status": "exited",
                    "ExitCode": 137,
                    "OOMKilled": True,
                    "StartedAt": "start",
                    "FinishedAt": "finish",
                },
            }
        raise AssertionError(path)


def test_docker_inspect_is_cached_and_sanitized() -> None:
    api = FakeDockerAPI()
    collector = DockerCollector(api, inspect_refresh_seconds=300)  # pyright: ignore[reportArgumentType]

    first, _ = collector.collect(now=10)
    second, _ = collector.collect(now=20)

    assert api.inspect_calls == 1
    assert first == second
    serialized = json.dumps(first)
    assert "do-not-store" not in serialized
    container = first["containers"][0]
    assert container["oom_killed"] is True
    assert container["exit_code"] == 137
    assert container["restart_count"] == 2


def test_docker_event_keeps_only_allowlisted_attributes() -> None:
    event = _safe_event(
        {
            "Action": "die",
            "id": "abc",
            "Actor": {
                "Attributes": {
                    "name": "test",
                    "image": "image",
                    "exitCode": "2",
                    "token": "secret",
                }
            },
        }
    )

    assert event == {
        "source": "docker",
        "action": "die",
        "container_id": "abc",
        "name": "test",
        "image": "image",
        "exit_code": 2,
    }
