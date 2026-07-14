"""Minimal Docker Engine client over its local Unix socket.

The collector polls the cheap container list and per-running-container stats.
Full inspect is cached and refreshed only on lifecycle events, state changes or
the configured slow refresh interval.
"""

from __future__ import annotations

import http.client
import json
import queue
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class DockerUnavailable(RuntimeError):
    pass


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: float = 3.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(str(self.socket_path))
        self.sock = connection


class DockerAPI:
    def __init__(self, socket_path: Path = Path("/var/run/docker.sock")) -> None:
        self.socket_path = socket_path

    def request_json(self, path: str, timeout: float = 3.0) -> Any:
        connection = _UnixHTTPConnection(self.socket_path, timeout=timeout)
        try:
            connection.request("GET", path, headers={"Host": "localhost"})
            response = connection.getresponse()
            body = response.read()
            if response.status >= 400:
                raise DockerUnavailable(
                    f"Docker API {path} returned HTTP {response.status}"
                )
            return json.loads(body) if body else None
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            raise DockerUnavailable(str(exc)) from exc
        finally:
            connection.close()

    def open_events(
        self, since: int
    ) -> tuple[_UnixHTTPConnection, http.client.HTTPResponse]:
        filters = urllib.parse.quote(
            json.dumps({"type": ["container"]}, separators=(",", ":"))
        )
        path = f"/events?since={since}&filters={filters}"
        connection = _UnixHTTPConnection(self.socket_path, timeout=65.0)
        try:
            connection.request("GET", path, headers={"Host": "localhost"})
            response = connection.getresponse()
            if response.status >= 400:
                connection.close()
                raise DockerUnavailable(
                    f"Docker events returned HTTP {response.status}"
                )
            return connection, response
        except (OSError, http.client.HTTPException) as exc:
            connection.close()
            raise DockerUnavailable(str(exc)) from exc


_LIFECYCLE_ACTIONS = {
    "create",
    "start",
    "stop",
    "die",
    "kill",
    "oom",
    "restart",
    "destroy",
    "pause",
    "unpause",
    "health_status: healthy",
    "health_status: unhealthy",
}


def _safe_event(payload: dict[str, Any]) -> dict[str, object] | None:
    action = str(payload.get("Action") or payload.get("status") or "")
    if action not in _LIFECYCLE_ACTIONS:
        return None
    actor = _mapping(payload.get("Actor"))
    attributes = _mapping(actor.get("Attributes"))
    event: dict[str, object] = {
        "source": "docker",
        "action": action,
        "container_id": str(payload.get("id") or actor.get("ID") or "")[:64],
    }
    name = attributes.get("name")
    image = attributes.get("image")
    if isinstance(name, str):
        event["name"] = name[:128]
    if isinstance(image, str):
        event["image"] = image[:256]
    exit_code = attributes.get("exitCode")
    if isinstance(exit_code, str) and exit_code.isdigit():
        event["exit_code"] = int(exit_code)
    return event


class DockerEventReader:
    def __init__(self, api: DockerAPI) -> None:
        self.api = api
        self.events: queue.SimpleQueue[dict[str, object]] = queue.SimpleQueue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="docker-events", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def drain(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                return result

    def _run(self) -> None:
        since = int(time.time())
        while not self._stop.is_set():
            connection: _UnixHTTPConnection | None = None
            try:
                connection, response = self.api.open_events(since)
                while not self._stop.is_set():
                    line = response.readline()
                    if not line:
                        break
                    payload = json.loads(line)
                    if not isinstance(payload, dict):
                        continue
                    event = _safe_event(payload)
                    if event is not None:
                        self.events.put(event)
                    timestamp = payload.get("time")
                    if isinstance(timestamp, int):
                        since = max(since, timestamp)
            except (
                DockerUnavailable,
                OSError,
                http.client.HTTPException,
                json.JSONDecodeError,
            ):
                self._stop.wait(5.0)
            finally:
                if connection is not None:
                    connection.close()


@dataclass
class _InspectCache:
    checked_at: float
    state: str
    value: dict[str, object]


class DockerCollector:
    def __init__(self, api: DockerAPI, inspect_refresh_seconds: float = 300.0) -> None:
        self.api = api
        self.inspect_refresh_seconds = inspect_refresh_seconds
        self.events = DockerEventReader(api)
        self._inspect: dict[str, _InspectCache] = {}
        self._dirty: set[str] = set()

    def start(self) -> None:
        if self.api.socket_path.exists():
            self.events.start()

    def stop(self) -> None:
        self.events.stop()

    def collect(
        self, now: float | None = None
    ) -> tuple[dict[str, Any], list[dict[str, object]]]:
        measured_at = time.monotonic() if now is None else now
        lifecycle = self.events.drain()
        for event in lifecycle:
            container_id = event.get("container_id")
            if isinstance(container_id, str) and container_id:
                self._dirty.add(container_id)
        try:
            containers = self.api.request_json("/containers/json?all=1")
        except DockerUnavailable as exc:
            return {
                "available": False,
                "error": type(exc).__name__,
                "containers": [],
            }, lifecycle
        if not isinstance(containers, list):
            containers = []
        values: list[dict[str, object]] = []
        present: set[str] = set()
        for item in containers:
            if not isinstance(item, dict):
                continue
            container_id = str(item.get("Id", ""))
            if not container_id:
                continue
            present.add(container_id)
            state = str(item.get("State", "unknown"))
            cache = self._inspect.get(container_id)
            must_inspect = (
                cache is None
                or cache.state != state
                or container_id in self._dirty
                or measured_at - cache.checked_at >= self.inspect_refresh_seconds
            )
            if must_inspect:
                inspected = self._inspect_container(container_id, measured_at, state)
            elif cache is not None:
                inspected = cache.value
            else:  # defensive; must_inspect is true whenever cache is None
                inspected = {}
            value = dict(inspected)
            if state == "running":
                stats = self._stats(container_id)
                if stats is not None:
                    value["stats"] = stats
            values.append(value)
        self._dirty.clear()
        for stale in set(self._inspect) - present:
            del self._inspect[stale]
        return {"available": True, "containers": values}, lifecycle

    def _inspect_container(
        self, container_id: str, now: float, state: str
    ) -> dict[str, object]:
        try:
            payload = self.api.request_json(f"/containers/{container_id}/json")
        except DockerUnavailable:
            payload = {}
        payload = _mapping(payload)
        state_value = _mapping(payload.get("State"))
        config = _mapping(payload.get("Config"))
        health = _mapping(state_value.get("Health"))
        name = str(payload.get("Name") or "").lstrip("/")[:128]
        value: dict[str, object] = {
            "id": container_id[:12],
            "name": name,
            "image": str(config.get("Image") or "")[:256],
            "state": str(state_value.get("Status") or state),
            "health": str(health.get("Status") or "none"),
            "exit_code": int(state_value.get("ExitCode") or 0),
            "oom_killed": bool(state_value.get("OOMKilled")),
            "restart_count": int(payload.get("RestartCount") or 0),
            "started_at": str(state_value.get("StartedAt") or ""),
            "finished_at": str(state_value.get("FinishedAt") or ""),
        }
        self._inspect[container_id] = _InspectCache(
            checked_at=now, state=state, value=value
        )
        return value

    def _stats(self, container_id: str) -> dict[str, int | float] | None:
        try:
            payload = self.api.request_json(
                f"/containers/{container_id}/stats?stream=false&one-shot=true",
                timeout=5.0,
            )
        except DockerUnavailable:
            return None
        if not isinstance(payload, dict):
            return None
        payload = _mapping(payload)
        cpu = _mapping(payload.get("cpu_stats"))
        precpu = _mapping(payload.get("precpu_stats"))
        cpu_usage = _mapping(cpu.get("cpu_usage"))
        pre_usage = _mapping(precpu.get("cpu_usage"))
        cpu_delta = int(cpu_usage.get("total_usage") or 0) - int(
            pre_usage.get("total_usage") or 0
        )
        system_delta = int(cpu.get("system_cpu_usage") or 0) - int(
            precpu.get("system_cpu_usage") or 0
        )
        online = int(
            cpu.get("online_cpus") or len(cpu_usage.get("percpu_usage") or []) or 1
        )
        cpu_pct = 100.0 * cpu_delta / system_delta * online if system_delta > 0 else 0.0
        memory = _mapping(payload.get("memory_stats"))
        memory_detail = _mapping(memory.get("stats"))
        memory_usage = max(
            0,
            int(memory.get("usage") or 0)
            - int(memory_detail.get("inactive_file") or 0),
        )
        blkio = _mapping(payload.get("blkio_stats"))
        io_values = blkio.get("io_service_bytes_recursive") or []
        read_bytes = write_bytes = 0
        if isinstance(io_values, list):
            for entry in io_values:
                if not isinstance(entry, dict):
                    continue
                operation = str(entry.get("op") or "").lower()
                value = int(entry.get("value") or 0)
                if operation == "read":
                    read_bytes += value
                elif operation == "write":
                    write_bytes += value
        pids = _mapping(payload.get("pids_stats"))
        return {
            "cpu_pct": round(max(0.0, cpu_pct), 2),
            "memory_bytes": memory_usage,
            "memory_limit_bytes": int(memory.get("limit") or 0),
            "pids": int(pids.get("current") or 0),
            "block_read_bytes": read_bytes,
            "block_write_bytes": write_bytes,
        }
