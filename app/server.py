"""HTTP layer: serves the chat page and a small JSON API.

    GET  /                         chat page
    GET  /api/health               approved tools
    POST /api/tasks                {"prompt": "..."} -> 202 {id, interpretation, notes}
    GET  /api/tasks/<id>           status, progress log, final result when done
    POST /api/tasks/<id>/cancel    stop a running task
"""
from __future__ import annotations

import json
import logging
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .intent import IntentError, parse_intent
from .runner import APPROVED_TOOLS
from .tasks import TaskManager

log = logging.getLogger("buildsprint.server")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}
MAX_BODY_BYTES = 8192
MAX_PROMPT_CHARS = 1000
TASK_PATH = re.compile(r"^/api/tasks/([0-9a-f]{32})(/cancel)?$")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'"
    ),
}


class ChatHandler(BaseHTTPRequestHandler):
    server_version = "BuildSprintChat/1.0"
    sys_version = ""

    @property
    def manager(self) -> TaskManager:
        return self.server.manager  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in STATIC_ROUTES:
            filename, content_type = STATIC_ROUTES[path]
            self._send_bytes(200, (STATIC_DIR / filename).read_bytes(), content_type)
            return
        if path == "/api/health":
            self._send_json(200, {
                "ok": True,
                "tools": [{"name": t.name, "description": t.description} for t in APPROVED_TOOLS.values()],
            })
            return
        match = TASK_PATH.match(path)
        if match and not match.group(2):
            task = self.manager.get(match.group(1))
            if task is None:
                self._send_json(404, {"error": "task not found"})
            else:
                self._send_json(200, self.manager.view(task))
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/tasks":
            body = self._read_json()
            if body is None:
                return  # error response already sent
            prompt = body.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                self._send_json(400, {"error": "a non-empty 'prompt' is required"})
                return
            if len(prompt) > MAX_PROMPT_CHARS:
                self._send_json(400, {"error": f"prompt must be {MAX_PROMPT_CHARS} characters or fewer"})
                return
            try:
                intent = parse_intent(prompt)
            except IntentError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            task = self.manager.submit(prompt.strip(), intent)
            self._send_json(202, {
                "id": task.id,
                "status": task.status,
                "interpretation": intent.describe(),
                "notes": list(intent.notes),
            })
            return
        match = TASK_PATH.match(path)
        if match and match.group(2):
            task = self.manager.cancel(match.group(1))
            if task is None:
                self._send_json(404, {"error": "task not found"})
            else:
                self._send_json(200, self.manager.view(task))
            return
        self._send_json(404, {"error": "not found"})

    # ----- helpers ---------------------------------------------------------

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": f"request body must be at most {MAX_BODY_BYTES} bytes"})
            return None
        if length <= 0:
            self._send_json(400, {"error": "a JSON request body is required"})
            return None
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "the request body is not valid JSON"})
            return None
        if not isinstance(body, dict):
            self._send_json(400, {"error": "the request body must be a JSON object"})
            return None
        return body

    def _send_json(self, status: int, payload: dict) -> None:
        self._send_bytes(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s %s", self.address_string(), fmt % args)


def create_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), ChatHandler)
    httpd.manager = TaskManager()  # type: ignore[attr-defined]
    return httpd


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    httpd = create_server(host, port)
    log.info("Site crawler chat running at http://%s:%s", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
