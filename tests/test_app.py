"""Tests for the chat-to-CLI pipeline. Run from the project root:

    python -m unittest discover -s tests

Network-free: a local fixture site (with a robots.txt that blocks /private/)
stands in for a public website. One test checks the real example.com when the
sandbox has internet access and is skipped otherwise.
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))
# Local fixture servers must be reached directly, even when a proxy is configured.
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

from app.intent import IntentError, parse_intent  # noqa: E402
from app.report import CrawlerOutputError, build_result, parse_crawler_output  # noqa: E402
from app.runner import CliResult, run_approved_tool  # noqa: E402
from app.server import create_server  # noqa: E402
from app.validation import ValidationError, normalise_url, validate_public_url  # noqa: E402

CRAWLER = PROJECT_DIR / "tools" / "site_crawler.py"
FIXTURE_FILES = {
    "index.html": (
        "<!doctype html><html><head><title>Fixture Home</title>"
        '<meta name="description" content="A tiny site used by the tests."></head>'
        "<body><script>var x = 'not text';</script><h1>Welcome</h1><p>Hello fixture world.</p>"
        '<a href="about.html">About</a> <a href="private/secret.html">Secret</a>'
        '<a href="logo.png">logo</a></body></html>'
    ),
    "about.html": (
        "<!doctype html><html><head><title>About Us</title></head>"
        "<body><h2>Our team</h2><p>We build small things.</p>"
        '<a href="index.html">home</a></body></html>'
    ),
    "private/secret.html": "<html><head><title>Secret</title></head><body>secret</body></html>",
    "robots.txt": "User-agent: *\nDisallow: /private/\n",
}


def _public_ip_info(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


class FixtureSite:
    """Serves FIXTURE_FILES on 127.0.0.1 for the duration of a test class."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fixture-site-")
        root = Path(self._tmp.name)
        for rel, body in FIXTURE_FILES.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(body, encoding="utf-8")

        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):  # keep test output readable
                pass

        handler = functools.partial(QuietHandler, directory=str(root))
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> "FixtureSite":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._tmp.cleanup()


class IntentTests(unittest.TestCase):
    def test_mandatory_example_prompt(self):
        intent = parse_intent(
            "Crawl https://example.com, extract the important information, give me a summary, "
            "and return the extracted data in JSON format."
        )
        self.assertEqual(intent.url, "https://example.com/")
        self.assertEqual(intent.task_type, "website_crawl")
        self.assertEqual(intent.max_pages, 5)

    def test_bare_domain_and_page_limit(self):
        intent = parse_intent("Summarise example.com up to 3 pages.")
        self.assertEqual(intent.url, "https://example.com/")
        self.assertEqual(intent.max_pages, 3)

    def test_page_limit_is_clamped_with_a_note(self):
        intent = parse_intent("Crawl https://example.com and read 50 pages")
        self.assertEqual(intent.max_pages, 10)
        self.assertTrue(any("Page limit" in note for note in intent.notes))

    def test_missing_url_is_rejected(self):
        with self.assertRaises(IntentError):
            parse_intent("Summarise the weather today")

    def test_empty_prompt_is_rejected(self):
        with self.assertRaises(IntentError):
            parse_intent("   ")

    def test_unusable_scheme_is_rejected(self):
        with self.assertRaises(IntentError):
            parse_intent("Crawl file:///etc/passwd")


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("BUILDSPRINT_ALLOW_PRIVATE_TARGETS", None)

    def tearDown(self):
        self._env.stop()

    def test_rejects_non_web_schemes_and_credentials(self):
        for bad in ("file:///etc/passwd", "ftp://example.com", "https://user:pw@example.com", "https://exa mple.com"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                normalise_url(bad)

    def test_rejects_non_standard_ports(self):
        with self.assertRaises(ValidationError):
            normalise_url("https://example.com:8443/")

    def test_rejects_local_and_private_addresses(self):
        for bad in ("http://localhost/", "http://127.0.0.1/", "http://10.1.2.3/", "http://169.254.169.254/latest"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                validate_public_url(bad)

    def test_rejects_hostnames_that_resolve_internally(self):
        internal = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 443))]
        with mock.patch("app.validation.socket.getaddrinfo", return_value=internal):
            with self.assertRaises(ValidationError):
                validate_public_url("https://intranet.example.com")

    def test_accepts_public_hostnames(self):
        with mock.patch("app.validation.socket.getaddrinfo", side_effect=_public_ip_info):
            self.assertEqual(validate_public_url("https://example.com"), "https://example.com/")

    def test_unresolvable_host_gives_a_clear_error(self):
        with mock.patch("app.validation.socket.getaddrinfo", side_effect=socket.gaierror("nope")):
            with self.assertRaises(ValidationError) as ctx:
                validate_public_url("https://no-such-host.invalid")
        self.assertIn("could not resolve", str(ctx.exception))


class CrawlerCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = FixtureSite().start()

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()

    def _run_crawler(self, url, **extra):
        import subprocess

        args = [sys.executable, "-I", str(CRAWLER), f"--url={url}", "--delay=0", "--timeout=3"]
        args += [f"--{k.replace('_', '-')}={v}" for k, v in extra.items()]
        env = {"PATH": os.environ.get("PATH", ""), "NO_PROXY": "127.0.0.1,localhost"}
        return subprocess.run(args, capture_output=True, text=True, timeout=60, env=env)

    def test_crawls_same_host_and_respects_robots(self):
        proc = self._run_crawler(self.site.base + "/", **{"max_pages": 5, "max_depth": 2})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["status"], "partial")  # /private/ was blocked by robots.txt
        urls = [page["url"] for page in doc["pages"]]
        self.assertTrue(any(u.endswith("/about.html") for u in urls))
        self.assertFalse(any("/private/" in u for u in urls))
        self.assertTrue(any("disallowed by robots.txt" in e["error"] for e in doc["errors"]))
        home = next(p for p in doc["pages"] if p["url"].endswith("/index.html") or p["url"].endswith("/"))
        self.assertEqual(home["title"], "Fixture Home")
        self.assertEqual(home["meta_description"], "A tiny site used by the tests.")
        self.assertEqual([h["text"] for h in home["headings"]], ["Welcome"])
        self.assertNotIn("not text", home["excerpt"])  # script content is not treated as page text

    def test_page_limit_is_enforced(self):
        proc = self._run_crawler(self.site.base + "/", **{"max_pages": 1})
        doc = json.loads(proc.stdout)
        self.assertEqual(len(doc["pages"]), 1)
        self.assertEqual(doc["stats"]["max_pages"], 1)

    def test_unreachable_site_exits_failed_with_json(self):
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        proc = self._run_crawler(f"http://127.0.0.1:{port}/")
        self.assertEqual(proc.returncode, 4)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["status"], "failed")
        self.assertEqual(doc["pages"], [])

    def test_invalid_arguments_are_rejected_by_the_cli(self):
        proc = self._run_crawler(self.site.base + "/", **{"max_pages": 99})
        self.assertEqual(proc.returncode, 2)


class RunnerTests(unittest.TestCase):
    def test_timeout_kills_the_process_and_is_reported(self):
        import app.runner as runner

        tool = runner.ApprovedTool("sleeper", Path(tempfile.mkstemp()[1]), "test", 5.0)
        script = Path(tempfile.mkdtemp()) / "sleep.py"
        script.write_text("import time\ntime.sleep(30)\n")
        tool = runner.ApprovedTool("sleeper", script, "test", 5.0)
        with mock.patch.dict(runner.APPROVED_TOOLS, {"sleeper": tool}):
            started = time.monotonic()
            result = run_approved_tool("sleeper", [], timeout_s=1, cancel_event=threading.Event())
        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - started, 10)

    def test_cancellation_stops_the_process(self):
        import app.runner as runner

        script = Path(tempfile.mkdtemp()) / "sleep.py"
        script.write_text("import time\ntime.sleep(30)\n")
        tool = runner.ApprovedTool("sleeper", script, "test", 60.0)
        cancel = threading.Event()
        threading.Timer(0.8, cancel.set).start()
        with mock.patch.dict(runner.APPROVED_TOOLS, {"sleeper": tool}):
            result = run_approved_tool("sleeper", [], timeout_s=30, cancel_event=cancel)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.timed_out)

    def test_unapproved_tool_is_refused(self):
        with self.assertRaises(KeyError):
            run_approved_tool("rm", ["-rf", "/"], 5, threading.Event())


class ReportTests(unittest.TestCase):
    def test_garbage_output_is_failure_not_success(self):
        with self.assertRaises(CrawlerOutputError):
            parse_crawler_output("<html>not json</html>")
        with self.assertRaises(CrawlerOutputError):
            parse_crawler_output(json.dumps({"status": "success", "pages": [], "errors": []}))

    def test_nonzero_exit_is_failed_even_with_json(self):
        intent = parse_intent("Crawl https://example.com")
        cli = CliResult("site-crawler", [], 4, json.dumps({"status": "failed", "pages": [], "errors": [{"url": "x", "error": "boom"}]}),
                        "", 0.1, 10, False, False)
        result = build_result(intent, cli)
        self.assertEqual(result["task_status"], "failed")
        self.assertIsNone(result["json"])
        self.assertIn("boom", result["banner"]["text"])


class EndToEndTests(unittest.TestCase):
    """Full stack: HTTP API -> intent -> validation -> subprocess crawler -> chat result."""

    @classmethod
    def setUpClass(cls):
        cls.site = FixtureSite().start()
        cls._env = mock.patch.dict(os.environ, {"BUILDSPRINT_ALLOW_PRIVATE_TARGETS": "1"})
        cls._env.start()
        cls.server = create_server("127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._env.stop()
        cls.site.stop()

    def _request(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _wait(self, task_id, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, view = self._request("GET", f"/api/tasks/{task_id}")
            if view["result"] is not None:
                return view
            time.sleep(0.2)
        self.fail("task did not finish in time")

    def test_success_path_returns_summary_and_valid_json(self):
        status, created = self._request("POST", "/api/tasks",
                                        {"prompt": f"Crawl {self.site.base}/ and return JSON"})
        self.assertEqual(status, 202)
        view = self._wait(created["id"])
        result = view["result"]
        self.assertIn(result["task_status"], ("succeeded", "partial"))
        self.assertIn("Fixture Home", result["summary"])
        payload = json.loads(result["json"])
        self.assertEqual(payload["task"], "website_crawl")
        self.assertTrue(payload["pages"])
        self.assertEqual(payload["summary_method"], "extractive")
        self.assertIn("stderr_tail", result["execution"])
        self.assertTrue(any("[site_crawler]" in line or "Crawler exited" in line for line in view["progress"]))

    def test_invalid_request_is_rejected_with_a_400(self):
        status, body = self._request("POST", "/api/tasks", {"prompt": "tell me a joke"})
        self.assertEqual(status, 400)
        self.assertIn("website address", body["error"])

    def test_unreachable_site_reports_honest_failure(self):
        status, created = self._request("POST", "/api/tasks", {"prompt": "Summarise http://127.0.0.1:1/"})
        self.assertEqual(status, 202)
        result = self._wait(created["id"])["result"]
        self.assertEqual(result["task_status"], "failed")
        self.assertIsNone(result["summary"])
        self.assertIsNone(result["json"])

    def test_prompt_size_limit(self):
        status, body = self._request("POST", "/api/tasks", {"prompt": "x" * 1001})
        self.assertEqual(status, 400)

    def test_unknown_task_is_404(self):
        status, _ = self._request("GET", "/api/tasks/" + "0" * 32)
        self.assertEqual(status, 404)

    def test_static_page_is_served_with_security_headers(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("default-src 'self'", resp.headers["Content-Security-Policy"])
            self.assertIn(b"Website Task Chat", resp.read())


@unittest.skipUnless(os.environ.get("RUN_LIVE_TESTS") == "1", "set RUN_LIVE_TESTS=1 to hit the public internet")
class LiveExampleDotComTest(unittest.TestCase):
    def test_example_com(self):
        proc = subprocess_run_crawler("https://example.com/")
        self.assertIn(proc.returncode, (0, 3))
        doc = json.loads(proc.stdout)
        self.assertIn(doc["status"], ("success", "partial", "blocked"))


def subprocess_run_crawler(url: str):
    import subprocess

    return subprocess.run(
        [sys.executable, "-I", str(CRAWLER), f"--url={url}", "--max-pages=2", "--timeout=10"],
        capture_output=True, text=True, timeout=120,
        env={k: v for k, v in os.environ.items() if k in ("PATH", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy", "SSL_CERT_FILE")},
    )


if __name__ == "__main__":
    unittest.main()
