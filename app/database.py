"""SQLite persistence layer with versioned migrations for MAI-Chat task lifecycle.

Guarantees durable task tracking and stage restoration across server restarts.
Interrupted executions are honestly marked as interrupted/failed, never successful.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator

log = logging.getLogger("buildsprint.database")

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "mai_chat.db"


@dataclass
class TaskRecord:
    id: str
    prompt: str
    task_type: str
    target_url: str
    status: str
    tool: str
    created_at: float
    started_at: float | None = None
    completed_at: float | None = None
    duration_s: float | None = None
    exit_code: int | None = None
    intent_json: str = "{}"
    plan_json: str = "{}"
    result_json: str | None = None
    error_json: str | None = None
    progress_json: str = "[]"
    stages_json: str = "[]"
    thread_id: str | None = None

    @property
    def intent(self) -> dict[str, Any]:
        try:
            return json.loads(self.intent_json)
        except Exception:
            return {}

    @property
    def plan(self) -> dict[str, Any]:
        try:
            return json.loads(self.plan_json)
        except Exception:
            return {}

    @property
    def result(self) -> dict[str, Any] | None:
        if not self.result_json:
            return None
        try:
            return json.loads(self.result_json)
        except Exception:
            return None

    @property
    def progress(self) -> list[str]:
        try:
            return json.loads(self.progress_json)
        except Exception:
            return []

    @property
    def stages(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.stages_json)
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Versioned Database Migrations
# ---------------------------------------------------------------------------

def _migration_v1(conn: sqlite3.Connection) -> None:
    """Version 1: Create base tasks table and indexes."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            prompt TEXT NOT NULL,
            task_type TEXT NOT NULL,
            target_url TEXT NOT NULL,
            status TEXT NOT NULL,
            tool TEXT NOT NULL,
            created_at REAL NOT NULL,
            started_at REAL,
            completed_at REAL,
            duration_s REAL,
            exit_code INTEGER,
            intent_json TEXT NOT NULL DEFAULT '{}',
            plan_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT,
            error_json TEXT,
            progress_json TEXT NOT NULL DEFAULT '[]'
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")


def _migration_v2(conn: sqlite3.Connection) -> None:
    """Version 2: Add stages_json column to tasks table for timeline persistence."""
    cursor = conn.execute("PRAGMA table_info(tasks)")
    columns = [row[1] for row in cursor.fetchall()]
    if "stages_json" not in columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN stages_json TEXT NOT NULL DEFAULT '[]'")


def _migration_v3(conn: sqlite3.Connection) -> None:
    """Version 3: Add conversation threads, thread messages, and thread_id on tasks."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS threads (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL DEFAULT '',
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL,
            context_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_threads_updated ON threads(updated_at DESC)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS thread_messages (
            id          TEXT PRIMARY KEY,
            thread_id   TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
            role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content     TEXT NOT NULL,
            task_id     TEXT,
            created_at  REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_thread_messages_thread ON thread_messages(thread_id, created_at)")

    cursor = conn.execute("PRAGMA table_info(tasks)")
    columns = [row[1] for row in cursor.fetchall()]
    if "thread_id" not in columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN thread_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_thread_id ON tasks(thread_id)")


MIGRATIONS = [
    (1, "Create base tasks table and indexes", _migration_v1),
    (2, "Add stages_json column for historical stage timeline persistence", _migration_v2),
    (3, "Add conversation threads and thread messages", _migration_v3),
]


class Database:
    """Manages SQLite connections, schema initialization, versioned migrations, and transactional queries."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            env_path = os.environ.get("MAI_DB_PATH")
            self.db_path = Path(env_path) if env_path else DEFAULT_DB_PATH
        else:
            self.db_path = Path(db_path)

        self._mem_conn: sqlite3.Connection | None = None
        if str(self.db_path) == ":memory:":
            self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
            self._mem_conn.row_factory = sqlite3.Row
            self._mem_conn.execute("PRAGMA foreign_keys = ON")
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._init_db()

    def close(self) -> None:
        """Safely close database connections and release file locks."""
        if self._mem_conn:
            try:
                self._mem_conn.close()
            except Exception:
                pass
            self._mem_conn = None

    @contextlib.contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager that ensures connections and file handles are safely managed."""
        if self._mem_conn is not None:
            self._mem_conn.execute("PRAGMA foreign_keys = ON")
            yield self._mem_conn
            return

        conn = sqlite3.connect(
            str(self.db_path),
            timeout=10.0,
            check_same_thread=False,
            isolation_level=None,  # autocommit mode
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Execute versioned database migrations in order."""
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_versions (
                    version INTEGER PRIMARY KEY,
                    applied_at REAL NOT NULL,
                    description TEXT NOT NULL DEFAULT ''
                )
                """
            )
            # Ensure description column exists if table was created in an earlier session
            cursor = conn.execute("PRAGMA table_info(schema_versions)")
            cols = [row["name"] for row in cursor.fetchall()]
            if "description" not in cols:
                conn.execute("ALTER TABLE schema_versions ADD COLUMN description TEXT NOT NULL DEFAULT ''")

            cursor = conn.execute("SELECT version FROM schema_versions ORDER BY version ASC")
            applied = {row["version"] for row in cursor.fetchall()}

            for ver, desc, migration_func in MIGRATIONS:
                if ver not in applied:
                    log.info("Applying database migration v%d: %s", ver, desc)
                    migration_func(conn)
                    conn.execute(
                        "INSERT INTO schema_versions (version, applied_at, description) VALUES (?, ?, ?)",
                        (ver, time.time(), desc),
                    )

    def recover_interrupted_tasks(self) -> int:
        """On startup, any tasks left in 'running' or 'queued' state are honestly marked interrupted."""
        with self._connection() as conn:
            now = time.time()
            cursor = conn.execute(
                """
                SELECT id, progress_json, stages_json FROM tasks WHERE status IN ('queued', 'running')
                """
            )
            interrupted = cursor.fetchall()
            for row in interrupted:
                task_id = row["id"]
                try:
                    prog = json.loads(row["progress_json"])
                except Exception:
                    prog = []
                prog.append("Server restarted while task was in progress; marked as interrupted.")

                # Mark any active stage as failed
                try:
                    stages = json.loads(row["stages_json"])
                    for s in stages:
                        if s.get("status") == "active":
                            s["status"] = "failed"
                            s["detail"] = "Interrupted by server restart"
                except Exception:
                    stages = []

                error_doc = {
                    "task_status": "failed",
                    "banner": {
                        "kind": "error",
                        "text": "Task was interrupted by a server restart before completion.",
                    },
                    "summary": None,
                    "json": None,
                    "errors": [{"url": "", "error": "Server restarted during execution"}],
                    "execution": {"exit_code": -1, "interrupted": True},
                }
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'failed',
                        completed_at = ?,
                        result_json = ?,
                        error_json = ?,
                        progress_json = ?,
                        stages_json = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        json.dumps(error_doc),
                        json.dumps(error_doc["errors"]),
                        json.dumps(prog),
                        json.dumps(stages),
                        task_id,
                    ),
                )
            if interrupted:
                log.info("Recovered %d interrupted tasks upon database startup.", len(interrupted))
            return len(interrupted)

    def save_task(self, record: TaskRecord) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO tasks (
                    id, prompt, task_type, target_url, status, tool,
                    created_at, started_at, completed_at, duration_s, exit_code,
                    intent_json, plan_json, result_json, error_json, progress_json, stages_json, thread_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.prompt,
                    record.task_type,
                    record.target_url,
                    record.status,
                    record.tool,
                    record.created_at,
                    record.started_at,
                    record.completed_at,
                    record.duration_s,
                    record.exit_code,
                    record.intent_json,
                    record.plan_json,
                    record.result_json,
                    record.error_json,
                    record.progress_json,
                    record.stages_json,
                    record.thread_id,
                ),
            )

    def update_task_progress(self, task_id: str, status: str | None, progress_line: str | None) -> None:
        with self._connection() as conn:
            row = conn.execute("SELECT status, progress_json, started_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return
            new_status = status or row["status"]
            started_at = row["started_at"]
            if new_status == "running" and not started_at:
                started_at = time.time()
            try:
                prog = json.loads(row["progress_json"])
            except Exception:
                prog = []
            if progress_line:
                prog.append(progress_line)

            conn.execute(
                """
                UPDATE tasks
                SET status = ?, started_at = ?, progress_json = ?
                WHERE id = ?
                """,
                (new_status, started_at, json.dumps(prog), task_id),
            )

    def update_task_stages(self, task_id: str, stages_dict: list[dict[str, Any]]) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE tasks SET stages_json = ? WHERE id = ?",
                (json.dumps(stages_dict), task_id),
            )

    def complete_task(
        self,
        task_id: str,
        status: str,
        result: dict[str, Any],
        duration_s: float | None = None,
        exit_code: int | None = None,
        error_details: list[dict] | None = None,
        stages_dict: list[dict[str, Any]] | None = None,
    ) -> None:
        with self._connection() as conn:
            row = conn.execute("SELECT progress_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return
            try:
                prog = json.loads(row["progress_json"])
            except Exception:
                prog = []
            banner_text = result.get("banner", {}).get("text", status)
            prog.append(f"Finished: {banner_text}")
            now = time.time()

            if stages_dict is not None:
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?,
                        completed_at = ?,
                        duration_s = ?,
                        exit_code = ?,
                        result_json = ?,
                        error_json = ?,
                        progress_json = ?,
                        stages_json = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        now,
                        duration_s,
                        exit_code,
                        json.dumps(result),
                        json.dumps(error_details or []),
                        json.dumps(prog),
                        json.dumps(stages_dict),
                        task_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?,
                        completed_at = ?,
                        duration_s = ?,
                        exit_code = ?,
                        result_json = ?,
                        error_json = ?,
                        progress_json = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        now,
                        duration_s,
                        exit_code,
                        json.dumps(result),
                        json.dumps(error_details or []),
                        json.dumps(prog),
                        task_id,
                    ),
                )

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return None
            keys = row.keys()
            return TaskRecord(
                id=row["id"],
                prompt=row["prompt"],
                task_type=row["task_type"],
                target_url=row["target_url"],
                status=row["status"],
                tool=row["tool"],
                created_at=row["created_at"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
                duration_s=row["duration_s"],
                exit_code=row["exit_code"],
                intent_json=row["intent_json"],
                plan_json=row["plan_json"],
                result_json=row["result_json"],
                error_json=row["error_json"],
                progress_json=row["progress_json"],
                stages_json=row["stages_json"] if "stages_json" in keys else "[]",
                thread_id=row["thread_id"] if "thread_id" in keys else None,
            )

    def list_tasks(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, prompt, task_type, target_url, status, tool,
                       created_at, started_at, completed_at, duration_s, exit_code, thread_id
                FROM tasks
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (min(max(limit, 1), 200), max(offset, 0)),
            )
            results = []
            for r in cursor.fetchall():
                results.append({
                    "id": r["id"],
                    "prompt": r["prompt"],
                    "task_type": r["task_type"],
                    "target_url": r["target_url"],
                    "status": r["status"],
                    "tool": r["tool"],
                    "created_at": r["created_at"],
                    "started_at": r["started_at"],
                    "completed_at": r["completed_at"],
                    "duration_s": r["duration_s"],
                    "exit_code": r["exit_code"],
                    "thread_id": r["thread_id"] if "thread_id" in r.keys() else None,
                })
            return results

    # -----------------------------------------------------------------------
    # Conversation Threads & Thread Messages
    # -----------------------------------------------------------------------

    def create_thread(self, thread_id: str, title: str = "", now: float | None = None) -> None:
        """Create a new conversation thread."""
        now = now if now is not None else time.time()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO threads (id, title, created_at, updated_at, context_json)
                VALUES (?, ?, ?, ?, '{}')
                """,
                (thread_id, (title or "")[:80], now, now),
            )

    def list_threads(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """List threads with message count and target URL ordered by updated_at DESC."""
        with self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT t.id, t.title, t.created_at, t.updated_at, t.context_json,
                       (SELECT COUNT(*) FROM thread_messages m WHERE m.thread_id = t.id) AS message_count
                FROM threads t
                ORDER BY t.updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (min(max(limit, 1), 200), max(offset, 0)),
            )
            results = []
            for r in cursor.fetchall():
                context = {}
                try:
                    context = json.loads(r["context_json"])
                except Exception:
                    pass
                results.append({
                    "id": r["id"],
                    "title": r["title"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "message_count": r["message_count"],
                    "target_url": context.get("target_url", ""),
                })
            return results

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        """Retrieve thread metadata, parsed crawl memory context, and message count."""
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT t.id, t.title, t.created_at, t.updated_at, t.context_json,
                       (SELECT COUNT(*) FROM thread_messages m WHERE m.thread_id = t.id) AS message_count
                FROM threads t
                WHERE t.id = ?
                """,
                (thread_id,),
            ).fetchone()
            if not row:
                return None
            context = {}
            try:
                context = json.loads(row["context_json"])
            except Exception:
                pass
            return {
                "id": row["id"],
                "title": row["title"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "message_count": row["message_count"],
                "target_url": context.get("target_url", ""),
                "context": context,
                "has_crawl_memory": bool(context.get("crawl_doc")),
            }

    def delete_thread(self, thread_id: str) -> bool:
        """Delete thread and cascade-delete its messages."""
        with self._connection() as conn:
            cursor = conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
            return cursor.rowcount > 0

    def update_thread_title(self, thread_id: str, title: str, now: float | None = None) -> None:
        """Update thread title."""
        now = now if now is not None else time.time()
        with self._connection() as conn:
            conn.execute(
                "UPDATE threads SET title = ?, updated_at = ? WHERE id = ?",
                ((title or "")[:80], now, thread_id),
            )

    def update_thread_context(self, thread_id: str, context: dict, now: float | None = None) -> None:
        """Update thread crawl memory context under bounded memory limits."""
        now = now if now is not None else time.time()
        cleaned_context = dict(context)

        # Enforce crawl_doc 2MB cap
        if "crawl_doc" in cleaned_context and cleaned_context["crawl_doc"]:
            try:
                raw_json = json.dumps(cleaned_context["crawl_doc"], ensure_ascii=False)
                if len(raw_json.encode("utf-8")) > 2_000_000:
                    cleaned_context["crawl_doc"] = None
                    cleaned_context["crawl_doc_oversized"] = True
            except Exception:
                pass

        # Cap history at last 20 entries, each content <= 1000 chars
        if "history" in cleaned_context and isinstance(cleaned_context["history"], list):
            capped = []
            for item in cleaned_context["history"][-20:]:
                if isinstance(item, dict):
                    capped.append({
                        "role": str(item.get("role", "user")),
                        "content": str(item.get("content", ""))[:1000],
                    })
            cleaned_context["history"] = capped

        with self._connection() as conn:
            conn.execute(
                "UPDATE threads SET context_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(cleaned_context), now, thread_id),
            )

    def add_thread_message(
        self,
        message_id: str,
        thread_id: str,
        role: str,
        content: str,
        task_id: str | None = None,
        now: float | None = None,
    ) -> None:
        """Record message in thread and bump updated_at timestamp."""
        now = now if now is not None else time.time()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO thread_messages (id, thread_id, role, content, task_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (message_id, thread_id, role, (content or "")[:1000], task_id, now),
            )
            conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (now, thread_id))

    def list_thread_messages(self, thread_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """List messages in a thread in chronological order."""
        with self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, thread_id, role, content, task_id, created_at
                FROM thread_messages
                WHERE thread_id = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (thread_id, min(max(limit, 1), 500)),
            )
            results = []
            for r in cursor.fetchall():
                results.append({
                    "id": r["id"],
                    "thread_id": r["thread_id"],
                    "role": r["role"],
                    "content": r["content"],
                    "task_id": r["task_id"],
                    "created_at": r["created_at"],
                })
            return results

    def count_threads(self) -> int:
        """Count total threads for capacity limits."""
        with self._connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS total FROM threads").fetchone()
            return row["total"] if row else 0
