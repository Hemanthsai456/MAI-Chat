#!/usr/bin/env python3
"""site_crawler: hardened, polite, same-host web crawler and structured extractor.

Usage:
    site_crawler.py --url=https://example.com [--max-pages=5] [--max-depth=2]
                    [--timeout=10] [--delay=0.3] [--extract-schema=field1,field2]

Writes ONE valid JSON document to stdout. Progress and debug logs go to stderr.

Security and Reliability Controls:
- Standard library only (no untrusted dependencies).
- Strict same-host boundary (drops off-site redirects and links).
- Defense-in-depth Anti-SSRF: validates every outbound connection and redirect target IP against private/internal address spaces.
- Full compliance with robots.txt (supports User-Agent matches and Disallow rules).
- Safe HTML parsing as text data; never executes JavaScript or renders external content.
- Bounded memory and network limits (per-response body cap, max crawl duration).
- Structured schema extraction with honest null/unavailable handling (never fabricates data).

Exit Codes:
    0  Crawl finished with at least one readable page (JSON status "success" or "partial")
    2  Invalid arguments (argparse)
    3  Start URL disallowed by robots.txt (JSON status "blocked")
    4  No page could be read (JSON status "failed")
"""
from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from html.parser import HTMLParser
from urllib.robotparser import RobotFileParser

VERSION = "2.0.0"
USER_AGENT = "BuildSprintSiteCrawler/2.0 (bounded security-hardened crawler; honours robots.txt)"
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain"})
MAX_BYTES = 1_500_000  # 1.5MB body cap
EXCERPT_CHARS = 800
MAX_REDIRECTS = 5
SKIP_EXTENSIONS = (
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".zip", ".gz",
    ".tar", ".mp3", ".mp4", ".css", ".js", ".xml", ".json", ".woff", ".woff2", ".ttf",
)
EXIT_OK, EXIT_BLOCKED, EXIT_FAILED = 0, 3, 4

# Email and Phone regex for contact extraction
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b")
PHONE_RE = re.compile(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")


def log(message: str) -> None:
    """Log to stderr so stdout remains purely machine-readable JSON."""
    print(f"[site_crawler] {message}", file=sys.stderr, flush=True)


def squash(text: str) -> str:
    return " ".join(text.split())


def private_targets_allowed() -> bool:
    """Check if test bypass is enabled for offline test fixtures."""
    return os.environ.get("BUILDSPRINT_ALLOW_PRIVATE_TARGETS") == "1"


def is_ip_globally_routable(ip_str: str) -> bool:
    """Validate that an IP is global (reject loopback, private, link-local, carrier-grade NAT)."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped

    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False

    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.IPv4Network("100.64.0.0/10"):
        return False

    return ip.is_global


def verify_host_ip_safety(host: str, port: int = 80) -> None:
    """Verify host does not resolve to private or internal addresses before connecting."""
    if private_targets_allowed():
        return

    # Literal IP check
    try:
        literal = ipaddress.ip_address(host)
        if not is_ip_globally_routable(str(literal)):
            raise ValueError(f"Target host {host} is a private/internal IP address")
        return
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except Exception as exc:
        raise ValueError(f"Could not resolve host {host}: {exc}") from exc

    for info in infos:
        raw_ip = info[4][0].split("%")[0]
        if not is_ip_globally_routable(raw_ip):
            raise ValueError(f"Host {host} resolved to non-public IP: {raw_ip}")


class AntiSsrflRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Custom redirect handler that enforces same-host and anti-SSRF checks on every hop."""

    def __init__(self, allowed_host: str) -> None:
        super().__init__()
        self.allowed_host = allowed_host.lower()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme not in ("http", "https"):
            log(f"Redirect blocked: unapproved scheme '{parts.scheme}' in {newurl}")
            return None

        redirect_host = (parts.hostname or "").lower()
        if redirect_host != self.allowed_host:
            log(f"Redirect blocked: destination '{redirect_host}' leaves start host '{self.allowed_host}'")
            return None

        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            verify_host_ip_safety(redirect_host, port)
        except ValueError as exc:
            log(f"Redirect blocked by Anti-SSRF check: {exc}")
            return None

        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PageParser(HTMLParser):
    """Comprehensive HTML parser: metadata, all headings H1-H6, links, and readable text."""

    IGNORED = frozenset({"script", "style", "noscript", "template", "svg", "header", "footer", "nav"})

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.meta_description = ""
        self.meta_keywords = ""
        self.canonical_url = ""
        self.headings: list[dict] = []
        self.internal_links: list[str] = []
        self.external_links: list[str] = []
        self.text_parts: list[str] = []

        self._skip = 0
        self._in_title = False
        self._heading_tag: str | None = None
        self._heading_buf: list[str] = []

        base_parts = urllib.parse.urlsplit(base_url)
        self.base_host = (base_parts.hostname or "").lower()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {key.lower(): (value or "") for key, value in attrs}

        if tag in self.IGNORED:
            self._skip += 1
            return

        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = a.get("name", "").lower()
            prop = a.get("property", "").lower()
            content = a.get("content", "").strip()
            if (name == "description" or prop == "og:description") and not self.meta_description:
                self.meta_description = content
            elif name == "keywords" and not self.meta_keywords:
                self.meta_keywords = content
        elif tag == "link":
            rel = a.get("rel", "").lower()
            href = a.get("href", "").strip()
            if rel == "canonical" and href:
                self.canonical_url = href
        elif tag == "a" and a.get("href"):
            raw_href = a["href"].strip()
            try:
                abs_url, _ = urllib.parse.urldefrag(urllib.parse.urljoin(self.base_url, raw_href))
                parts = urllib.parse.urlsplit(abs_url)
                if parts.scheme in ("http", "https") and parts.hostname:
                    link_host = parts.hostname.lower()
                    if link_host == self.base_host:
                        if abs_url not in self.internal_links and len(self.internal_links) < 100:
                            self.internal_links.append(abs_url)
                    else:
                        if abs_url not in self.external_links and len(self.external_links) < 50:
                            self.external_links.append(abs_url)
            except Exception:
                pass
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading_tag = tag
            self._heading_buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self.IGNORED:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag == self._heading_tag:
            text = squash("".join(self._heading_buf))
            if text and len(self.headings) < 40:
                self.headings.append({"level": int(tag[1]), "text": text})
            self._heading_tag = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        if self._heading_tag:
            self._heading_buf.append(data)
        self.text_parts.append(data)


def canonical(url: str) -> str:
    """Normalize a URL to its canonical representation."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
    )


def next_url(base_url: str, href: str, host: str) -> str | None:
    """Return a normalized same-host URL or None if outside scope."""
    try:
        absolute, _ = urllib.parse.urldefrag(urllib.parse.urljoin(base_url, href))
        parts = urllib.parse.urlsplit(absolute)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() != host:
        return None
    if parts.path.lower().endswith(SKIP_EXTENSIONS):
        return None
    return canonical(absolute)


def fetch(url: str, host: str, timeout: float) -> dict:
    """Fetch URL using opener hardened with Anti-SSRF redirect checking."""
    port = urllib.parse.urlsplit(url).port or (443 if url.startswith("https") else 80)
    verify_host_ip_safety(host, port)

    opener = urllib.request.build_opener(AntiSsrflRedirectHandler(host))
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.5",
        },
    )
    with opener.open(request, timeout=timeout) as response:
        body = response.read(MAX_BYTES + 1)
        return {
            "final_url": response.geturl(),
            "status_code": getattr(response, "status", 200),
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


def load_robots(base_url: str, host: str, timeout: float) -> tuple[RobotFileParser, str]:
    """Fetch and parse robots.txt with safe fallback."""
    parser = RobotFileParser()
    try:
        response = fetch(f"{base_url}/robots.txt", host, timeout)
        parser.parse(decode(response["body"], response["charset"]).splitlines())
        return parser, "found"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            parser.parse(["User-agent: *", "Disallow: /"])
            return parser, f"HTTP {exc.code}: crawling disallowed"
        parser.parse([])
        return parser, f"HTTP {exc.code}: no rules, crawling allowed"
    except (OSError, http.client.HTTPException, ValueError) as exc:
        parser.parse([])
        return parser, f"unavailable ({getattr(exc, 'reason', exc)}): crawling allowed"


def extract_structured_fields(pages: list[dict], requested_fields: list[str]) -> dict[str, Any]:
    """Extract requested schema fields from crawled pages.

    Honest extraction: returns null when a requested piece of information is missing;
    never fabricates facts.
    """
    if not requested_fields or not pages:
        return {}

    first_page = pages[0]
    all_text = " ".join(p.get("excerpt", "") for p in pages)
    all_headings = [h["text"] for p in pages for h in p.get("headings", [])]

    structured: dict[str, Any] = {}
    for f in requested_fields:
        field_clean = f.strip().lower().replace(" ", "_")
        if "company" in field_clean or "name" in field_clean:
            # Derive from title
            title = first_page.get("title", "")
            name_cand = title.split("|")[0].split("-")[0].strip() if title else None
            structured[field_clean] = name_cand or None
        elif "contact" in field_clean or "email" in field_clean or "phone" in field_clean:
            emails = EMAIL_RE.findall(all_text)
            phones = PHONE_RE.findall(all_text)
            contacts = {}
            if emails:
                contacts["emails"] = list(set(emails))[:3]
            if phones:
                contacts["phones"] = list(set(phones))[:3]
            structured[field_clean] = contacts if contacts else None
        elif "service" in field_clean:
            service_headings = [h for h in all_headings if any(k in h.lower() for k in ("service", "solution", "what we do", "features"))]
            structured[field_clean] = service_headings[:5] if service_headings else None
        elif "product" in field_clean or "categor" in field_clean:
            prod_headings = [h for h in all_headings if any(k in h.lower() for k in ("product", "category", "offerings", "catalog"))]
            structured[field_clean] = prod_headings[:5] if prod_headings else None
        else:
            # Check if any heading matches
            matches = [h for h in all_headings if field_clean in h.lower()]
            structured[field_clean] = matches[:3] if matches else None

    return structured


def crawl(
    start_url: str,
    max_pages: int,
    max_depth: int,
    timeout: float,
    delay: float,
    schema_fields: list[str] | None = None,
    max_total_duration: float = 90.0,
) -> dict:
    start = urllib.parse.urlsplit(start_url)
    host = (start.hostname or "").lower()
    base_origin = f"{start.scheme}://{start.netloc}"

    # Verify IP safety before touching network
    port = start.port or (443 if start.scheme == "https" else 80)
    try:
        verify_host_ip_safety(host, port)
    except ValueError as exc:
        log(f"Start URL blocked by Anti-SSRF check: {exc}")
        return {
            "blocked": False,
            "pages": [],
            "errors": [{"url": start_url, "error": f"Security restriction: {exc}"}],
            "attempts": 0,
            "robots_note": "check blocked",
        }

    robots, robots_note = load_robots(base_origin, host, timeout)
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
    crawl_start = time.monotonic()

    while queue and len(pages) < max_pages:
        if time.monotonic() - crawl_start >= max_total_duration:
            log(f"Crawl total duration cap ({max_total_duration}s) reached; returning partial results")
            break

        url, depth = queue.popleft()
        if not robots.can_fetch(USER_AGENT, url):
            errors.append({"url": url, "error": "disallowed by robots.txt"})
            continue

        attempts += 1
        log(f"fetching page {len(pages) + 1}/{max_pages} (depth {depth}): {url}")
        try:
            response = fetch(url, host, timeout)
        except urllib.error.HTTPError as exc:
            errors.append({"url": url, "error": f"HTTP {exc.code}"})
            continue
        except (OSError, http.client.HTTPException, ValueError) as exc:
            errors.append({"url": url, "error": f"request failed: {getattr(exc, 'reason', exc)}"})
            continue

        final_host = (urllib.parse.urlsplit(response["final_url"]).hostname or "").lower()
        if final_host != host:
            errors.append({"url": url, "error": f"redirected off-site to {response['final_url']}"})
            continue
        if response["content_type"] not in HTML_TYPES:
            errors.append({"url": url, "error": f"skipped non-HTML content ({response['content_type']})"})
            continue

        parser = PageParser(response["final_url"])
        parser.feed(decode(response["body"], response["charset"]))
        parser.close()

        text = squash(" ".join(parser.text_parts))
        word_count = len(text.split())

        page_record = {
            "url": response["final_url"],
            "depth": depth,
            "status_code": response["status_code"],
            "title": squash(parser.title),
            "meta_description": squash(parser.meta_description),
            "meta_keywords": squash(parser.meta_keywords),
            "canonical_url": parser.canonical_url or response["final_url"],
            "headings": parser.headings,
            "word_count": word_count,
            "excerpt": text[:EXCERPT_CHARS],
            "internal_links": parser.internal_links,
            "external_links": parser.external_links,
            "links_found": len(parser.internal_links) + len(parser.external_links),
            "truncated": response["truncated"],
        }
        pages.append(page_record)

        if depth < max_depth:
            for href in parser.internal_links:
                candidate = next_url(response["final_url"], href, host)
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    queue.append((candidate, depth + 1))

        if queue and len(pages) < max_pages and delay > 0:
            time.sleep(delay)

    structured_data = {}
    if schema_fields:
        structured_data = extract_structured_fields(pages, schema_fields)

    return {
        "blocked": False,
        "pages": pages,
        "errors": errors,
        "attempts": attempts,
        "robots_note": robots_note,
        "structured_data": structured_data,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded, same-host website crawler that prints JSON.")
    parser.add_argument("--url", required=True, help="absolute http(s) URL to start from")
    parser.add_argument("--max-pages", type=int, default=5, help="1-10 (default 5)")
    parser.add_argument("--max-depth", type=int, default=2, help="0-3 link levels (default 2)")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-request seconds, 1-30")
    parser.add_argument("--delay", type=float, default=0.3, help="politeness delay between pages (0-5s)")
    parser.add_argument("--extract-schema", type=str, default="", help="comma-separated fields to extract")
    args = parser.parse_args(argv)

    if not 1 <= args.max_pages <= 10:
        parser.error("--max-pages must be between 1 and 10")
    if not 0 <= args.max_depth <= 3:
        parser.error("--max-depth must be between 0 and 3")
    if not 1 <= args.timeout <= 30:
        parser.error("--timeout must be between 1 and 30 seconds")
    if not 0.0 <= args.delay <= 5.0:
        parser.error("--delay must be between 0.0 and 5.0 seconds")

    parts = urllib.parse.urlsplit(args.url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        parser.error("--url must be an absolute http(s) URL")

    schema_fields = [f.strip() for f in args.extract_schema.split(",") if f.strip()] if args.extract_schema else []

    started = time.monotonic()
    try:
        outcome = crawl(
            args.url,
            args.max_pages,
            args.max_depth,
            args.timeout,
            args.delay,
            schema_fields=schema_fields,
        )
    except Exception as exc:
        log(f"unexpected internal error: {exc!r}")
        outcome = {
            "blocked": False,
            "pages": [],
            "errors": [{"url": args.url, "error": f"internal crawler error: {exc}"}],
            "attempts": 0,
            "robots_note": "unknown",
            "structured_data": {},
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
        "host": (parts.hostname or parts.netloc).lower(),
        "status": status,
        "robots_txt": outcome["robots_note"],
        "pages": outcome["pages"],
        "errors": outcome["errors"],
        "structured_data": outcome.get("structured_data", {}),
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
