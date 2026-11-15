#!/usr/bin/env python3
"""site_crawler: a small, polite, same-host crawler exposed as a CLI.

    site_crawler.py --url=https://example.com [--max-pages=5] [--max-depth=2]
                    [--timeout=10] [--delay=0.3]

Writes ONE JSON document to stdout. Progress goes to stderr, so the backend
can capture both separately.

Exit codes:
    0  crawl finished (JSON status "success" or "partial")
    2  invalid arguments (argparse)
    3  start URL disallowed by robots.txt (JSON status "blocked")
    4  nothing could be read (JSON status "failed")

Standard library only. It never executes page content: HTML is parsed as text.
"""
from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from html.parser import HTMLParser
from urllib.robotparser import RobotFileParser

VERSION = "1.0.0"
USER_AGENT = "BuildSprintSiteCrawler/1.0 (bounded demo crawler; honours robots.txt)"
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain"})
MAX_BYTES = 1_500_000  # hard cap per response body
EXCERPT_CHARS = 600
SKIP_EXTENSIONS = (
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".zip", ".gz",
    ".tar", ".mp3", ".mp4", ".css", ".js", ".xml", ".json",
)
EXIT_OK, EXIT_BLOCKED, EXIT_FAILED = 0, 3, 4


def log(message: str) -> None:
    print(f"[site_crawler] {message}", file=sys.stderr, flush=True)


def squash(text: str) -> str:
    return " ".join(text.split())


class PageParser(HTMLParser):
    """Collects title, meta description, h1-h3 headings, links and visible text."""

    IGNORED = frozenset({"script", "style", "noscript", "template", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta_description = ""
        self.headings: list[dict] = []
        self.links: list[str] = []
        self.text_parts: list[str] = []
        self._skip = 0
        self._in_title = False
        self._heading_tag: str | None = None
        self._heading_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {key: (value or "") for key, value in attrs}
        if tag in self.IGNORED:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta" and a.get("name", "").lower() == "description" and not self.meta_description:
            self.meta_description = a.get("content", "").strip()
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"].strip())
        elif tag in ("h1", "h2", "h3"):
            self._heading_tag = tag
            self._heading_buf = []

    def handle_endtag(self, tag):
        if tag in self.IGNORED:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag == self._heading_tag:
            text = squash("".join(self._heading_buf))
            if text and len(self.headings) < 25:
                self.headings.append({"level": int(tag[1]), "text": text})
            self._heading_tag = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return  # the title is metadata, not body text
        if self._skip:
            return
        if self._heading_tag:
            self._heading_buf.append(data)
        self.text_parts.append(data)


def canonical(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
    )


def next_url(base_url: str, href: str, host: str) -> str | None:
    """Return a canonical same-host http(s) URL, or None if it should not be crawled."""
    try:
        absolute, _fragment = urllib.parse.urldefrag(urllib.parse.urljoin(base_url, href))
        parts = urllib.parse.urlsplit(absolute)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or parts.netloc.lower() != host:
        return None
    if parts.path.lower().endswith(SKIP_EXTENSIONS):
        return None
    return canonical(absolute)


def fetch(url: str, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.5",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_BYTES + 1)
        return {
            "final_url": response.geturl(),
            "content_type": response.headers.get_content_type(),
            "charset": response.headers.get_content_charset(),
            "body": body[:MAX_BYTES],
            "truncated": len(body) > MAX_BYTES,
        }


def decode(body: bytes, charset: str | None) -> str:
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def load_robots(base: str, timeout: float) -> tuple[RobotFileParser, str]:
    parser = RobotFileParser()
    try:
        response = fetch(base + "/robots.txt", timeout)
        parser.parse(decode(response["body"], response["charset"]).splitlines())
        return parser, "found"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            parser.parse(["User-agent: *", "Disallow: /"])  # treat as "do not crawl"
            return parser, f"HTTP {exc.code}: crawling disallowed"
        parser.parse([])
        return parser, f"HTTP {exc.code}: no rules, crawling allowed"
    except (OSError, http.client.HTTPException, ValueError) as exc:
        parser.parse([])
        return parser, f"unavailable ({getattr(exc, 'reason', exc)}): crawling allowed"


def crawl(start_url: str, max_pages: int, max_depth: int, timeout: float, delay: float) -> dict:
    start = urllib.parse.urlsplit(start_url)
    host = start.netloc.lower()
    robots, robots_note = load_robots(f"{start.scheme}://{host}", timeout)
    log(f"robots.txt: {robots_note}")
    if not robots.can_fetch(USER_AGENT, start_url):
        log("start URL is disallowed by robots.txt; stopping")
        return {
            "blocked": True,
            "pages": [],
            "errors": [{"url": start_url, "error": "start URL disallowed by robots.txt"}],
            "attempts": 0,
            "robots_note": robots_note,
        }

    first = canonical(start_url)
    queue: deque[tuple[str, int]] = deque([(first, 0)])
    seen = {first}
    pages: list[dict] = []
    errors: list[dict] = []
    attempts = 0

    while queue and len(pages) < max_pages:
        url, depth = queue.popleft()
        if not robots.can_fetch(USER_AGENT, url):
            errors.append({"url": url, "error": "disallowed by robots.txt"})
            continue

        attempts += 1
        log(f"fetching page {len(pages) + 1}/{max_pages} (depth {depth}): {url}")
        try:
            response = fetch(url, timeout)
        except urllib.error.HTTPError as exc:
            errors.append({"url": url, "error": f"HTTP {exc.code}"})
            continue
        except (OSError, http.client.HTTPException, ValueError) as exc:
            errors.append({"url": url, "error": f"request failed: {getattr(exc, 'reason', exc)}"})
            continue

        final_host = urllib.parse.urlsplit(response["final_url"]).netloc.lower()
        if final_host != host:
            errors.append({"url": url, "error": f"redirected off-site to {response['final_url']}"})
            continue
        if response["content_type"] not in HTML_TYPES:
            errors.append({"url": url, "error": f"skipped non-HTML content ({response['content_type']})"})
            continue

        parser = PageParser()
        parser.feed(decode(response["body"], response["charset"]))
        parser.close()
        text = squash(" ".join(parser.text_parts))
        pages.append(
            {
                "url": response["final_url"],
                "depth": depth,
                "title": squash(parser.title),
                "meta_description": squash(parser.meta_description),
                "headings": parser.headings,
                "word_count": len(text.split()),
                "excerpt": text[:EXCERPT_CHARS],
                "links_found": len(parser.links),
                "truncated": response["truncated"],
            }
        )

        if depth < max_depth:
            for href in parser.links:
                candidate = next_url(response["final_url"], href, host)
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    queue.append((candidate, depth + 1))

        if queue and len(pages) < max_pages and delay > 0:
            time.sleep(delay)

    return {
        "blocked": False,
        "pages": pages,
        "errors": errors,
        "attempts": attempts,
        "robots_note": robots_note,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded, same-host website crawler that prints JSON.")
    parser.add_argument("--url", required=True, help="absolute http(s) URL to start from")
    parser.add_argument("--max-pages", type=int, default=5, help="1-10 (default 5)")
    parser.add_argument("--max-depth", type=int, default=2, help="0-3 link levels (default 2)")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-request seconds, 1-30")
    parser.add_argument("--delay", type=float, default=0.3, help="politeness delay between pages")
    args = parser.parse_args(argv)

    if not 1 <= args.max_pages <= 10:
        parser.error("--max-pages must be between 1 and 10")
    if not 0 <= args.max_depth <= 3:
        parser.error("--max-depth must be between 0 and 3")
    if not 1 <= args.timeout <= 30:
        parser.error("--timeout must be between 1 and 30 seconds")
    parts = urllib.parse.urlsplit(args.url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        parser.error("--url must be an absolute http(s) URL")

    started = time.monotonic()
    try:
        outcome = crawl(args.url, args.max_pages, args.max_depth, args.timeout, args.delay)
    except Exception as exc:  # never exit without a parseable result
        log(f"unexpected internal error: {exc!r}")
        outcome = {
            "blocked": False,
            "pages": [],
            "errors": [{"url": args.url, "error": "internal crawler error"}],
            "attempts": 0,
            "robots_note": "unknown",
        }

    if outcome["blocked"]:
        status, code = "blocked", EXIT_BLOCKED
    elif not outcome["pages"]:
        status, code = "failed", EXIT_FAILED
    elif outcome["errors"]:
        status, code = "partial", EXIT_OK
    else:
        status, code = "success", EXIT_OK

    document = {
        "crawler": {"name": "site_crawler", "version": VERSION},
        "start_url": args.url,
        "host": parts.netloc.lower(),
        "status": status,
        "robots_txt": outcome["robots_note"],
        "pages": outcome["pages"],
        "errors": outcome["errors"],
        "stats": {
            "pages_fetched": len(outcome["pages"]),
            "fetch_attempts": outcome["attempts"],
            "error_count": len(outcome["errors"]),
            "max_pages": args.max_pages,
            "max_depth": args.max_depth,
            "duration_s": round(time.monotonic() - started, 2),
        },
    }
    sys.stdout.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.flush()
    log(f"finished with status {status}")
    return code


if __name__ == "__main__":
    sys.exit(main())
