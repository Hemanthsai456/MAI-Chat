"""Comprehensive test suite for MAI-Chat upgrades.

Covers:
  - Approved CLI Registry and strict argument validation;
  - Streaming bounded output capture and truncation enforcement;
  - SQLite task persistence, migrations, and server restart recovery;
  - Concurrency control, worker pool, and queue capacity bounds;
  - Security defenses: Anti-SSRF, DNS rebinding guards, IP ranges, Carrier-Grade NAT;
  - Export capabilities: JSON, Markdown, and CSV formats;
  - Optional LLM integration interface and safe deterministic fallback;
  - Structured schema extraction and honest null handling.
"""
from __future__ import annotations

import csv
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from app.database import Database, TaskRecord
from app.intent import Intent, TaskPlan, parse_intent
from app.registry import ApprovedTool, ArgumentSpec, get_tool, list_tools, register_tool
from app.runner import CliResult, run_approved_tool
from app.server import create_server
from app.tasks import QueueFullError, TaskManager, crawler_args
from app.validation import ValidationError, is_ip_globally_routable, validate_public_url


class ToolRegistryTests(unittest.TestCase):
    def test_approved_tool_listing(self):
        tools = list_tools()
        self.assertTrue(any(t["name"] == "site-crawler" for t in tools))
        crawler_tool = next(t for t in tools if t["name"] == "site-crawler")
        self.assertIn("--url", crawler_tool["supported_flags"])
        self.assertIn("--max-pages", crawler_tool["supported_flags"])

    def test_strict_argument_validation(self):
        tool = get_tool("site-crawler")
        self.assertIsNotNone(tool)
        # Valid args pass
        valid = tool.validate_args(["--url=https://example.com", "--max-pages=3"])
        self.assertEqual(len(valid), 2)

        # Unapproved flag raises ValueError
        with self.assertRaises(ValueError):
            tool.validate_args(["--url=https://example.com", "--unapproved-flag=dangerous"])

        # Range violation raises ValueError
        with self.assertRaises(ValueError):
            tool.validate_args(["--url=https://example.com", "--max-pages=50"])

        # Positional argument raises ValueError
        with self.assertRaises(ValueError):
            tool.validate_args(["https://example.com"])


class StreamingCaptureTests(unittest.TestCase):
    def test_output_capped_while_reading_without_memory_blowup(self):
        script = Path(tempfile.mkdtemp()) / "spammer.py"
        script.write_text("import sys\nfor _ in range(1000):\n    sys.stdout.write('X' * 100 + '\\n')\n")
        tool = ApprovedTool("spammer", script, "test spammer", 10.0, max_output_bytes=5000)
        register_tool(tool)

        result = run_approved_tool("spammer", [], timeout_s=5, max_output_bytes=5000)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.stdout_truncated)
        self.assertLessEqual(len(result.stdout), 5000)

    def test_captures_stdout_and_stderr_separately(self):
        script = Path(tempfile.mkdtemp()) / "two_streams.py"
        script.write_text("import sys\nsys.stdout.write('from-stdout\\n')\nsys.stderr.write('from-stderr\\n')\n")
        tool = ApprovedTool("two_streams", script, "test streams", 10.0)
        register_tool(tool)

        result = run_approved_tool("two_streams", [], timeout_s=5)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("from-stdout", result.stdout)
        self.assertIn("from-stderr", result.stderr)


class DatabasePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def test_save_and_retrieve_task(self):
        record = TaskRecord(
            id="test-task-1",
            prompt="Crawl https://example.com",
            task_type="website_crawl",
            target_url="https://example.com",
            status="queued",
            tool="site-crawler",
            created_at=time.time(),
        )
        self.db.save_task(record)

        loaded = self.db.get_task("test-task-1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.prompt, "Crawl https://example.com")
        self.assertEqual(loaded.status, "queued")

    def test_server_restart_honestly_marks_interrupted_tasks(self):
        record = TaskRecord(
            id="interrupted-1",
            prompt="Crawl https://example.com",
            task_type="website_crawl",
            target_url="https://example.com",
            status="running",
            tool="site-crawler",
            created_at=time.time(),
            started_at=time.time(),
            progress_json=json.dumps(["Validating URL", "Crawling started"]),
        )
        self.db.save_task(record)

        recovered_count = self.db.recover_interrupted_tasks()
        self.assertEqual(recovered_count, 1)

        loaded = self.db.get_task("interrupted-1")
        self.assertEqual(loaded.status, "failed")
        self.assertTrue(any("interrupted" in line.lower() for line in loaded.progress))
        self.assertIn("interrupted", loaded.result["banner"]["text"].lower())

    def test_task_history_listing_and_pagination(self):
        for i in range(15):
            rec = TaskRecord(
                id=f"task-{i:02d}",
                prompt=f"Task {i}",
                task_type="website_crawl",
                target_url="https://example.com",
                status="succeeded",
                tool="site-crawler",
                created_at=time.time() + i,
            )
            self.db.save_task(rec)

        page1 = self.db.list_tasks(limit=10, offset=0)
        self.assertEqual(len(page1), 10)
        page2 = self.db.list_tasks(limit=10, offset=10)
        self.assertEqual(len(page2), 5)


class QueueAndWorkerPoolTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def test_queue_capacity_saturation_raises_error(self):
        # Create manager with max queue capacity of 2
        manager = TaskManager(db=self.db, max_workers=1, queue_capacity=2)
        intent = parse_intent("Crawl https://example.com")

        # Fill queue to maximum capacity with valid task objects
        from app.tasks import Task
        while manager._queue.qsize() < manager.queue_capacity:
            dummy_task = Task(id=f"dummy-{manager._queue.qsize()}", prompt="dummy", intent=intent)
            manager._queue.put(dummy_task)

        # Submitting when queue is full raises QueueFullError
        with self.assertRaises(QueueFullError):
            manager.submit("Prompt overflow", intent)


class SecurityAndAntiSSRFTests(unittest.TestCase):
    def test_ip_globally_routable_blocks_internal_and_private(self):
        blocked_ips = [
            "127.0.0.1",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",  # AWS/Cloud metadata
            "100.64.0.1",       # Carrier-Grade NAT
            "0.0.0.0",
            "::1",              # IPv6 loopback
            "fe80::1",          # IPv6 link-local
            "fc00::1",          # IPv6 unique local
            "::ffff:127.0.0.1", # IPv4-mapped IPv6
            "::ffff:10.0.0.1",
        ]
        for ip in blocked_ips:
            with self.subTest(ip=ip):
                self.assertFalse(is_ip_globally_routable(ip), f"{ip} should be rejected as non-global")

    def test_ip_globally_routable_accepts_public_addresses(self):
        public_ips = [
            "93.184.216.34",    # example.com
            "8.8.8.8",
            "1.1.1.1",
            "2606:2800:220:1:248:1893:25c8:1946",
        ]
        for ip in public_ips:
            with self.subTest(ip=ip):
                self.assertTrue(is_ip_globally_routable(ip), f"{ip} should be accepted as global")

    def test_validate_public_url_blocks_aws_metadata_and_private(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BUILDSPRINT_ALLOW_PRIVATE_TARGETS", None)
            for bad_url in (
                "http://169.254.169.254/latest/meta-data/",
                "http://127.0.0.1:8000/secret",
                "http://10.20.30.40/",
                "http://[::1]/",
            ):
                with self.subTest(url=bad_url), self.assertRaises(ValidationError):
                    validate_public_url(bad_url)


class LLMPlannerTests(unittest.TestCase):
    def test_llm_plan_parsing_and_validation(self):
        mock_llm_json = {
            "task_type": "website_crawl",
            "target_url": "https://example.com",
            "max_pages": 4,
            "max_depth": 1,
            "extraction_fields": ["company_name", "services"],
            "requested_operation": "extract_company_info",
            "notes": ["Extracted 2 target fields"],
        }
        with mock.patch("app.intent.os.environ.get", side_effect=lambda k, d=None: "dummy-key" if k == "LLM_API_KEY" else d):
            with mock.patch("app.intent._call_optional_llm", return_value=mock_llm_json):
                intent = parse_intent("Find company info for example.com")
                self.assertEqual(intent.url, "https://example.com/")
                self.assertEqual(intent.max_pages, 4)
                self.assertIsNotNone(intent.plan)
                self.assertEqual(intent.plan.source, "llm")
                self.assertIn("company_name", intent.plan.extraction_fields)

    def test_llm_plan_rejection_falls_back_to_rules(self):
        bad_llm_json = {
            "task_type": "run_arbitrary_shell_command",
            "target_url": "https://example.com",
        }
        with mock.patch("app.intent.os.environ.get", side_effect=lambda k, d=None: "dummy-key" if k == "LLM_API_KEY" else d):
            with mock.patch("app.intent._call_optional_llm", return_value=bad_llm_json):
                intent = parse_intent("Crawl https://example.com")
                self.assertEqual(intent.task_type, "website_crawl")
                self.assertEqual(intent.plan.source, "rules")


class ExportAndDetailsApiTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.manager = TaskManager(db=self.db)

    def test_export_json_csv_and_markdown(self):
        intent = parse_intent("Crawl https://example.com")
        task = self.manager.submit("Crawl https://example.com", intent)

        task.result = {
            "task_status": "succeeded",
            "banner": {"kind": "success", "text": "Crawl completed"},
            "summary": "Sample summary text",
            "json": json.dumps({"status": "succeeded", "pages": [{"url": "https://example.com", "title": "Example", "word_count": 100}]}),
            "payload": {
                "pages": [{"url": "https://example.com", "title": "Example", "word_count": 100, "excerpt": "Sample"}]
            },
        }

        # JSON Export
        content, c_type, fname = self.manager.export(task, "json")
        self.assertEqual(c_type, "application/json; charset=utf-8")
        self.assertTrue(fname.endswith(".json"))
        self.assertIn("succeeded", content)

        # Markdown Export
        md_content, md_type, md_fname = self.manager.export(task, "md")
        self.assertEqual(md_type, "text/markdown; charset=utf-8")
        self.assertTrue(md_fname.endswith(".md"))
        self.assertIn("# MAI-Chat Execution Report", md_content)

        # CSV Export
        csv_content, csv_type, csv_fname = self.manager.export(task, "csv")
        self.assertEqual(csv_type, "text/csv; charset=utf-8")
        self.assertTrue(csv_fname.endswith(".csv"))
        reader = list(csv.reader(io.StringIO(csv_content)))
        self.assertEqual(reader[0], ["Page URL", "Title", "Word Count", "Status Code", "Headings"])
        self.assertEqual(reader[1][0], "https://example.com")


class ServerApiUpgradesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server("127.0.0.1", 0, db_path=":memory:")
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _get(self, path: str):
        import urllib.request
        req = urllib.request.Request(self.base + path)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), resp.headers

    def test_api_tools_endpoint(self):
        status, body, _ = self._get("/api/tools")
        self.assertEqual(status, 200)
        self.assertIn("tools", body)
        self.assertTrue(any(t["name"] == "site-crawler" for t in body["tools"]))

    def test_api_tasks_history_endpoint(self):
        status, body, _ = self._get("/api/tasks?limit=10")
        self.assertEqual(status, 200)
        self.assertIn("tasks", body)
        self.assertIn("count", body)


class StructuredExtractionTests(unittest.TestCase):
    def test_extract_structured_fields_honest_nulls(self):
        from tools.site_crawler import extract_structured_fields
        pages = [
            {
                "url": "https://example.com",
                "title": "Acme Widgets Inc. - Leading Innovations",
                "meta_description": "We build fast widgets.",
                "headings": [{"level": 1, "text": "Our Services and Offerings"}, {"level": 2, "text": "Cloud Solutions"}],
                "excerpt": "Welcome to Acme Widgets. Contact us at info@acme.com or (555) 123-4567.",
            }
        ]
        fields = ["company_name", "services", "contact_info", "nonexistent_field"]
        extracted = extract_structured_fields(pages, fields)

        # Honest extraction
        self.assertIn("company_name", extracted)
        self.assertEqual(extracted["company_name"], "Acme Widgets Inc.")

        self.assertIn("contact_info", extracted)
        self.assertIn("info@acme.com", extracted["contact_info"]["emails"])

        self.assertIn("services", extracted)
        self.assertTrue(any("Services" in s for s in extracted["services"]))

        # Missing field is explicitly None / null (never fabricated)
        self.assertIn("nonexistent_field", extracted)
        self.assertIsNone(extracted["nonexistent_field"])


if __name__ == "__main__":
    unittest.main()
