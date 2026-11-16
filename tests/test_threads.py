"""Tests for conversation threads, crawl memory, and follow-up executions in MAI-Chat.

Phase 1 covers:
  - Database migration v3 on new and existing v2 databases;
  - Idempotency of migration v3;
  - Foreign key cascading deletion on thread delete;
  - Enforcement of foreign keys constraint.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest import mock

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from app.database import (
    MIGRATIONS,
    Database,
    TaskRecord,
    _migration_v1,
    _migration_v2,
    _migration_v3,
)
from app.intent import (
    TASK_CRAWL,
    TASK_FOLLOWUP,
    FollowupIntent,
    Intent,
    IntentError,
    RouteDecision,
    parse_followup,
    parse_intent,
    route_thread_message,
)
from app.server import create_server
from app.tasks import (
    QueueFullError,
    TaskManager,
    ThreadBusyError,
)


class ThreadDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_threads.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_migration_v3_applies_to_existing_v2_database(self) -> None:
        """Simulate an existing v2 database and verify migration v3 upgrades it seamlessly."""
        # 1. Create a raw SQLite database with only v1 and v2
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """
                CREATE TABLE schema_versions (
                    version INTEGER PRIMARY KEY,
                    applied_at REAL NOT NULL,
                    description TEXT NOT NULL DEFAULT ''
                )
                """
            )
            _migration_v1(conn)
            conn.execute("INSERT INTO schema_versions (version, applied_at, description) VALUES (1, ?, 'v1')", (time.time(),))
            _migration_v2(conn)
            conn.execute("INSERT INTO schema_versions (version, applied_at, description) VALUES (2, ?, 'v2')", (time.time(),))

            # Insert a pre-existing task
            conn.execute(
                """
                INSERT INTO tasks (
                    id, prompt, task_type, target_url, status, tool,
                    created_at, started_at, completed_at, duration_s, exit_code,
                    intent_json, plan_json, result_json, error_json, progress_json, stages_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "task-existing-123", "Crawl https://example.com", "website_crawl", "https://example.com",
                    "succeeded", "site-crawler", 1000.0, 1000.1, 1001.0, 0.9, 0,
                    "{}", "{}", "{}", "[]", "[]", "[]",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        # 2. Open with the application Database class
        db = Database(self.db_path)
        try:
            # Check schema_versions has version 3
            with db._connection() as c:
                cur = c.execute("SELECT version, description FROM schema_versions ORDER BY version ASC")
                versions = [r["version"] for r in cur.fetchall()]
                self.assertEqual(versions, [1, 2, 3])

                # Check threads and thread_messages tables exist
                cur = c.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = {r["name"] for r in cur.fetchall()}
                self.assertIn("threads", tables)
                self.assertIn("thread_messages", tables)

                # Check thread_id column exists on tasks table
                cur = c.execute("PRAGMA table_info(tasks)")
                task_cols = [r["name"] for r in cur.fetchall()]
                self.assertIn("thread_id", task_cols)

            # Check pre-existing task is still readable
            old_task = db.get_task("task-existing-123")
            self.assertIsNotNone(old_task)
            self.assertEqual(old_task.id, "task-existing-123")
            self.assertIsNone(old_task.thread_id)
        finally:
            db.close()

    def test_migration_v3_is_idempotent(self) -> None:
        """Calling _migration_v3 multiple times on the same connection raises no error."""
        conn = sqlite3.connect(":memory:")
        _migration_v1(conn)
        _migration_v2(conn)
        _migration_v3(conn)
        # Calling second time should not raise
        _migration_v3(conn)
        conn.close()

    def test_thread_delete_cascades_messages(self) -> None:
        """Deleting a thread must cascade-delete all its thread_messages."""
        db = Database(self.db_path)
        try:
            t_id = uuid.uuid4().hex
            db.create_thread(t_id, "Test Thread")

            msg1_id = uuid.uuid4().hex
            msg2_id = uuid.uuid4().hex
            db.add_thread_message(msg1_id, t_id, "user", "Hello website")
            db.add_thread_message(msg2_id, t_id, "assistant", "Hello user")

            messages = db.list_thread_messages(t_id)
            self.assertEqual(len(messages), 2)

            # Delete thread
            deleted = db.delete_thread(t_id)
            self.assertTrue(deleted)

            # Thread should be gone
            self.assertIsNone(db.get_thread(t_id))

            # Messages must be cascade-deleted
            remaining_msgs = db.list_thread_messages(t_id)
            self.assertEqual(len(remaining_msgs), 0)

            # Direct table check
            with db._connection() as conn:
                cur = conn.execute("SELECT COUNT(*) AS cnt FROM thread_messages WHERE thread_id = ?", (t_id,))
                self.assertEqual(cur.fetchone()["cnt"], 0)
        finally:
            db.close()

    def test_foreign_keys_enabled(self) -> None:
        """Inserting a message with an invalid thread_id must fail foreign key check."""
        db = Database(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                db.add_thread_message(uuid.uuid4().hex, "non-existent-thread-id", "user", "Fail me")
        finally:
            db.close()

    def test_thread_crud_and_context(self) -> None:
        """Test creating, listing, updating context, and retrieving threads."""
        db = Database(self.db_path)
        try:
            t_id = uuid.uuid4().hex
            db.create_thread(t_id, "Example Thread")

            # Check get_thread
            t = db.get_thread(t_id)
            self.assertIsNotNone(t)
            self.assertEqual(t["title"], "Example Thread")
            self.assertEqual(t["message_count"], 0)
            self.assertFalse(t["has_crawl_memory"])

            # Update context with crawl_doc and history
            context = {
                "target_url": "https://example.com/",
                "host": "example.com",
                "crawled_at": time.time(),
                "crawl_doc": {
                    "status": "success",
                    "host": "example.com",
                    "pages": [{"url": "https://example.com/", "title": "Example Domain"}],
                },
                "history": [
                    {"role": "user", "content": "Crawl https://example.com"},
                    {"role": "assistant", "content": "Done"},
                ],
            }
            db.update_thread_context(t_id, context)

            t_updated = db.get_thread(t_id)
            self.assertTrue(t_updated["has_crawl_memory"])
            self.assertEqual(t_updated["target_url"], "https://example.com/")
            self.assertEqual(len(t_updated["context"]["history"]), 2)

            # Check list_threads
            threads_list = db.list_threads()
            self.assertEqual(len(threads_list), 1)
            self.assertEqual(threads_list[0]["id"], t_id)
            self.assertEqual(threads_list[0]["target_url"], "https://example.com/")

            # Test 2MB cap on crawl_doc
            oversized_context = {
                "target_url": "https://huge.example.com/",
                "crawl_doc": {"data": "X" * 2_100_000},
            }
            db.update_thread_context(t_id, oversized_context)
            t_huge = db.get_thread(t_id)
            self.assertIsNone(t_huge["context"]["crawl_doc"])
            self.assertTrue(t_huge["context"]["crawl_doc_oversized"])
        finally:
            db.close()


class ThreadIntentRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mock_memory = {
            "target_url": "https://example.com/",
            "host": "example.com",
            "crawl_doc": {
                "status": "success",
                "host": "example.com",
                "pages": [{"url": "https://example.com/", "title": "Example Domain"}],
            },
            "history": [],
        }

    def test_route_with_url_is_crawl(self) -> None:
        # Prompt with URL routes to crawl regardless of whether memory exists
        decision1 = route_thread_message("Crawl https://example.com", None)
        self.assertEqual(decision1.action, "crawl")
        self.assertIsNotNone(decision1.intent)
        self.assertEqual(decision1.intent.url, "https://example.com/")

        decision2 = route_thread_message("Crawl https://another.com", self.mock_memory)
        self.assertEqual(decision2.action, "crawl")
        self.assertEqual(decision2.intent.url, "https://another.com/")

    def test_route_without_url_and_with_memory_is_followup(self) -> None:
        prompt = "What is this website about?"
        decision = route_thread_message(prompt, self.mock_memory)
        self.assertEqual(decision.action, "followup")
        self.assertEqual(decision.question, prompt)
        self.assertIsNone(decision.intent)

    def test_route_without_url_and_without_memory_is_needs_url(self) -> None:
        prompt = "What is this website about?"
        decision = route_thread_message(prompt, {})
        self.assertEqual(decision.action, "needs_url")
        self.assertIn("Give me a website to work on first", decision.message)
        self.assertIsNone(decision.intent)

    def test_route_invalid_lengths(self) -> None:
        with self.assertRaises(IntentError):
            route_thread_message("", self.mock_memory)
        with self.assertRaises(IntentError):
            route_thread_message("   ", self.mock_memory)
        with self.assertRaises(IntentError):
            route_thread_message("x" * 1001, self.mock_memory)

    def test_route_failed_crawl_keeps_previous_memory(self) -> None:
        """Verify that when a crawl fails, the previous crawl_doc is not overwritten in thread context."""
        temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(temp_dir.name) / "test.db"
        db = Database(db_path)
        try:
            t_id = uuid.uuid4().hex
            db.create_thread(t_id, "Memory Persistence Thread")

            # Store good initial crawl
            initial_context = {
                "target_url": "https://example.com/",
                "host": "example.com",
                "crawl_doc": {"status": "success", "host": "example.com", "pages": [{"url": "https://example.com/"}]},
                "history": [],
            }
            db.update_thread_context(t_id, initial_context)

            # Simulate logic for crawl outcome: if failed, preserve old crawl_doc
            def update_thread_after_task(thread_id: str, new_task_status: str, new_crawl_doc: dict | None, target_url: str):
                thread = db.get_thread(thread_id)
                ctx = thread["context"]
                if new_task_status in ("succeeded", "partial") and new_crawl_doc:
                    ctx["crawl_doc"] = new_crawl_doc
                    ctx["target_url"] = target_url
                # If failed/cancelled/timed_out: old crawl_doc is preserved
                db.update_thread_context(thread_id, ctx)

            # Test failed crawl attempt
            failed_doc = {"status": "failed", "pages": [], "errors": [{"error": "Connection refused"}]}
            update_thread_after_task(t_id, "failed", failed_doc, "https://failed.example.com/")

            # Verify memory still has example.com
            updated_thread = db.get_thread(t_id)
            self.assertEqual(updated_thread["context"]["target_url"], "https://example.com/")
            self.assertEqual(updated_thread["context"]["crawl_doc"]["status"], "success")
        finally:
            db.close()
            temp_dir.cleanup()


SAMPLE_CRAWL_DOC = {
    "status": "success",
    "host": "example.com",
    "start_url": "https://example.com/",
    "pages": [
        {
            "url": "https://example.com/",
            "title": "Example Domain",
            "meta_description": "Documentation example site",
            "excerpt": "This domain is established to be used for illustrative examples in documents.",
            "headings": [{"level": 1, "text": "Example Domain"}],
            "word_count": 35,
        }
    ],
    "errors": [],
}


class ThreadTaskExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_exec.db"
        self.db = Database(self.db_path)
        self.manager = TaskManager(db=self.db, max_workers=2, queue_capacity=10)

    def tearDown(self) -> None:
        self.manager.shutdown()
        self.temp_dir.cleanup()

    def _wait_for_task(self, task, timeout: float = 5.0):
        start = time.time()
        while time.time() - start < timeout:
            t = self.manager.get(task.id)
            if t and t.status in ("succeeded", "partial", "failed", "cancelled", "timed_out"):
                return t
            time.sleep(0.05)
        self.fail(f"Task {task.id} did not finish within {timeout}s")

    def test_followup_does_not_start_subprocess(self) -> None:
        t_id = uuid.uuid4().hex
        self.db.create_thread(t_id, "Subprocess Check")
        self.db.update_thread_context(t_id, {
            "target_url": "https://example.com/",
            "host": "example.com",
            "crawled_at": time.time(),
            "crawl_doc": SAMPLE_CRAWL_DOC,
            "history": [],
        })

        with mock.patch("app.tasks.run_approved_tool") as mock_run:
            task = self.manager.submit_followup(t_id, "What is this domain?", "What is this domain?")
            finished = self._wait_for_task(task)

            # Assert subprocess was NEVER called
            mock_run.assert_not_called()

        self.assertEqual(finished.status, "succeeded")
        self.assertIn("No new crawl was run", finished.result["banner"]["text"])

        # Check skipped stages
        self.assertEqual(finished.stages["input_validated"].status, "skipped")
        self.assertEqual(finished.stages["cli_selected"].status, "skipped")
        self.assertEqual(finished.stages["process_started"].status, "skipped")
        self.assertEqual(finished.stages["execution_running"].status, "skipped")
        self.assertEqual(finished.stages["output_captured"].status, "skipped")
        self.assertEqual(finished.stages["result_validated"].status, "completed")
        self.assertEqual(finished.stages["task_completed"].status, "completed")

        # Check execution metadata
        self.assertTrue(finished.result["execution"].get("reused_crawl"))

    def test_followup_without_memory_fails_honestly(self) -> None:
        t_id = uuid.uuid4().hex
        self.db.create_thread(t_id, "Empty Memory Thread")

        task = self.manager.submit_followup(t_id, "What is this?", "What is this?")
        finished = self._wait_for_task(task)

        self.assertEqual(finished.status, "failed")
        self.assertIn("No crawl memory", finished.result["banner"]["text"])

    def test_second_request_while_busy_is_rejected_with_409(self) -> None:
        t_id = uuid.uuid4().hex
        self.db.create_thread(t_id, "Busy Check")
        self.db.update_thread_context(t_id, {
            "target_url": "https://example.com/",
            "host": "example.com",
            "crawl_doc": SAMPLE_CRAWL_DOC,
            "history": [],
        })

        # Submit task and simulate running state
        task1 = self.manager.submit_followup(t_id, "First Q", "First Q")
        with self.manager._lock:
            task1.status = "running"
            self.manager._tasks[task1.id] = task1

        # Second submission to same thread must be rejected with ThreadBusyError (QueueFullError)
        with self.assertRaises((ThreadBusyError, QueueFullError)):
            self.manager.submit_followup(t_id, "Second Q", "Second Q")

    def test_failed_crawl_keeps_previous_memory(self) -> None:
        t_id = uuid.uuid4().hex
        self.db.create_thread(t_id, "Failed Crawl Keep Memory")
        self.db.update_thread_context(t_id, {
            "target_url": "https://example.com/",
            "host": "example.com",
            "crawl_doc": SAMPLE_CRAWL_DOC,
            "history": [],
        })

        # Submit a crawl that fails immediately during URL validation
        bad_intent = Intent(task_type="website_crawl", url="http://192.168.1.1", max_pages=1)
        task = self.manager.submit("Crawl private IP", bad_intent, thread_id=t_id)
        finished = self._wait_for_task(task)
        self.assertEqual(finished.status, "failed")

        # Verify thread memory still retains example.com
        thread = self.db.get_thread(t_id)
        self.assertEqual(thread["context"]["target_url"], "https://example.com/")
        self.assertIsNotNone(thread["context"]["crawl_doc"])
        self.assertEqual(thread["context"]["crawl_doc"]["host"], "example.com")

        # Verify follow-up still answers from previous memory
        followup_task = self.manager.submit_followup(t_id, "What domain is this?", "What domain is this?")
        finished_fu = self._wait_for_task(followup_task)
        self.assertEqual(finished_fu.status, "succeeded")
        self.assertIn("example.com", finished_fu.result["banner"]["text"])

    def test_threads_are_isolated(self) -> None:
        t_a = uuid.uuid4().hex
        t_b = uuid.uuid4().hex
        self.db.create_thread(t_a, "Thread Alpha")
        self.db.create_thread(t_b, "Thread Beta")

        doc_a = dict(SAMPLE_CRAWL_DOC, host="alpha.org", pages=[{"url": "https://alpha.org", "title": "Alpha Portal", "excerpt": "Alpha Portal Content"}])
        doc_b = dict(SAMPLE_CRAWL_DOC, host="beta.net", pages=[{"url": "https://beta.net", "title": "Beta Network", "excerpt": "Beta Network Content"}])

        self.db.update_thread_context(t_a, {"target_url": "https://alpha.org", "host": "alpha.org", "crawl_doc": doc_a, "history": []})
        self.db.update_thread_context(t_b, {"target_url": "https://beta.net", "host": "beta.net", "crawl_doc": doc_b, "history": []})

        task_a = self.manager.submit_followup(t_a, "Summarize site", "Summarize site")
        task_b = self.manager.submit_followup(t_b, "Summarize site", "Summarize site")

        res_a = self._wait_for_task(task_a)
        res_b = self._wait_for_task(task_b)

        self.assertIn("alpha.org", res_a.result["banner"]["text"])
        self.assertNotIn("beta.net", res_a.result["banner"]["text"])

        self.assertIn("beta.net", res_b.result["banner"]["text"])
        self.assertNotIn("alpha.org", res_b.result["banner"]["text"])

    def test_thread_memory_survives_restart(self) -> None:
        t_id = uuid.uuid4().hex
        self.db.create_thread(t_id, "Persistence Across Restart")
        self.db.update_thread_context(t_id, {
            "target_url": "https://example.com/",
            "host": "example.com",
            "crawl_doc": SAMPLE_CRAWL_DOC,
            "history": [],
        })

        # Shut down first manager
        self.manager.shutdown()

        # Reopen on same database file with fresh manager
        new_db = Database(self.db_path)
        new_manager = TaskManager(db=new_db, max_workers=2, queue_capacity=10)
        try:
            task = new_manager.submit_followup(t_id, "What is this?", "What is this?")
            start = time.time()
            finished = None
            while time.time() - start < 5.0:
                t = new_manager.get(task.id)
                if t and t.status in ("succeeded", "partial", "failed"):
                    finished = t
                    break
                time.sleep(0.05)
            self.assertIsNotNone(finished)
            self.assertEqual(finished.status, "succeeded")
            self.assertIn("example.com", finished.result["banner"]["text"])

            # Verify messages stored in DB
            msgs = new_db.list_thread_messages(t_id)
            self.assertGreaterEqual(len(msgs), 2)
            self.assertEqual(msgs[0]["role"], "user")
            self.assertEqual(msgs[1]["role"], "assistant")
        finally:
            new_manager.shutdown()


class ThreadApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "test_api.db"
        cls.server = create_server("127.0.0.1", 0, db_path=str(cls.db_path))
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        import threading
        cls._thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp_dir.cleanup()

    def _request(self, method: str, path: str, payload: dict | None = None, raw_body: bytes | None = None):
        data = raw_body if raw_body is not None else (json.dumps(payload).encode("utf-8") if payload is not None else None)
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if (payload is not None or raw_body is not None) else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                data = {"raw": raw.decode("utf-8", errors="replace")}
            return exc.code, data

    def test_post_threads_creates_thread_201(self) -> None:
        status, data = self._request("POST", "/api/threads", {"title": "Test Title"})
        self.assertEqual(status, 201)
        self.assertIn("id", data)
        self.assertEqual(data["title"], "Test Title")
        self.assertIn("created_at", data)

        # Empty body also creates thread
        status2, data2 = self._request("POST", "/api/threads", {})
        self.assertEqual(status2, 201)
        self.assertEqual(data2["title"], "")

    def test_get_threads_list_200(self) -> None:
        status, data = self._request("GET", "/api/threads?limit=10&offset=0")
        self.assertEqual(status, 200)
        self.assertIn("threads", data)
        self.assertIn("count", data)
        self.assertTrue(isinstance(data["threads"], list))

    def test_get_thread_by_id_200_404_400(self) -> None:
        # Create thread
        _, created = self._request("POST", "/api/threads", {"title": "Specific Thread"})
        t_id = created["id"]

        # Valid 200
        status, data = self._request("GET", f"/api/threads/{t_id}")
        self.assertEqual(status, 200)
        self.assertEqual(data["id"], t_id)
        self.assertEqual(data["title"], "Specific Thread")
        self.assertFalse(data["has_crawl_memory"])
        self.assertEqual(data["messages"], [])
        self.assertIsNone(data["active_task_id"])

        # Unknown ID 404
        status404, _ = self._request("GET", f"/api/threads/{uuid.uuid4().hex}")
        self.assertEqual(status404, 404)

        # Bad format 400
        status400, _ = self._request("GET", "/api/threads/not-valid-hex-id")
        self.assertEqual(status400, 400)

    def test_delete_thread_200_404_400(self) -> None:
        _, created = self._request("POST", "/api/threads", {"title": "Delete Me"})
        t_id = created["id"]

        status, data = self._request("DELETE", f"/api/threads/{t_id}")
        self.assertEqual(status, 200)
        self.assertTrue(data.get("deleted"))

        # Subsequent DELETE is 404
        status404, _ = self._request("DELETE", f"/api/threads/{t_id}")
        self.assertEqual(status404, 404)

        # Bad format is 400
        status400, _ = self._request("DELETE", "/api/threads/bad-id-format")
        self.assertEqual(status400, 400)

    def test_post_messages_needs_url_200(self) -> None:
        _, created = self._request("POST", "/api/threads", {"title": "No URL Thread"})
        t_id = created["id"]

        status, data = self._request("POST", f"/api/threads/{t_id}/messages", {"prompt": "What is this site?"})
        self.assertEqual(status, 200)
        self.assertEqual(data["route"], "needs_url")
        self.assertIn("Give me a website", data["message"])
        self.assertEqual(data["assistant_message"]["role"], "assistant")

    def test_post_messages_crawl_202(self) -> None:
        _, created = self._request("POST", "/api/threads", {"title": "Crawl Thread"})
        t_id = created["id"]

        status, data = self._request("POST", f"/api/threads/{t_id}/messages", {"prompt": "Crawl https://example.com"})
        self.assertEqual(status, 202)
        self.assertEqual(data["route"], "crawl")
        self.assertEqual(data["thread_id"], t_id)
        self.assertEqual(data["status"], "queued")
        self.assertIsNotNone(data["plan"])

    def test_post_messages_followup_202(self) -> None:
        _, created = self._request("POST", "/api/threads", {"title": "Followup Thread"})
        t_id = created["id"]

        # Seed memory in thread
        self.server.manager.db.update_thread_context(t_id, {
            "target_url": "https://example.com/",
            "host": "example.com",
            "crawl_doc": SAMPLE_CRAWL_DOC,
            "history": [],
        })

        status, data = self._request("POST", f"/api/threads/{t_id}/messages", {"prompt": "What is the site title?"})
        self.assertEqual(status, 202)
        self.assertEqual(data["route"], "followup")
        self.assertEqual(data["thread_id"], t_id)

    def test_post_messages_409_when_busy(self) -> None:
        _, created = self._request("POST", "/api/threads", {"title": "Busy API Thread"})
        t_id = created["id"]

        # Simulate busy task in memory
        task = self.server.manager.submit("Crawl https://example.com", Intent(task_type="website_crawl", url="https://example.com", max_pages=1), thread_id=t_id)
        with self.server.manager._lock:
            task.status = "running"
            self.server.manager._tasks[task.id] = task

        status, data = self._request("POST", f"/api/threads/{t_id}/messages", {"prompt": "Followup while busy"})
        self.assertEqual(status, 409)
        self.assertIn("Wait for the current answer to finish", data["error"])

    def test_post_messages_invalid_id_and_not_found(self) -> None:
        status400, _ = self._request("POST", "/api/threads/bad-id/messages", {"prompt": "Hello"})
        self.assertEqual(status400, 400)

        status404, _ = self._request("POST", f"/api/threads/{uuid.uuid4().hex}/messages", {"prompt": "Hello"})
        self.assertEqual(status404, 404)

    def test_body_too_large_413(self) -> None:
        _, created = self._request("POST", "/api/threads", {})
        t_id = created["id"]

        huge_payload = b"X" * 10000
        status, data = self._request("POST", f"/api/threads/{t_id}/messages", raw_body=huge_payload)
        self.assertEqual(status, 413)

    def test_thread_limit_429(self) -> None:
        # Test capacity check when count reaches 200
        with mock.patch.object(self.server.manager.db, "count_threads", return_value=200):
            status, data = self._request("POST", "/api/threads", {"title": "Exceed Thread"})
            self.assertEqual(status, 429)
            self.assertIn("Thread limit reached", data["error"])

    def test_static_assets_serve_threads_ui(self) -> None:
        """Verify frontend assets are served correctly with thread UI elements."""
        req = urllib.request.Request(f"{self.base_url}/")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            html = resp.read().decode("utf-8")
            self.assertIn('id="threadSidebar"', html)
            self.assertIn('id="threadsToggle"', html)
            self.assertIn('id="newThreadBtn"', html)
            self.assertIn('id="threadsList"', html)

        req_js = urllib.request.Request(f"{self.base_url}/static/app.js")
        with urllib.request.urlopen(req_js) as resp_js:
            self.assertEqual(resp_js.status, 200)
            js = resp_js.read().decode("utf-8")
            self.assertIn("/api/threads", js)
            self.assertIn("selectThread", js)
            self.assertIn("submitMessage", js)
            self.assertNotIn(".innerHTML", js)

        req_css = urllib.request.Request(f"{self.base_url}/static/style.css")
        with urllib.request.urlopen(req_css) as resp_css:
            self.assertEqual(resp_css.status, 200)
            css = resp_css.read().decode("utf-8")
            self.assertIn(".thread-sidebar", css)
            self.assertIn(".stage-item.skipped", css)
            self.assertIn(".followup-honesty-tag", css)

    def test_backward_compatibility_with_api_tasks(self) -> None:
        """Verify legacy /api/tasks POST and GET operate seamlessly alongside threads."""
        status, data = self._request("POST", "/api/tasks", {
            "prompt": "Crawl https://example.com and summarize it",
        })
        self.assertEqual(status, 202)
        task_id = data["id"]
        self.assertIn("plan", data)

        status_get, data_get = self._request("GET", f"/api/tasks/{task_id}")
        self.assertEqual(status_get, 200)
        self.assertEqual(data_get["id"], task_id)



if __name__ == "__main__":
    unittest.main()
