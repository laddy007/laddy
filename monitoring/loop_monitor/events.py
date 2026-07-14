"""Local, secret-minimizing lifecycle events for logical subagents."""

from __future__ import annotations

import json
import os
import queue
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_MAX_HOOK_INPUT = 65_536
_SAFE_TEXT = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-/"
)


def _safe(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    cleaned = "".join(character for character in value if character in _SAFE_TEXT)
    return cleaned[:limit] or None


def sanitize_hook_payload(
    payload: object, vendor: str, now: float | None = None
) -> dict[str, object] | None:
    """Reduce a hook payload to the only fields the monitor is allowed to persist."""

    if vendor not in {"claude", "codex"} or not isinstance(payload, dict):
        return None
    event_name = payload.get("hook_event_name")
    normalized = str(event_name or "").lower()
    if normalized not in {"subagentstart", "subagentstop"}:
        return None
    agent_id = _safe(payload.get("agent_id"), 128)
    session_id = _safe(payload.get("session_id"), 128)
    if agent_id is None or session_id is None:
        return None
    cwd = payload.get("cwd")
    repo = _safe(Path(cwd).name if isinstance(cwd, str) else None, 80)
    result: dict[str, object] = {
        "source": "agent_hook",
        "vendor": vendor,
        "action": "start" if normalized.endswith("start") else "stop",
        "agent_id": agent_id,
        "session_id": session_id,
        "agent_type": _safe(payload.get("agent_type"), 80) or "unknown",
        "observed_at": time.time() if now is None else now,
    }
    if repo is not None:
        result["repo"] = repo
    return result


def emit_hook_event(vendor: str, socket_path: Path, raw: bytes) -> bool:
    if len(raw) > _MAX_HOOK_INPUT:
        return False
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    event = sanitize_hook_payload(payload, vendor)
    if event is None:
        return False
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.settimeout(0.2)
        client.sendto(
            json.dumps(event, separators=(",", ":")).encode(), str(socket_path)
        )
        return True
    except OSError:
        return False
    finally:
        client.close()


class HookServer:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.events: queue.SimpleQueue[dict[str, object]] = queue.SimpleQueue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None
        self.available = False

    def start(self) -> bool:
        server: socket.socket | None = None
        try:
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.socket_path.unlink()
            except FileNotFoundError:
                pass
            server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            server.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o660)
        except OSError:
            if server is not None:
                server.close()
            return False
        assert server is not None
        server.settimeout(1.0)
        self._socket = server
        self.available = True
        self._thread = threading.Thread(
            target=self._run, name="agent-hooks", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self.available = False
        if self._socket is not None:
            self._socket.close()
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    def drain(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                return result

    def _run(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                raw = self._socket.recv(16_384)
                payload = json.loads(raw)
                if isinstance(payload, dict) and payload.get("source") == "agent_hook":
                    self.events.put(payload)
            except socket.timeout:
                continue
            except (OSError, json.JSONDecodeError):
                if not self._stop.is_set():
                    continue


@dataclass
class ActiveSubagents:
    ttl_seconds: float

    def __post_init__(self) -> None:
        self._active: dict[tuple[str, str, str], dict[str, object]] = {}

    def apply(self, event: dict[str, object]) -> None:
        vendor = event.get("vendor")
        session = event.get("session_id")
        agent = event.get("agent_id")
        if not all(isinstance(item, str) for item in (vendor, session, agent)):
            return
        key = str(vendor), str(session), str(agent)
        if event.get("action") == "start":
            self._active[key] = event
        elif event.get("action") == "stop":
            self._active.pop(key, None)

    def reconcile_sessions(self, claude_sessions: int, codex_sessions: int) -> None:
        """Drop orphan hook state after a parent CLI crashed without a stop hook."""

        inactive = {
            vendor
            for vendor, count in (
                ("claude", claude_sessions),
                ("codex", codex_sessions),
            )
            if count <= 0
        }
        for key in [key for key in self._active if key[0] in inactive]:
            del self._active[key]

    def counts(self, now: float | None = None) -> dict[str, int]:
        timestamp = time.time() if now is None else now

        def observed_at(event: dict[str, object]) -> float:
            value = event.get("observed_at")
            return float(value) if isinstance(value, (int, float)) else 0.0

        expired = [
            key
            for key, event in self._active.items()
            if timestamp - observed_at(event) > self.ttl_seconds
        ]
        for key in expired:
            del self._active[key]
        return {
            "claude_subagents": sum(key[0] == "claude" for key in self._active),
            "codex_subagents": sum(key[0] == "codex" for key in self._active),
            "subagents_total": len(self._active),
        }
