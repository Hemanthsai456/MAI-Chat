"""HTTP server and REST API for MAI-Chat.

Endpoints:
    GET  /                              Chat web application
    GET  /api/health                    Health check and engine status
    GET  /api/tools                     List of approved CLI tools and specifications
    GET  /api/tasks                     List persistent task history
    POST /api/tasks                     Submit a new natural language task
    GET  /api/tasks/<id>                Task status, progress, 10-stage events, and result
    GET  /api/tasks/<id>/details        In-depth execution metadata, stderr tail, and plan
    POST /api/tasks/<id>/cancel         Cancel an active task
    GET  /api/tasks/<id>/export         Export result as JSON, CSV, or Markdown
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .database import Database
from .intent import IntentError, parse_intent
from .registry import list_tools
from .tasks import QueueFullError, TaskManager
from .validation import enforce_startup_safety

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
TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'"
    ),
}

# Simple sliding window rate limiter per client IP
_RATE_LIMITS: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_WINDOW_S = 60.0
RATE_LIMIT_MAX_REQUESTS = 60


def _check_rate_limit(client_ip: str) -> bool:
    now = time.time()
    history = _RATE_LIMITS[client_ip]
    # Prune old timestamps
    _RATE_LIMITS[client_ip] = [t for t in history if now - t < RATE_LIMIT_WINDOW_S]
    if len(_RATE_LIMITS[client_ip]) >= RATE_LIMIT_MAX_REQUESTS:
        return False
    _RATE_LIMITS[client_ip].append(now)
    return True


class ChatHandler(BaseHTTPRequestHandler):
    server_version = "MAIChatServer/2.0"
    sys_version = ""

    @property
    def manager(self) -> TaskManager:
        return self.server.manager  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        client_ip = self.client_address[0]
        if not _check_rate_limit(client_ip):
            self._send_json(429, {"error": "Rate limit exceeded. Please wait a moment."})
            return

        split = urlsplit(self.path)
        path = split.path
        query = parse_qs(split.query)

        # Static assets
        if path in STATIC_ROUTES:
            filename, content_type = STATIC_ROUTES[path]
            file_path = STATIC_DIR / filename
            if file_path.exists():
                self._send_bytes(200, file_path.read_bytes(), content_type)
            else:
                self._send_json(404, {"error": "static file not found"})
            return

        # Health endpoint
        if path == "/api/health":
            tools = list_tools()
            self._send_json(200, {
                "ok": True,
                "engine": "MAI-Chat CLI Execution Engine",
                "version": "2.0.0",
                "tools": tools,
            })
            return

        # Approved tools listing
        if path == "/api/tools":
            self._send_json(200, {"tools": list_tools()})
            return

        # Task history listing
        if path == "/api/tasks":
            try:
                limit = int(query.get("limit", ["50"])[0])
                offset = int(query.get("offset", ["0"])[0])
            except ValueError:
                limit, offset = 50, 0
            tasks = self.manager.list_history(limit, offset)
            self._send_json(200, {"tasks": tasks, "count": len(tasks)})
            return

        # Individual task endpoints: /api/tasks/<id>
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "tasks":
            task_id = parts[2]
            if not TASK_ID_RE.match(task_id):
                self._send_json(400, {"error": "invalid task id format"})
                return

            task = self.manager.get(task_id)
            if task is None:
                self._send_json(404, {"error": "task not found"})
                return

            # GET /api/tasks/<id>
            if len(parts) == 3:
                self._send_json(200, self.manager.view(task))
                return

            # GET /api/tasks/<id>/details
            if len(parts) == 4 and parts[3] == "details":
                view_data = self.manager.view(task)
                # Include full execution data if available
                exec_details = {}
                if task.result and "execution" in task.result:
                    exec_details = task.result["execution"]
                view_data["execution_details"] = exec_details
                self._send_json(200, view_data)
                return

            # GET /api/tasks/<id>/export?format=json|csv|md
            if len(parts) == 4 and parts[3] == "export":
                fmt = query.get("format", ["json"])[0]
                content, c_type, filename = self.manager.export(task, fmt)
                self.send_response(200)
                self.send_header("Content-Type", c_type)
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                body_bytes = content.encode("utf-8")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.send_header("Cache-Control", "no-store")
                for name, value in SECURITY_HEADERS.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body_bytes)
                return

        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        client_ip = self.client_address[0]
        if not _check_rate_limit(client_ip):
            self._send_json(429, {"error": "Rate limit exceeded. Please wait a moment."})
            return

        path = urlsplit(self.path).path

        # POST /api/tasks
        if path == "/api/tasks":
            body = self._read_json()
            if body is None:
                return
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
            except Exception as exc:
                log.exception("Unexpected error parsing intent")
                self._send_json(500, {"error": "An internal error occurred while parsing request."})
                return

            try:
                task = self.manager.submit(prompt.strip(), intent)
            except QueueFullError as exc:
                self._send_json(429, {"error": str(exc)})
                return

            plan_dict = intent.plan.to_dict() if intent.plan else None
            self._send_json(202, {
                "id": task.id,
                "status": task.status,
                "interpretation": intent.describe(),
                "plan": plan_dict,
                "notes": list(intent.notes),
            })
            return

        # POST /api/tasks/<id>/cancel
        parts = [p for p in path.split("/") if p]
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "tasks" and parts[3] == "cancel":
            task_id = parts[2]
            if not TASK_ID_RE.match(task_id):
                self._send_json(400, {"error": "invalid task id format"})
                return
            task = self.manager.cancel(task_id)
            if task is None:
                self._send_json(404, {"error": "task not found"})
            else:
                self._send_json(200, self.manager.view(task))
            return

        self._send_json(404, {"error": "not found"})

    # ----- Helpers ---------------------------------------------------------

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
            raw_body = self.rfile.read(length)
            body = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "the request body is not valid JSON"})
            return None
        if not isinstance(body, dict):
            self._send_json(400, {"error": "the request body must be a JSON object"})
            return None
        return body

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

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


class MAIChatServer(ThreadingHTTPServer):
    """Threading HTTP server with coordinated TaskManager lifecycle management."""

    manager: TaskManager

    def server_close(self) -> None:
        if hasattr(self, "manager") and self.manager:
            try:
                self.manager.shutdown()
            except Exception:
                log.exception("Error shutting down TaskManager during server_close")
        super().server_close()


def create_server(host: str = "127.0.0.1", port: int = 8000, db_path: str | None = None) -> MAIChatServer:
    enforce_startup_safety(host)
    httpd = MAIChatServer((host, port), ChatHandler)
    db = Database(db_path) if db_path else None
    httpd.manager = TaskManager(db=db)
    return httpd


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    httpd = create_server(host, port)
    log.info("MAI-Chat CLI Execution Engine running at http://%s:%s", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Server shutting down.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
