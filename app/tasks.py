"""Task lifecycle: queue, run in a worker thread, monitor, cancel, and keep results."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .intent import Intent
from .report import CANCELLED, FAILED, build_message, build_result
from .runner import run_approved_tool
from .validation import ValidationError, validate_public_url

log = logging.getLogger("buildsprint.tasks")

CRAWL_TIMEOUT_S = 90
MAX_TASKS_KEPT = 200
TERMINAL_STATES = frozenset({"succeeded", "partial", "failed", "timed_out", "cancelled"})


@dataclass
class Task:
    id: str
    prompt: str
    intent: Intent
    status: str = "queued"
    progress: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event)


def crawler_args(intent: Intent) -> list[str]:
    # Every value here comes from validated intent fields, and is passed as one argv item.
    return [
        f"--url={intent.url}",
        f"--max-pages={intent.max_pages}",
        "--max-depth=2",
        "--timeout=10",
        "--delay=0.3",
    ]


class TaskManager:
    def __init__(self) -> None:
        self._tasks: OrderedDict[str, Task] = OrderedDict()
        self._lock = threading.Lock()

    def submit(self, prompt: str, intent: Intent) -> Task:
        task = Task(id=uuid.uuid4().hex, prompt=prompt, intent=intent)
        with self._lock:
            self._tasks[task.id] = task
            self._evict_locked()
        threading.Thread(target=self._execute, args=(task,), daemon=True, name=f"task-{task.id[:8]}").start()
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def cancel(self, task_id: str) -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None
        with self._lock:
            if task.status not in TERMINAL_STATES:
                task.cancel_event.set()
                task.progress.append("Cancellation requested")
        return task

    def view(self, task: Task) -> dict[str, Any]:
        with self._lock:
            return {
                "id": task.id,
                "status": task.status,
                "interpretation": task.intent.describe(),
                "progress": list(task.progress),
                "result": task.result,
            }

    # ----- internals -------------------------------------------------------

    def _update(self, task: Task, *, status: str | None = None, note: str | None = None) -> None:
        with self._lock:
            if status:
                task.status = status
            if note:
                task.progress.append(note)

    def _finish(self, task: Task, result: dict[str, Any]) -> None:
        with self._lock:
            task.result = result
            task.status = result["task_status"]
            task.progress.append(f"Finished: {result['banner']['text']}")

    def _execute(self, task: Task) -> None:
        try:
            self._update(task, status="running", note="Validating the URL")
            try:
                validate_public_url(task.intent.url)
            except ValidationError as exc:
                self._finish(task, build_message(FAILED, "error", f"Task failed: {exc}. The crawler was not started."))
                return

            if task.cancel_event.is_set():
                self._finish(task, build_message(CANCELLED, "warning", "Cancelled by you before the crawler started."))
                return

            self._update(
                task,
                note=(
                    f"Starting approved tool site-crawler (up to {task.intent.max_pages} pages, "
                    f"2 link levels, {CRAWL_TIMEOUT_S}s limit)"
                ),
            )
            cli = run_approved_tool("site-crawler", crawler_args(task.intent), CRAWL_TIMEOUT_S, task.cancel_event)
            self._update(task, note=f"Crawler exited with code {cli.exit_code} after {cli.duration_s}s")
            self._finish(task, build_result(task.intent, cli))
        except Exception:  # keep internals out of the chat; log the detail server-side
            log.exception("task %s crashed", task.id)
            self._finish(task, build_message(FAILED, "error", "Task failed because of an internal error. See the server log."))

    def _evict_locked(self) -> None:
        while len(self._tasks) > MAX_TASKS_KEPT:
            oldest_id, oldest = next(iter(self._tasks.items()))
            if oldest.status not in TERMINAL_STATES:
                break
            self._tasks.pop(oldest_id)
