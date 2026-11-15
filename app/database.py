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
    columns = [row["name"] for row in cursor.fetchall()]
    if "stages_json" not in columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN stages_json TEXT NOT NULL DEFAULT '[]'")


MIGRATIONS = [
    (1, "Create base tasks table and indexes", _migration_v1),
    (2, "Add stages_json column for historical stage timeline persistence", _migration_v2),
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
                    intent_json, plan_json, result_json, error_json, progress_json, stages_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                stages_json=row["stages_json"] if "stages_json" in row.keys() else "[]",
            )

    def list_tasks(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, prompt, task_type, target_url, status, tool,
                       created_at, started_at, completed_at, duration_s, exit_code
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
                })
            return results
