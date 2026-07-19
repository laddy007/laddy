"""laddy-phone HTTP server: stdlib-only app behind the tailnet.

``python -m phone.server`` starts a ``ThreadingHTTPServer`` that serves the
static PWA (no token) and a token-gated ``/api/*`` surface (questions,
answer, status, queue, enqueue, resume, log tail). No third-party deps by
engine rule; no shell=True anywhere; the subprocess runner is an injected
callable on :class:`PhoneApp` so tests never spawn a real orchestrator.

Config comes from the environment (see :func:`config_from_env`):
  LADDY_PHONE_TOKEN  required - the server refuses to start without it
  LADDY_PHONE_BIND   default 127.0.0.1:8787 (bind a tailnet IP to expose)
  AGENT_WORK_ROOT    default ~/agent-work   (questions/ lives under it)
  AGENT_LOG_DIR      default ~/agent-logs   (<task>.log tails)
  PYTHON_BIN         default python3        (runs orchestrator phases)
The engine dir is derived from this package's own location.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from phone.handlers import (
    BadRequest,
    Runner,
    enqueue_argv,
    list_questions,
    make_subprocess_runner,
    parse_lines,
    queue_argv,
    resume_argv,
    status_argv,
    tail_log,
    write_answer,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
ENGINE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_BIND = "127.0.0.1:8787"
MAX_BODY_BYTES = 64 * 1024

# Closed allowlist, not a file server: nothing outside these five names is
# reachable, so the static side cannot leak anything sensitive.
STATIC_ROUTES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/manifest.json": ("manifest.json", "application/manifest+json"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}


class ConfigError(RuntimeError):
    """Bad or missing server configuration; main() exits 2 on it."""


@dataclass(frozen=True)
class PhoneConfig:
    token: str
    host: str
    port: int
    work_root: Path
    log_dir: Path
    engine_dir: Path
    python_bin: str


def config_from_env(environ: Mapping[str, str]) -> PhoneConfig:
    token = environ.get("LADDY_PHONE_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "LADDY_PHONE_TOKEN is not set - refusing to start an unauthenticated "
            "control server. Generate one (e.g. `openssl rand -hex 24`), put it "
            "in env.vps, and restart."
        )
    bind = environ.get("LADDY_PHONE_BIND", "").strip() or DEFAULT_BIND
    host, sep, port_text = bind.rpartition(":")
    if not sep or not host or not port_text.isdigit():
        raise ConfigError(f"LADDY_PHONE_BIND must be host:port, got {bind!r}")
    home = Path.home()
    return PhoneConfig(
        token=token,
        host=host,
        port=int(port_text),
        work_root=Path(environ.get("AGENT_WORK_ROOT", "") or home / "agent-work"),
        log_dir=Path(environ.get("AGENT_LOG_DIR", "") or home / "agent-logs"),
        engine_dir=ENGINE_DIR,
        python_bin=environ.get("PYTHON_BIN", "").strip() or "python3",
    )


@dataclass(frozen=True)
class PhoneApp:
    """Everything a request needs; ``runner`` is the injected subprocess seam."""

    token: str
    work_root: Path
    log_dir: Path
    python_bin: str
    runner: Runner


class PhoneServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: PhoneApp) -> None:
        super().__init__(address, PhoneHandler)
        self.app: PhoneApp = app


class PhoneHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> PhoneApp:
        server = self.server
        assert isinstance(server, PhoneServer)
        return server.app

    # -- plumbing ------------------------------------------------------------

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Redact query strings: ?token=... must never reach a log/journal.
        message = re.sub(r"\?\S*", "", format % args)
        sys.stderr.write(f"[phone] {self.address_string()} {message}\n")

    def _send_json(self, code: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("ascii")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, path: str) -> bool:
        route = STATIC_ROUTES.get(path)
        if route is None:
            return False
        name, content_type = route
        data = (STATIC_DIR / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        # The service worker owns caching; keep the HTTP layer honest.
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)
        return True

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            candidate = header[len("Bearer ") :]
        else:
            # ?token= exists for ntfy action URLs, which cannot set headers.
            candidate = (query.get("token") or [""])[0]
        return hmac.compare_digest(
            candidate.encode("utf-8"), self.app.token.encode("utf-8")
        )

    def _read_json_body(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise BadRequest("bad Content-Length") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise BadRequest(f"body must be 1..{MAX_BODY_BYTES} bytes")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError as exc:
            raise BadRequest("body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise BadRequest("body must be a JSON object")
        return payload

    # -- routing -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        url = urlsplit(self.path)
        if self._send_static(url.path):
            return
        if not url.path.startswith("/api/"):
            self._send_json(404, {"error": "not found"})
            return
        query = parse_qs(url.query)
        if not self._authorized(query):
            self._send_json(401, {"error": "unauthorized"})
            return
        try:
            self._api_get(url.path, query)
        except BadRequest as exc:
            self._send_json(400, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if not url.path.startswith("/api/"):
            self._send_json(404, {"error": "not found"})
            return
        if not self._authorized(query):
            self._send_json(401, {"error": "unauthorized"})
            return
        try:
            self._api_post(url.path)
        except BadRequest as exc:
            self._send_json(400, {"error": str(exc)})

    def _api_get(self, path: str, query: dict[str, list[str]]) -> None:
        app = self.app
        if path == "/api/questions":
            self._send_json(200, {"questions": list_questions(app.work_root)})
        elif path == "/api/status":
            result = app.runner(status_argv(app.python_bin))
            self._send_json(200, {"text": result.text})
        elif path == "/api/queue":
            result = app.runner(queue_argv(app.python_bin))
            self._send_json(200, {"text": result.text})
        elif path == "/api/log":
            task = (query.get("task") or [""])[0]
            lines = parse_lines((query.get("lines") or [None])[0])
            self._send_json(200, {"text": tail_log(app.log_dir, task, lines)})
        else:
            self._send_json(404, {"error": "not found"})

    def _api_post(self, path: str) -> None:
        app = self.app
        if path == "/api/answer":
            body = self._read_json_body()
            write_answer(app.work_root, body.get("task"), body.get("id"), body.get("answer"))
            self._send_json(200, {"ok": True})
        elif path == "/api/enqueue":
            body = self._read_json_body()
            argv = enqueue_argv(app.python_bin, body.get("tasks"), body.get("chain"))
            result = app.runner(argv)
            self._send_json(200, {"rc": result.rc, "text": result.text})
        elif path == "/api/resume":
            body = self._read_json_body()
            argv = resume_argv(app.python_bin, body.get("task"), body.get("reason"))
            result = app.runner(argv)
            self._send_json(200, {"rc": result.rc, "text": result.text})
        else:
            self._send_json(404, {"error": "not found"})


def make_server(config: PhoneConfig, runner: Runner | None = None) -> PhoneServer:
    app = PhoneApp(
        token=config.token,
        work_root=config.work_root,
        log_dir=config.log_dir,
        python_bin=config.python_bin,
        runner=runner if runner is not None else make_subprocess_runner(config.engine_dir),
    )
    return PhoneServer((config.host, config.port), app)


def main() -> int:
    try:
        config = config_from_env(os.environ)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    server = make_server(config)
    host, port = server.server_address[0], server.server_address[1]
    print(
        f"[phone] listening on {host}:{port} "
        f"(work_root={config.work_root}, log_dir={config.log_dir})",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
