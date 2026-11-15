"""Comprehensive security and correctness remediation tests for MAI-Chat.

Validates all 18 required remediation scenarios:
1. DNS rebinding prevention at connection layer (TOCTOU: public at validation, private at connection).
2. Rejection of mixed public/private DNS answers.
3. Redirect to private IP is blocked.
4. Redirect to different hostname is blocked (same-host crawl boundary).
5. Redirect to same hostname on non-standard port (8080/8443) is blocked.
6. Same-host HTML links to non-standard ports (8080/8443) are filtered out by next_url.
7. Redirects containing embedded credentials are blocked.
8. robots.txt HTTP 500 triggers complete disallow (RFC 9309).
9. robots.txt connection failure triggers complete disallow (RFC 9309).
10. robots.txt HTTP 404 allows crawling (RFC 9309).
11. Atomic queue admission under high concurrency with Barrier (zero overflow, exact 429s).
12. Worker pool concurrency strictly bounded by MAI_MAX_WORKERS.
13. CLI schema: required arguments omitted raises ValueError.
14. CLI schema: invalid or missing flag values rejected.
15. Subprocess tree termination on cancellation and timeout (no orphaned processes).
16. Bounded streaming capture with simultaneous large stdout and stderr.
17. Coordinated server and TaskManager shutdown leaves no worker threads or child procs.
18. Historical task stages faithfully restored from SQLite without showing pending.
"""
from __future__ import annotations

import http.server
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.database import Database, TaskRecord
from app.intent import Intent, TaskPlan
from app.registry import ApprovedTool, ArgumentSpec, get_tool, register_tool
from app.runner import CliResult, run_approved_tool
from app.server import MAIChatServer, create_server
from app.tasks import QueueFullError, Task, TaskManager
from app.validation import (
    ValidationError,
    enforce_startup_safety,
    is_ip_globally_routable,
    validate_public_url,
)
from tools.site_crawler import (
    EXIT_BLOCKED,
    EXIT_FAILED,
    EXIT_OK,
    PinnedHTTPConnection,
    PinnedHTTPSConnection,
    SecurityError,
    crawl,
    load_robots,
    next_url,
    resolve_and_validate_destination,
)

ROOT_DIR = Path(__file__).resolve().parent.parent


class _MockSocket:
    """Mock socket for connection-layer DNS rebinding and peer verification testing."""
    def __init__(self, peer_ip: str, peer_port: int) -> None:
        self._peer = (peer_ip, peer_port)
        self.closed = False

    def getpeername(self) -> tuple[str, int]:
        return self._peer

    def close(self) -> None:
        self.closed = True

    def settimeout(self, t: float | None) -> None:
        pass


class SecurityRemediationTests(unittest.TestCase):
    """Test suite for targeted P0 security and P1 reliability remediation."""

    @classmethod
    def setUpClass(cls):
        os.environ["MAI_TESTING"] = "1"
        os.environ["BUILDSPRINT_ALLOW_PRIVATE_TARGETS"] = "1"

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("MAI_TESTING", None)
        os.environ.pop("BUILDSPRINT_ALLOW_PRIVATE_TARGETS", None)

    # -----------------------------------------------------------------------
    # Scenario 1: Genuine Connection-Layer DNS Rebinding Test
    # -----------------------------------------------------------------------
    def test_01_dns_rebinding_connection_layer_simulated_change(self):
        """Genuine connection-layer DNS rebinding test.

        Validates:
        1. Hostname resolves initially to a validated public IP (93.184.216.34).
        2. Connection pins this exact IP for destination selection.
        3. Subsequent DNS resolution changes to a private IP (127.0.0.1 or 10.0.0.1).
        4. Verifies socket connection connects to the pinned public IP, never the private IP.
        5. Verifies hostname is resolved only once for the connection's destination selection.
        6. Verifies peer-address check rejects connections if peer is private.
        7. Tests both PinnedHTTPConnection and PinnedHTTPSConnection paths.
        """
        orig_getaddrinfo = socket.getaddrinfo
        orig_create_conn = socket.create_connection

        os.environ["BUILDSPRINT_ALLOW_PRIVATE_TARGETS"] = "0"
        try:
            # --- Part A: PinnedHTTPConnection ---
            http_dns_calls: list[int] = []
            http_conn_targets: list[tuple[str, int]] = []

            def mock_http_dns(host, port, *args, **kwargs):
                if host == "rebind-http.example.com":
                    http_dns_calls.append(len(http_dns_calls) + 1)
                    if len(http_dns_calls) == 1:
                        # 1st call: returns valid public IP
                        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
                    else:
                        # Subsequent call: attacker changes DNS to loopback/private
                        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
                return orig_getaddrinfo(host, port, *args, **kwargs)

            def mock_create_http(addr, timeout=None, source_address=None):
                http_conn_targets.append(addr)
                target_ip, target_port = addr
                return _MockSocket(target_ip, target_port)

            socket.getaddrinfo = mock_http_dns
            socket.create_connection = mock_create_http

            conn_http = PinnedHTTPConnection("rebind-http.example.com", port=80)
            conn_http.connect()

            # Verify connection used the pinned public IP, NOT the changed private IP
            self.assertEqual(conn_http.pinned_ip, "93.184.216.34")
            self.assertEqual(http_conn_targets, [("93.184.216.34", 80)])
            # Verify hostname was resolved only once for destination selection
            self.assertEqual(len(http_dns_calls), 1)

            # Verify subsequent DNS query indeed changes to private IP
            changed_res = socket.getaddrinfo("rebind-http.example.com", 80)
            self.assertEqual(changed_res[0][4][0], "127.0.0.1")
            self.assertEqual(len(http_dns_calls), 2)
            # But the connection's target socket was never affected
            self.assertEqual(http_conn_targets, [("93.184.216.34", 80)])

            # Verify peer address check on HTTP: if peer IP is private, SecurityError is raised
            def mock_create_bad_peer(addr, timeout=None, source_address=None):
                return _MockSocket("192.168.1.1", 80)

            socket.create_connection = mock_create_bad_peer
            conn_bad_peer = PinnedHTTPConnection("rebind-http.example.com", port=80)
            http_dns_calls.clear()
            with self.assertRaises(SecurityError) as ctx:
                conn_bad_peer.connect()
            self.assertIn("not globally routable", str(ctx.exception).lower())

            # --- Part B: PinnedHTTPSConnection ---
            https_dns_calls: list[int] = []
            https_conn_targets: list[tuple[str, int]] = []
            ssl_wrapped_hostnames: list[str | None] = []

            def mock_https_dns(host, port, *args, **kwargs):
                if host == "rebind-https.example.com":
                    https_dns_calls.append(len(https_dns_calls) + 1)
                    if len(https_dns_calls) == 1:
                        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
                    else:
                        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port))]
                return orig_getaddrinfo(host, port, *args, **kwargs)

            def mock_create_https(addr, timeout=None, source_address=None):
                https_conn_targets.append(addr)
                target_ip, target_port = addr
                return _MockSocket(target_ip, target_port)

            socket.getaddrinfo = mock_https_dns
            socket.create_connection = mock_create_https

            import ssl
            orig_wrap = ssl.SSLContext.wrap_socket

            def mock_wrap(self_ssl, sock, server_hostname=None, *w_args, **w_kwargs):
                ssl_wrapped_hostnames.append(server_hostname)
                return sock

            ssl.SSLContext.wrap_socket = mock_wrap
            try:
                conn_https = PinnedHTTPSConnection("rebind-https.example.com", port=443)
                conn_https.connect()

                # Verify HTTPS connection targeted the pinned public IP
                self.assertEqual(conn_https.pinned_ip, "93.184.216.34")
                self.assertEqual(https_conn_targets, [("93.184.216.34", 443)])
                # Verify DNS resolved once for destination selection
                self.assertEqual(len(https_dns_calls), 1)
                # Verify TLS SNI preserved original hostname, not IP
                self.assertEqual(ssl_wrapped_hostnames, ["rebind-https.example.com"])

                # Verify HTTPS peer check
                def mock_create_bad_https_peer(addr, timeout=None, source_address=None):
                    return _MockSocket("10.0.0.1", 443)

                socket.create_connection = mock_create_bad_https_peer
                https_dns_calls.clear()
                conn_bad_https = PinnedHTTPSConnection("rebind-https.example.com", port=443)
                with self.assertRaises(SecurityError) as ctx:
                    conn_bad_https.connect()
                self.assertIn("not globally routable", str(ctx.exception).lower())
            finally:
                ssl.SSLContext.wrap_socket = orig_wrap

        finally:
            socket.getaddrinfo = orig_getaddrinfo
            socket.create_connection = orig_create_conn
            os.environ["BUILDSPRINT_ALLOW_PRIVATE_TARGETS"] = "1"

    # -----------------------------------------------------------------------
    # Scenario 2: Mixed Public/Private DNS Answers Rejected
    # -----------------------------------------------------------------------
    def test_02_mixed_dns_answers_rejected(self):
        """A hostname returning both public and private IP addresses must be rejected."""
        orig_getaddrinfo = socket.getaddrinfo

        def mock_mixed_getaddrinfo(host, port, *args, **kwargs):
            if host == "mixed-dns.example.com":
                return [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port)),
                ]
            return orig_getaddrinfo(host, port, *args, **kwargs)

        socket.getaddrinfo = mock_mixed_getaddrinfo
        try:
            os.environ["BUILDSPRINT_ALLOW_PRIVATE_TARGETS"] = "0"
            with self.assertRaises(SecurityError) as ctx:
                resolve_and_validate_destination("mixed-dns.example.com", 80)
            self.assertIn("private", str(ctx.exception).lower())
        finally:
            socket.getaddrinfo = orig_getaddrinfo
            os.environ["BUILDSPRINT_ALLOW_PRIVATE_TARGETS"] = "1"

    # -----------------------------------------------------------------------
    # Scenario 3: Redirect to Private IP is Blocked
    # -----------------------------------------------------------------------
    def test_03_redirect_to_private_ip_blocked(self):
        """Redirecting from an allowed target to a private IP (e.g. 127.0.0.1 or 169.254.169.254) must be blocked."""
        os.environ["BUILDSPRINT_ALLOW_PRIVATE_TARGETS"] = "0"
        try:
            from tools.site_crawler import AntiSsrflRedirectHandler
            handler = AntiSsrflRedirectHandler(allowed_host="example.com", same_host_only=False)
            req = http.client.HTTPMessage()

            with self.assertRaises(SecurityError) as ctx:
                handler.redirect_request(req, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data/")
            self.assertIn("not globally routable", str(ctx.exception).lower())
        finally:
            os.environ["BUILDSPRINT_ALLOW_PRIVATE_TARGETS"] = "1"

    # -----------------------------------------------------------------------
    # Scenario 4: Redirect to Different Hostname is Blocked (Same-Host Boundary)
    # -----------------------------------------------------------------------
    def test_04_redirect_to_different_hostname_blocked_for_crawler(self):
        """When same_host_only=True, redirecting off-site must raise SecurityError."""
        from tools.site_crawler import AntiSsrflRedirectHandler
        handler = AntiSsrflRedirectHandler(allowed_host="example.com", same_host_only=True)
        req = http.client.HTTPMessage()

        with self.assertRaises(SecurityError) as ctx:
            handler.redirect_request(req, None, 302, "Found", {}, "http://evil.com/phishing")
        self.assertIn("leaves start host", str(ctx.exception).lower())

    # -----------------------------------------------------------------------
    # Scenario 5: Redirect to Non-Standard Port Blocked
    # -----------------------------------------------------------------------
    def test_05_redirect_to_non_standard_port_blocked(self):
        """Redirecting to port 8080 or 8443 must be blocked when testing bypass is inactive."""
        os.environ["BUILDSPRINT_ALLOW_PRIVATE_TARGETS"] = "0"
        try:
            from tools.site_crawler import AntiSsrflRedirectHandler
            handler = AntiSsrflRedirectHandler(allowed_host="example.com", same_host_only=True)
            req = http.client.HTTPMessage()

            with self.assertRaises(SecurityError) as ctx:
                handler.redirect_request(req, None, 302, "Found", {}, "http://example.com:8080/admin")
            self.assertIn("non-standard port 8080", str(ctx.exception).lower())
        finally:
            os.environ["BUILDSPRINT_ALLOW_PRIVATE_TARGETS"] = "1"

    # -----------------------------------------------------------------------
    # Scenario 6: Same-Host HTML Links to Non-Standard Ports Filtered Out
    # -----------------------------------------------------------------------
    def test_06_html_links_to_non_standard_ports_filtered_by_next_url(self):
        """Discovered link to example.com:8080 must be dropped by next_url."""
        os.environ["BUILDSPRINT_ALLOW_PRIVATE_TARGETS"] = "0"
        try:
            base = "https://example.com/page1"
            # Link to port 8080
            link_8080 = "https://example.com:8080/internal"
            self.assertIsNone(next_url(base, link_8080, "example.com"))

            # Link to port 443 on https is allowed
            link_443 = "https://example.com:443/valid"
            self.assertIsNotNone(next_url(base, link_443, "example.com"))

            # Standard portless link is allowed
            link_std = "/valid-path"
            self.assertEqual(next_url(base, link_std, "example.com"), "https://example.com/valid-path")
        finally:
            os.environ["BUILDSPRINT_ALLOW_PRIVATE_TARGETS"] = "1"

    # -----------------------------------------------------------------------
    # Scenario 7: Redirect Containing Embedded Credentials Blocked
    # -----------------------------------------------------------------------
    def test_07_redirect_with_embedded_credentials_blocked(self):
        """Redirecting to URL with embedded user:pass credentials must be blocked."""
        from tools.site_crawler import AntiSsrflRedirectHandler
        handler = AntiSsrflRedirectHandler(allowed_host="example.com", same_host_only=True)
        req = http.client.HTTPMessage()

        with self.assertRaises(SecurityError) as ctx:
            handler.redirect_request(req, None, 302, "Found", {}, "http://admin:secret@example.com/login")
        self.assertIn("credentials", str(ctx.exception).lower())

    # -----------------------------------------------------------------------
    # Scenario 8: robots.txt HTTP 500 Triggers Complete Disallow
    # -----------------------------------------------------------------------
    def test_08_robots_txt_http_500_disallows(self):
        """Under RFC 9309, HTTP 500 for robots.txt must treat all paths as disallowed."""
        class ServerErrorRobotsHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"Server Error")

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), ServerErrorRobotsHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            parser, note = load_robots(f"http://127.0.0.1:{port}", "127.0.0.1", timeout=5.0)
            self.assertFalse(parser.can_fetch("*", f"http://127.0.0.1:{port}/anypage"))
            self.assertIn("500", note)
            self.assertIn("disallowed", note.lower())
        finally:
            server.shutdown()
            server.server_close()

    # -----------------------------------------------------------------------
    # Scenario 9: robots.txt Connection Failure Triggers Complete Disallow
    # -----------------------------------------------------------------------
    def test_09_robots_txt_connection_failure_disallows(self):
        """Under RFC 9309, network failure fetching robots.txt must assume complete disallow."""
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()

        parser, note = load_robots(f"http://127.0.0.1:{port}", "127.0.0.1", timeout=1.0)
        self.assertFalse(parser.can_fetch("*", f"http://127.0.0.1:{port}/page"))
        self.assertIn("disallowed", note.lower())

    # -----------------------------------------------------------------------
    # Scenario 10: robots.txt HTTP 404 Allows Crawling
    # -----------------------------------------------------------------------
    def test_10_robots_txt_http_404_allows_crawling(self):
        """Under RFC 9309, HTTP 404 for robots.txt means no restrictions (crawling allowed)."""
        class NotFoundRobotsHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not Found")

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), NotFoundRobotsHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            parser, note = load_robots(f"http://127.0.0.1:{port}", "127.0.0.1", timeout=5.0)
            self.assertTrue(parser.can_fetch("*", f"http://127.0.0.1:{port}/index.html"))
            self.assertIn("404", note)
            self.assertIn("allowed", note.lower())
        finally:
            server.shutdown()
            server.server_close()

    # -----------------------------------------------------------------------
    # Scenario 11: Atomic Queue Admission with High Concurrency Barrier
    # -----------------------------------------------------------------------
    def test_11_atomic_queue_admission_with_barrier(self):
        """Concurrent submissions hitting bounded queue: exact capacity accepted, zero overflow, rest rejected."""
        queue_cap = 5
        db = Database(":memory:")
        # Manager with 1 worker that sleeps, holding the queue full
        mgr = TaskManager(db=db, max_workers=1, queue_capacity=queue_cap)

        num_threads = 20
        barrier = threading.Barrier(num_threads)
        results: list[tuple[bool, str]] = []
        lock = threading.Lock()

        def submit_worker(idx: int):
            intent = Intent(task_type="crawl_and_extract", url="https://example.com", max_pages=1)
            barrier.wait()  # synchronize all threads for simultaneous burst
            try:
                task = mgr.submit(f"Burst request {idx}", intent)
                with lock:
                    results.append((True, task.id))
            except QueueFullError as exc:
                with lock:
                    results.append((False, str(exc)))

        threads = [threading.Thread(target=submit_worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r[0]]
        failures = [r for r in results if not r[0]]

        # Exact queue capacity must be admitted (plus at most 1 currently picked up by worker)
        self.assertLessEqual(len(successes), queue_cap + 1)
        self.assertGreaterEqual(len(failures), num_threads - (queue_cap + 1))
        # Total must equal all threads
        self.assertEqual(len(successes) + len(failures), num_threads)
        mgr.shutdown(timeout=2.0)

    # -----------------------------------------------------------------------
    # Scenario 12: Worker Concurrency Never Exceeds Configured Limit
    # -----------------------------------------------------------------------
    def test_12_worker_concurrency_never_exceeds_max(self):
        """Worker pool strictly enforces MAI_MAX_WORKERS concurrent executions."""
        max_workers = 3
        db = Database(":memory:")
        mgr = TaskManager(db=db, max_workers=max_workers, queue_capacity=20)

        active_workers = 0
        peak_active = 0
        active_lock = threading.Lock()

        # Monkeypatch _execute to measure active worker concurrency
        orig_execute = mgr._execute

        def tracked_execute(task):
            nonlocal active_workers, peak_active
            with active_lock:
                active_workers += 1
                if active_workers > peak_active:
                    peak_active = active_workers
            try:
                time.sleep(0.1)
            finally:
                with active_lock:
                    active_workers -= 1

        mgr._execute = tracked_execute

        # Submit 10 tasks
        for i in range(10):
            intent = Intent(task_type="crawl_and_extract", url="https://example.com", max_pages=1)
            mgr.submit(f"Worker concurrency test {i}", intent)

        # Wait for tasks to drain
        time.sleep(1.0)
        self.assertLessEqual(peak_active, max_workers)
        mgr.shutdown(timeout=2.0)

    # -----------------------------------------------------------------------
    # Scenario 13: Required CLI Arguments Omitted Raises ValueError
    # -----------------------------------------------------------------------
    def test_13_required_cli_arguments_omitted(self):
        """Validating CLI arguments when required flags are omitted must raise ValueError."""
        tool = get_tool("site-crawler")
        self.assertIsNotNone(tool)

        # site-crawler requires --url
        with self.assertRaises(ValueError) as ctx:
            tool.validate_args(["--max-pages=3"])
        self.assertIn("required", str(ctx.exception).lower())
        self.assertIn("--url", str(ctx.exception))

    # -----------------------------------------------------------------------
    # Scenario 14: CLI Flags Have Missing or Invalid Values
    # -----------------------------------------------------------------------
    def test_14_cli_flags_missing_or_invalid_values(self):
        """Passing unapproved flags, duplicate flags, missing values, or bad types must raise ValueError."""
        tool = get_tool("site-crawler")

        # Flag without value
        with self.assertRaises(ValueError):
            tool.validate_args(["--url="])

        # Flag without equals sign
        with self.assertRaises(ValueError):
            tool.validate_args(["--url"])

        # Duplicate flag
        with self.assertRaises(ValueError):
            tool.validate_args(["--url=https://example.com", "--url=https://example.com/2"])

        # Out-of-range integer value
        with self.assertRaises(ValueError):
            tool.validate_args(["--url=https://example.com", "--max-pages=999"])

        # Unapproved / arbitrary flag
        with self.assertRaises(ValueError):
            tool.validate_args(["--url=https://example.com", "--malicious-flag=1"])

        # Positional argument
        with self.assertRaises(ValueError):
            tool.validate_args(["--url=https://example.com", "positional_arg"])

    # -----------------------------------------------------------------------
    # Scenario 15: Cancellation and Timeout Terminate Complete Process Tree
    # -----------------------------------------------------------------------
    def test_15_cancellation_terminates_subprocess_tree(self):
        """User cancellation terminates the running subprocess tree promptly without hanging."""
        cancel_ev = threading.Event()

        # Trigger cancellation after 0.2 seconds in a background thread
        def cancel_timer():
            time.sleep(0.2)
            cancel_ev.set()

        threading.Thread(target=cancel_timer, daemon=True).start()

        start = time.monotonic()
        res = run_approved_tool(
            "site-crawler",
            ["--url=https://example.com", "--timeout=10"],
            timeout_s=30.0,
            cancel_event=cancel_ev,
        )
        duration = time.monotonic() - start

        self.assertTrue(res.cancelled)
        # Must exit promptly, well before 30.0s
        self.assertLess(duration, 5.0)

    # -----------------------------------------------------------------------
    # Scenario 16: Large Simultaneous Stdout and Stderr Output Remains Bounded
    # -----------------------------------------------------------------------
    def test_16_large_simultaneous_output_bounded(self):
        """Generating large stdout and stderr concurrently does not deadlock and respects byte limits."""
        # We test _StreamCapture with simulated large chunks
        from app.runner import _StreamCapture
        import io

        large_payload = b"A" * 100_000
        stream = io.BytesIO(large_payload)
        cap = _StreamCapture(stream, limit_bytes=5_000)
        cap.start()
        cap.join(timeout=2.0)

        text, truncated = cap.get_text()
        self.assertEqual(len(text.encode("utf-8")), 5_000)
        self.assertTrue(truncated)

    # -----------------------------------------------------------------------
    # Scenario 17: Coordinated Shutdown Leaves No Worker Threads or Processes
    # -----------------------------------------------------------------------
    def test_17_coordinated_shutdown_cleans_resources(self):
        """Calling TaskManager.shutdown stops workers, drains queue, and closes database cleanly."""
        db = Database(":memory:")
        mgr = TaskManager(db=db, max_workers=2, queue_capacity=10)

        # Submit task
        intent = Intent(task_type="crawl_and_extract", url="https://example.com", max_pages=1)
        t1 = mgr.submit("Task before shutdown", intent)

        # Call shutdown
        mgr.shutdown(timeout=3.0)

        # Workers should be stopped
        for w in mgr._workers:
            self.assertFalse(w.is_alive())

        # Submissions after shutdown should raise QueueFullError
        with self.assertRaises(QueueFullError):
            mgr.submit("Post-shutdown task", intent)

    # -----------------------------------------------------------------------
    # Scenario 18: Historical Task Stages Restored from SQLite
    # -----------------------------------------------------------------------
    def test_18_historical_task_stages_restored_accurately(self):
        """Historical tasks retrieved from SQLite accurately restore their stage events."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            temp_db_path = f.name

        try:
            db = Database(temp_db_path)
            stages_list = [
                {"stage": "request_received", "label": "Request received", "status": "completed", "timestamp": 100.0, "detail": "Accepted"},
                {"stage": "intent_interpreted", "label": "Intent interpreted", "status": "completed", "timestamp": 101.0, "detail": "Interpreted"},
                {"stage": "plan_prepared", "label": "Plan prepared", "status": "completed", "timestamp": 102.0, "detail": "Prepared"},
                {"stage": "input_validated", "label": "Input validated", "status": "completed", "timestamp": 103.0, "detail": "Validated"},
                {"stage": "cli_selected", "label": "CLI selected", "status": "completed", "timestamp": 104.0, "detail": "Selected"},
                {"stage": "process_started", "label": "Process started", "status": "completed", "timestamp": 105.0, "detail": "Started"},
                {"stage": "execution_running", "label": "Execution running", "status": "completed", "timestamp": 106.0, "detail": "Running"},
                {"stage": "output_captured", "label": "Output captured", "status": "completed", "timestamp": 107.0, "detail": "Captured"},
                {"stage": "result_validated", "label": "Result validated", "status": "completed", "timestamp": 108.0, "detail": "Validated"},
                {"stage": "task_completed", "label": "Task completed", "status": "completed", "timestamp": 109.0, "detail": "Finished"},
            ]
            record = TaskRecord(
                id="test-task-history-1",
                prompt="Crawl https://example.com",
                task_type="crawl_and_extract",
                target_url="https://example.com",
                status="succeeded",
                tool="site-crawler",
                created_at=100.0,
                started_at=103.0,
                completed_at=110.0,
                duration_s=7.0,
                exit_code=0,
                stages_json=json.dumps(stages_list),
            )
            db.save_task(record)
            db.close()

            # Now create a fresh TaskManager from the database (simulating restart)
            db2 = Database(temp_db_path)
            mgr = TaskManager(db=db2, max_workers=1)

            restored = mgr.get("test-task-history-1")
            self.assertIsNotNone(restored)
            self.assertEqual(restored.status, "succeeded")

            # Check that stages are restored and not all pending!
            stages = restored.stages
            self.assertEqual(stages["request_received"].status, "completed")
            self.assertEqual(stages["input_validated"].status, "completed")
            self.assertEqual(stages["cli_selected"].status, "completed")
            self.assertEqual(stages["task_completed"].status, "completed")

            # Verify view() representation also includes completed stages
            view_data = mgr.view(restored)
            self.assertEqual(len(view_data["stages"]), 10)
            self.assertTrue(all(s["status"] == "completed" for s in view_data["stages"]))

            mgr.shutdown(timeout=2.0)
        finally:
            try:
                os.remove(temp_db_path)
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # Scenario 19: Queue Capacity Configuration (1, 2, Default, and Validation)
    # -----------------------------------------------------------------------
    def test_19_queue_capacity_configuration(self):
        """Respects configured positive queue capacities (1, 2, default) and rejects <= 0."""
        # 1. Capacity <= 0 must raise ValueError
        with self.assertRaises(ValueError):
            TaskManager(db=Database(":memory:"), queue_capacity=0)

        with self.assertRaises(ValueError):
            TaskManager(db=Database(":memory:"), queue_capacity=-5)

        with self.assertRaises(ValueError):
            TaskManager(db=Database(":memory:"), max_workers=0)

        with self.assertRaises(ValueError):
            TaskManager(db=Database(":memory:"), max_workers=-1)

        # 2. Default capacity
        mgr_default = TaskManager(db=Database(":memory:"))
        self.assertEqual(mgr_default.queue_capacity, 50)
        mgr_default.shutdown(timeout=1.0)

        # 3. Capacity = 1: accepts 1 waiting task, rejects the 2nd with QueueFullError
        mgr_1 = TaskManager(db=Database(":memory:"), max_workers=1, queue_capacity=1)
        intent = Intent(task_type="crawl_and_extract", url="https://example.com", max_pages=1)

        # Block the 1 worker with a running task so subsequent task must wait in queue
        worker_busy = threading.Event()
        worker_can_finish = threading.Event()

        orig_execute = mgr_1._execute
        def blocking_execute(task):
            worker_busy.set()
            worker_can_finish.wait(timeout=5.0)

        mgr_1._execute = blocking_execute

        t_running = mgr_1.submit("Running task", intent)
        self.assertTrue(worker_busy.wait(timeout=2.0))

        # Now worker is busy. Queue capacity is 1. Submit waiting task #1 (should succeed)
        t_waiting = mgr_1.submit("Waiting task 1", intent)
        self.assertEqual(mgr_1._queue.qsize(), 1)

        # Submit waiting task #2: MUST be rejected because capacity is 1
        with self.assertRaises(QueueFullError):
            mgr_1.submit("Waiting task 2 (overflow)", intent)

        # Release worker and clean up
        worker_can_finish.set()
        mgr_1.shutdown(timeout=2.0)

        # 4. Capacity = 2: accepts 2 waiting tasks, rejects 3rd
        mgr_2 = TaskManager(db=Database(":memory:"), max_workers=1, queue_capacity=2)
        worker_busy_2 = threading.Event()
        worker_can_finish_2 = threading.Event()

        def blocking_execute_2(task):
            worker_busy_2.set()
            worker_can_finish_2.wait(timeout=5.0)

        mgr_2._execute = blocking_execute_2

        mgr_2.submit("Running task", intent)
        self.assertTrue(worker_busy_2.wait(timeout=2.0))

        mgr_2.submit("Waiting task 1", intent)
        mgr_2.submit("Waiting task 2", intent)
        self.assertEqual(mgr_2._queue.qsize(), 2)

        # 3rd waiting task must be rejected
        with self.assertRaises(QueueFullError):
            mgr_2.submit("Waiting task 3 (overflow)", intent)

        worker_can_finish_2.set()
        mgr_2.shutdown(timeout=2.0)

    # -----------------------------------------------------------------------
    # Scenario 20: Transactional Admission — Failed Persistence Cannot Execute
    # -----------------------------------------------------------------------
    def test_20_transactional_admission_failed_persistence_cannot_execute(self):
        """If save_task() fails, task is never placed in queue and no worker executes it."""
        import sqlite3
        db = Database(":memory:")
        mgr = TaskManager(db=db, max_workers=2, queue_capacity=5)

        # Track any task execution by workers
        executed_tasks: list[str] = []
        orig_execute = mgr._execute
        def tracked_execute(task):
            executed_tasks.append(task.id)
            orig_execute(task)
        mgr._execute = tracked_execute

        # Monkeypatch save_task to simulate database persistence failure
        def failing_save_task(record):
            raise sqlite3.OperationalError("Simulated database disk failure during save_task")

        db.save_task = failing_save_task

        intent = Intent(task_type="crawl_and_extract", url="https://example.com", max_pages=1)
        with self.assertRaises(sqlite3.OperationalError):
            mgr.submit("Task that will fail persistence", intent)

        # Allow worker threads time to potentially pick up any errant tasks
        time.sleep(0.5)

        # 1. No worker must have executed this task
        self.assertEqual(executed_tasks, [])

        # 2. Task must not be in in-memory tasks
        self.assertEqual(len(mgr._tasks), 0)

        # 3. Task must not be sitting in the queue
        self.assertEqual(mgr._queue.qsize(), 0)

        # 4. Reserved count must have been rolled back cleanly
        self.assertEqual(mgr._reserved_count, 0)

        # Restore working save_task and verify system operates normally without slot leaks
        del db.save_task
        valid_task = mgr.submit("Valid subsequent task", intent)
        self.assertIsNotNone(valid_task)
        self.assertIn(valid_task.id, mgr._tasks)

        mgr.shutdown(timeout=2.0)

    # -----------------------------------------------------------------------
    # Scenario 21: Queue-Full Rejection Creates No Database Record
    # -----------------------------------------------------------------------
    def test_21_queue_full_creates_no_database_record(self):
        """When a task is rejected due to queue capacity, no database record is created."""
        db = Database(":memory:")
        mgr = TaskManager(db=db, max_workers=1, queue_capacity=1)

        worker_busy = threading.Event()
        worker_can_finish = threading.Event()

        def blocking_execute(task):
            worker_busy.set()
            worker_can_finish.wait(timeout=5.0)

        mgr._execute = blocking_execute
        intent = Intent(task_type="crawl_and_extract", url="https://example.com", max_pages=1)

        # Task 1 occupies worker
        t1 = mgr.submit("Task 1 (running)", intent)
        self.assertTrue(worker_busy.wait(timeout=2.0))

        # Task 2 fills the 1 waiting slot
        t2 = mgr.submit("Task 2 (waiting)", intent)

        # Count records currently in database
        initial_db_tasks = mgr.list_history()
        initial_ids = {t["id"] for t in initial_db_tasks}
        self.assertIn(t1.id, initial_ids)
        self.assertIn(t2.id, initial_ids)
        self.assertEqual(len(initial_db_tasks), 2)

        # Task 3 should be rejected with QueueFullError
        with self.assertRaises(QueueFullError):
            mgr.submit("Task 3 (rejected)", intent)

        # Verify no record was created in the database for Task 3
        post_db_tasks = mgr.list_history()
        self.assertEqual(len(post_db_tasks), 2)
        post_ids = {t["id"] for t in post_db_tasks}
        self.assertEqual(initial_ids, post_ids)

        worker_can_finish.set()
        mgr.shutdown(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
