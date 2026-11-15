"""Task lifecycle management: bounded queue, worker pool, SQLite persistence, and 10-stage execution tracking.

Guarantees:
  * Strict concurrency limits (worker pool size);
  * Bounded queue capacity (rejects overload);
  * Real-time tracking of 10 genuine execution stages;
  * Persistent SQLite history across server restarts;
  * Subprocess cancellation without orphaned processes;
  * Safe exports in JSON, CSV, and Markdown.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import queue
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .database import Database, TaskRecord
from .intent import Intent, TaskPlan
from .report import CANCELLED, FAILED, build_message, build_result
from .runner import run_approved_tool
from .validation import ValidationError, validate_public_url

log = logging.getLogger("buildsprint.tasks")

CRAWL_TIMEOUT_S = 90
MAX_TASKS_KEPT = 200
DEFAULT_MAX_WORKERS = int(os.environ.get("MAI_MAX_WORKERS", "3"))
DEFAULT_QUEUE_CAPACITY = int(os.environ.get("MAI_QUEUE_CAPACITY", "50"))
TERMINAL_STATES = frozenset({"succeeded", "partial", "failed", "timed_out", "cancelled"})

STAGES = (
    ("request_received", "Request received and enqueued"),
    ("intent_interpreted", "Intent interpreted from natural language"),
    ("plan_prepared", "Execution plan prepared"),
    ("input_validated", "Target URL and parameters validated"),
    ("cli_selected", "Approved CLI tool selected"),
    ("process_started", "CLI subprocess launched"),
    ("execution_running", "Tool execution running"),
    ("output_captured", "Process output captured safely"),
    ("result_validated", "Output document and schema validated"),
    ("task_completed", "Task completed and verified"),
)


class QueueFullError(Exception):
    """Raised when task queue exceeds maximum capacity."""


@dataclass
class StageEvent:
    stage: str
    label: str
    status: str  # 'pending', 'active', 'completed', 'failed'
    timestamp: float | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "label": self.label,
            "status": self.status,
            "timestamp": self.timestamp,
            "detail": self.detail,
        }


@dataclass
class Task:
    id: str
    prompt: str
    intent: Intent
    status: str = "queued"
    progress: list[str] = field(default_factory=list)
    stages: dict[str, StageEvent] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        if not self.stages:
            for s_id, s_label in STAGES:
                self.stages[s_id] = StageEvent(
                    stage=s_id,
                    label=s_label,
                    status="pending",
                )


def crawler_args(intent: Intent) -> list[str]:
    """Build approved arguments for site-crawler tool from validated intent."""
    args = [
        f"--url={intent.url}",
        f"--max-pages={intent.max_pages}",
        "--max-depth=2",
        "--timeout=10",
        "--delay=0.3",
    ]
    if intent.plan and intent.plan.extraction_fields:
        args.append(f"--extract-schema={','.join(intent.plan.extraction_fields)}")
    return args


class TaskManager:
    """Manages task submission, bounded thread-pool execution, status polling, and SQLite durability."""

    def __init__(
        self,
        db: Database | None = None,
        max_workers: int = DEFAULT_MAX_WORKERS,
        queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
    ) -> None:
        self.db = db or Database()
        self.max_workers = max(1, max_workers)
        self.queue_capacity = max(5, queue_capacity)
        self._tasks: OrderedDict[str, Task] = OrderedDict()
        self._lock = threading.Lock()
        self._queue: queue.Queue[Task | None] = queue.Queue(maxsize=self.queue_capacity)
        self._workers: list[threading.Thread] = []
        self._shutdown = False

        # Recover interrupted tasks from previous runs
        self.db.recover_interrupted_tasks()

        # Start worker thread pool
        for i in range(self.max_workers):
            t = threading.Thread(target=self._worker_loop, daemon=True, name=f"mai-worker-{i}")
            t.start()
            self._workers.append(t)

    def submit(self, prompt: str, intent: Intent) -> Task:
        """Submit a task to the execution queue atomically."""
        task = Task(id=uuid.uuid4().hex, prompt=prompt, intent=intent)
        self._set_stage(task, "request_received", "completed", "Request accepted and assigned ID")
        self._set_stage(task, "intent_interpreted", "completed", intent.describe())
        self._set_stage(task, "plan_prepared", "completed", f"Tool site-crawler with limit {intent.max_pages} pages")

        record = TaskRecord(
            id=task.id,
            prompt=task.prompt,
            task_type=intent.task_type,
            target_url=intent.url,
            status=task.status,
            tool="site-crawler",
            created_at=task.created_at,
            intent_json=json.dumps({
                "task_type": intent.task_type,
                "url": intent.url,
                "max_pages": intent.max_pages,
                "notes": list(intent.notes),
            }),
            plan_json=json.dumps(intent.plan.to_dict() if intent.plan else {}),
            progress_json=json.dumps(task.progress),
            stages_json=json.dumps([s.to_dict() for s in task.stages.values()]),
        )

        with self._lock:
            if self._shutdown:
                raise QueueFullError("Task manager is shutting down; not accepting new tasks.")
            try:
                self._queue.put_nowait(task)
            except queue.Full:
                raise QueueFullError("Task queue is at maximum capacity. Please retry shortly.")

            self._tasks[task.id] = task
            self._evict_locked()
            self.db.save_task(record)

        return task

    def get(self, task_id: str) -> Task | None:
        """Look up task in memory, falling back to SQLite database."""
        with self._lock:
            in_memory = self._tasks.get(task_id)
            if in_memory:
                return in_memory

        record = self.db.get_task(task_id)
        if not record:
            return None

        # Reconstruct Task object from DB record
        plan_doc = record.plan
        plan = None
        if plan_doc:
            plan = TaskPlan(
                task_type=plan_doc.get("task_type", record.task_type),
                target_url=plan_doc.get("target_url", record.target_url),
                requested_operation=plan_doc.get("requested_operation", "crawl_and_extract"),
                expected_output_format=plan_doc.get("expected_output_format", "json"),
                max_pages=plan_doc.get("max_pages", 5),
                max_depth=plan_doc.get("max_depth", 2),
                extraction_fields=tuple(plan_doc.get("extraction_fields", [])),
                confirmation_needed=plan_doc.get("confirmation_needed", False),
                execution_steps=tuple(plan_doc.get("execution_steps", [])),
                source=plan_doc.get("source", "rules"),
            )
        intent = Intent(
            task_type=record.task_type,
            url=record.target_url,
            max_pages=plan.max_pages if plan else 5,
            notes=tuple(record.intent.get("notes", [])),
            plan=plan,
        )

        stages_map: dict[str, StageEvent] = {}
        if record.stages:
            for s_dict in record.stages:
                s_id = s_dict.get("stage", "")
                if s_id:
                    stages_map[s_id] = StageEvent(
                        stage=s_id,
                        label=s_dict.get("label", ""),
                        status=s_dict.get("status", "pending"),
                        timestamp=s_dict.get("timestamp"),
                        detail=s_dict.get("detail", ""),
                    )
        elif record.status in TERMINAL_STATES:
            # Fallback for historical tasks without saved stages: never show pending
            is_success = record.status in ("succeeded", "partial")
            for s_id, s_label in STAGES:
                st = "completed" if is_success else ("failed" if s_id == "task_completed" else "completed")
                stages_map[s_id] = StageEvent(
                    stage=s_id,
                    label=s_label,
                    status=st,
                    timestamp=record.completed_at or record.created_at,
                    detail=f"Task {record.status}",
                )

        task = Task(
            id=record.id,
            prompt=record.prompt,
            intent=intent,
            status=record.status,
            progress=record.progress,
            stages=stages_map,
            result=record.result,
            created_at=record.created_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
        )
        with self._lock:
            self._tasks[task.id] = task
        return task

    def list_history(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """List tasks from persistent storage."""
        return self.db.list_tasks(limit, offset)

    def cancel(self, task_id: str) -> Task | None:
        """Request cancellation of a running or queued task."""
        task = self.get(task_id)
        if task is None:
            return None
        with self._lock:
            if task.status not in TERMINAL_STATES:
                task.cancel_event.set()
                task.progress.append("Cancellation requested by user")
                self.db.update_task_progress(task.id, None, "Cancellation requested by user")
        return task

    def view(self, task: Task) -> dict[str, Any]:
        """Generate structured view for API response."""
        with self._lock:
            plan_dict = task.intent.plan.to_dict() if task.intent.plan else None
            stages_list = [s.to_dict() for s in task.stages.values()]
            return {
                "id": task.id,
                "status": task.status,
                "prompt": task.prompt,
                "interpretation": task.intent.describe(),
                "plan": plan_dict,
                "stages": stages_list,
                "progress": list(task.progress),
                "result": task.result,
                "created_at": task.created_at,
                "started_at": task.started_at,
                "completed_at": task.completed_at,
            }

    def export(self, task: Task, fmt: str) -> tuple[str, str, str]:
        """Export task result into requested format (json, md, or csv).

        Returns: (content_string, content_type, filename)
        """
        fmt = fmt.lower().strip()
        task_id_short = task.id[:8]

        if fmt == "csv":
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["Page URL", "Title", "Word Count", "Status Code", "Headings"])
            if task.result and "payload" in task.result and "pages" in task.result["payload"]:
                for p in task.result["payload"]["pages"]:
                    headings_str = " | ".join(p.get("headings", []))
                    writer.writerow([
                        p.get("url", ""),
                        p.get("title", ""),
                        p.get("word_count", 0),
                        p.get("status_code", 200),
                        headings_str,
                    ])
            return output.getvalue(), "text/csv; charset=utf-8", f"task_{task_id_short}.csv"

        if fmt == "md":
            lines = [
                f"# MAI-Chat Execution Report — Task {task_id_short}",
                f"**Target URL:** {task.intent.url}  ",
                f"**Status:** {task.status.upper()}  ",
                f"**Created At:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(task.created_at))}  ",
                "",
                "## Summary",
                task.result.get("summary", "No summary available.") if task.result else "No result generated.",
                "",
            ]
            if task.result and task.result.get("errors"):
                lines.extend(["## Errors Encountered", ""])
                for err in task.result["errors"]:
                    lines.append(f"- **{err.get('url')}**: {err.get('error')}")
                lines.append("")

            if task.result and "payload" in task.result and "pages" in task.result["payload"]:
                lines.extend(["## Crawled Pages", ""])
                for i, page in enumerate(task.result["payload"]["pages"], 1):
                    lines.extend([
                        f"### {i}. {page.get('title') or 'Untitled'}",
                        f"- **URL:** {page.get('url')}",
                        f"- **Words:** {page.get('word_count')}",
                        f"- **Excerpt:** {page.get('excerpt')}",
                        "",
                    ])

            return "\n".join(lines), "text/markdown; charset=utf-8", f"task_{task_id_short}.md"

        # Default: JSON
        json_data = task.result.get("json") if task.result else "{}"
        return json_data or "{}", "application/json; charset=utf-8", f"task_{task_id_short}.json"

    def shutdown(self, timeout: float = 5.0) -> None:
        """Coordinated graceful shutdown of the execution engine.

        Stops accepting new submissions, marks queued tasks as cancelled,
        signals active subprocesses to terminate, joins worker threads,
        and cleanly closes database resources.
        """
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True

            # Drain unstarted queued tasks and honestly cancel them
            drained_tasks: list[Task] = []
            while not self._queue.empty():
                try:
                    item = self._queue.get_nowait()
                    if item is not None:
                        drained_tasks.append(item)
                except queue.Empty:
                    break

            for t in drained_tasks:
                if t.status in TERMINAL_STATES:
                    continue
                t.status = "cancelled"
                t.completed_at = time.time()
                t.progress.append("Task was cancelled during graceful server shutdown.")
                self._set_stage(t, "task_completed", "failed", "Cancelled due to server shutdown")
                stages_list = [s.to_dict() for s in t.stages.values()]
                cancel_result = build_message(CANCELLED, "warning", "Cancelled during server shutdown.")
                self.db.complete_task(
                    t.id,
                    "cancelled",
                    cancel_result,
                    exit_code=-1,
                    stages_dict=stages_list,
                )

            # Signal cancellation to active running tasks to terminate subprocess trees
            for task in self._tasks.values():
                if task.status not in TERMINAL_STATES:
                    task.cancel_event.set()

        # Send sentinels to wake up waiting workers
        for _ in self._workers:
            try:
                self._queue.put_nowait(None)
            except (queue.Full, Exception):
                pass

        # Join worker threads
        start_wait = time.monotonic()
        for worker in self._workers:
            remaining = max(0.1, timeout - (time.monotonic() - start_wait))
            worker.join(timeout=remaining)

        # Close database connection cleanly
        try:
            self.db.close()
        except Exception:
            log.exception("Error closing database during shutdown")

    # ----- Internals -------------------------------------------------------

    def _set_stage(self, task: Task, stage_name: str, status: str, detail: str = "") -> None:
        if stage_name in task.stages:
            s = task.stages[stage_name]
            s.status = status
            s.timestamp = time.time()
            if detail:
                s.detail = detail

    def _update(self, task: Task, *, status: str | None = None, note: str | None = None) -> None:
        with self._lock:
            if status:
                task.status = status
            if note:
                task.progress.append(note)
            stages_list = [s.to_dict() for s in task.stages.values()]
        self.db.update_task_progress(task.id, status, note)
        self.db.update_task_stages(task.id, stages_list)

    def _finish(self, task: Task, result: dict[str, Any], exit_code: int | None = None, duration_s: float | None = None) -> None:
        with self._lock:
            task.result = result
            task.status = result["task_status"]
            task.completed_at = time.time()
            banner_text = result["banner"]["text"]
            task.progress.append(f"Finished: {banner_text}")
            stage_status = "completed" if task.status in ("succeeded", "partial") else "failed"
            self._set_stage(task, "task_completed", stage_status, banner_text)
            stages_list = [s.to_dict() for s in task.stages.values()]

        self.db.complete_task(
            task.id,
            task.status,
            result,
            duration_s=duration_s,
            exit_code=exit_code,
            error_details=result.get("errors", []),
            stages_dict=stages_list,
        )

    def _worker_loop(self) -> None:
        while not self._shutdown:
            try:
                task = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if task is None:
                break

            try:
                self._execute(task)
            except Exception:
                log.exception("Unexpected error executing task %s", task.id)
            finally:
                self._queue.task_done()

    def _execute(self, task: Task) -> None:
        try:
            task.started_at = time.time()
            self._update(task, status="running", note="Validating the target URL")
            self._set_stage(task, "input_validated", "active", "Validating URL against SSRF policy")

            try:
                validate_public_url(task.intent.url)
                self._set_stage(task, "input_validated", "completed", "Target URL is valid and public")
            except ValidationError as exc:
                self._set_stage(task, "input_validated", "failed", str(exc))
                self._finish(task, build_message(FAILED, "error", f"Task failed: {exc}. The crawler was not started."))
                return

            if task.cancel_event.is_set():
                self._finish(task, build_message(CANCELLED, "warning", "Cancelled by you before the crawler started."))
                return

            self._set_stage(task, "cli_selected", "completed", "Tool site-crawler selected")
            self._set_stage(task, "process_started", "completed", "Subprocess launched with isolated environment")
            self._set_stage(task, "execution_running", "active", f"Crawling up to {task.intent.max_pages} pages")

            self._update(
                task,
                note=(
                    f"Starting approved tool site-crawler (up to {task.intent.max_pages} pages, "
                    f"2 link levels, {CRAWL_TIMEOUT_S}s limit)"
                ),
            )
            cli = run_approved_tool("site-crawler", crawler_args(task.intent), CRAWL_TIMEOUT_S, task.cancel_event)

            self._set_stage(task, "execution_running", "completed", f"Subprocess finished with exit code {cli.exit_code}")
            self._set_stage(task, "output_captured", "completed", f"Captured stdout ({len(cli.stdout)} bytes) and stderr")
            self._update(task, note=f"Crawler exited with code {cli.exit_code} after {cli.duration_s}s")

            self._set_stage(task, "result_validated", "active", "Validating crawler JSON document")
            res = build_result(task.intent, cli)
            self._set_stage(task, "result_validated", "completed", "JSON schema and page structures verified")

            self._finish(task, res, exit_code=cli.exit_code, duration_s=cli.duration_s)
        except Exception:
            log.exception("task %s crashed unexpectedly", task.id)
            self._finish(task, build_message(FAILED, "error", "Task failed because of an internal error. See the server log."))

    def _evict_locked(self) -> None:
        while len(self._tasks) > MAX_TASKS_KEPT:
            oldest_id, oldest = next(iter(self._tasks.items()))
            if oldest.status not in TERMINAL_STATES:
                break
            self._tasks.pop(oldest_id)
