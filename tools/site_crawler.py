#!/usr/bin/env python3
"""site_crawler: hardened, polite, same-host web crawler and structured extractor.

Usage:
    site_crawler.py --url=https://example.com [--max-pages=5] [--max-depth=2]
                    [--timeout=10] [--delay=0.3] [--extract-schema=field1,field2]

Writes ONE valid JSON document to stdout. Progress and debug logs go to stderr.

Security and Correctness Controls:
- Standard library only (no external untrusted dependencies).
- DNS rebinding prevention via IP-pinned connection layer (single resolution per attempt,
  connecting directly to validated IP, verifying getpeername(), preserving TLS SNI/cert).
- Mixed public/private DNS answers are strictly rejected.
- Ambient proxy bypass is explicitly prevented.
- Strict destination restrictions on initial URL, discovered links, and redirects:
  only HTTP/HTTPS, only ports 80/443, no embedded credentials, same-host boundary.
- RFC 9309-compliant robots.txt handling:
  - 2xx: parse rules
  - 404/410: allowed
  - 401/403: disallowed
  - 5xx / network failure / blocked redirects: disallowed (never fails open)
- Bounded memory and network limits (per-response body cap, max crawl duration).
- Structured schema extraction with honest null values (never fabricates data).

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
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from html.parser import HTMLParser
from urllib.robotparser import RobotFileParser

VERSION = "2.1.0"
USER_AGENT = "BuildSprintSiteCrawler/2.1 (bounded security-hardened crawler; RFC 9309 compliant)"
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain"})
MAX_BYTES = 1_500_000  # 1.5MB body cap
EXCERPT_CHARS = 800
MAX_REDIRECTS = 5
SKIP_EXTENSIONS = (
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".zip", ".gz",
    ".tar", ".mp3", ".mp4", ".css", ".js", ".xml", ".json", ".woff", ".woff2", ".ttf",
)
EXIT_OK, EXIT_BLOCKED, EXIT_FAILED = 0, 3, 4

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443})
BLOCKED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".lan",
    ".corp",
    ".home",
    ".arpa",
)

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b")
PHONE_RE = re.compile(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")


class SecurityError(urllib.error.URLError):
    """Raised when an outbound connection attempt violates SSRF/DNS-rebinding policy."""


def log(message: str) -> None:
    """Log to stderr so stdout remains purely machine-readable JSON."""
    print(f"[site_crawler] {message}", file=sys.stderr, flush=True)


def squash(text: str) -> str:
    return " ".join(text.split())


def private_targets_allowed() -> bool:
    """Check if test bypass is enabled for offline test fixtures."""
    return os.environ.get("BUILDSPRINT_ALLOW_PRIVATE_TARGETS") == "1"


def is_ip_globally_routable(ip_str: str) -> bool:
    """Validate that an IP is global (reject loopback, private, link-local, carrier-grade NAT, etc.)."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    # Handle IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False

    # Check Carrier-Grade NAT (100.64.0.0/10)
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.IPv4Network("100.64.0.0/10"):
        return False

    # Check Documentation / benchmarking ranges (RFC 5737 / RFC 2544)
    if isinstance(ip, ipaddress.IPv4Address) and (
        ip in ipaddress.IPv4Network("192.0.2.0/24")
        or ip in ipaddress.IPv4Network("198.51.100.0/24")
        or ip in ipaddress.IPv4Network("203.0.113.0/24")
        or ip in ipaddress.IPv4Network("198.18.0.0/15")
    ):
        return False

    return ip.is_global


def resolve_and_validate_destination(host: str, port: int) -> tuple[str, list[str]]:
    """Resolves hostname once, validates ALL returned IPv4 and IPv6 addresses.

    Rejects mixed public/private answers.
    Returns: (pinned_ip, all_ips)
    """
    clean_host = host.strip().lower()
    if clean_host in BLOCKED_HOSTS or any(clean_host.endswith(sfx) for sfx in BLOCKED_SUFFIXES):
        if not private_targets_allowed():
            raise SecurityError(f"Host '{clean_host}' is a prohibited local/internal hostname")

    # If it's already an IP literal:
    try:
        literal = ipaddress.ip_address(clean_host)
        if not private_targets_allowed() and not is_ip_globally_routable(str(literal)):
            raise SecurityError(f"Literal IP '{clean_host}' is not globally routable")
        return str(literal), [str(literal)]
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(clean_host, port, type=socket.SOCK_STREAM)
    except Exception as exc:
        raise SecurityError(f"DNS resolution failed for '{clean_host}': {exc}") from exc

    if not infos:
        raise SecurityError(f"No address records found for '{clean_host}'")

    all_ips = [info[4][0].split("%")[0] for info in infos]

    if not private_targets_allowed():
        # Every single returned address MUST be globally routable
        # Reject mixed public/private answers
        for ip in all_ips:
            if not is_ip_globally_routable(ip):
                raise SecurityError(
                    f"DNS response for '{clean_host}' contained private/internal IP '{ip}'. "
                    f"Mixed or private DNS answers are strictly rejected."
                )

    # Pin to the first validated IP
    pinned_ip = all_ips[0]
    return pinned_ip, all_ips


# ---------------------------------------------------------------------------
# IP-Pinned Connection Layer (Eliminates DNS Rebinding TOCTOU)
# ---------------------------------------------------------------------------

class PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects directly to a pre-validated IP without re-resolving DNS."""

    def __init__(self, host, port=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, **kwargs):
        super().__init__(host, port=port, timeout=timeout, **kwargs)
        self.original_host = host
        self.pinned_ip = None

    def connect(self):
        port = self.port or 80
        if not private_targets_allowed() and port not in ALLOWED_PORTS:
            raise SecurityError(f"Port {port} is not allowed (only 80 and 443 permitted)")

        self.pinned_ip, _ = resolve_and_validate_destination(self.host, port)

        sock = socket.create_connection(
            (self.pinned_ip, port),
            timeout=self.timeout,
            source_address=self.source_address,
        )

        # Verify connected peer address
        peer_ip = sock.getpeername()[0].split("%")[0]
        if not private_targets_allowed() and not is_ip_globally_routable(peer_ip):
            sock.close()
            raise SecurityError(f"Connected peer IP '{peer_ip}' is not globally routable")

        self.sock = sock


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that connects directly to a pre-validated IP, preserving TLS SNI and cert checks."""

    def __init__(self, host, port=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, **kwargs):
        super().__init__(host, port=port, timeout=timeout, **kwargs)
        self.original_host = host
        self.pinned_ip = None

    def connect(self):
        port = self.port or 443
        if not private_targets_allowed() and port not in ALLOWED_PORTS:
            raise SecurityError(f"Port {port} is not allowed (only 80 and 443 permitted)")

        self.pinned_ip, _ = resolve_and_validate_destination(self.host, port)

        sock = socket.create_connection(
            (self.pinned_ip, port),
            timeout=self.timeout,
            source_address=self.source_address,
        )

        peer_ip = sock.getpeername()[0].split("%")[0]
        if not private_targets_allowed() and not is_ip_globally_routable(peer_ip):
            sock.close()
            raise SecurityError(f"Connected peer IP '{peer_ip}' is not globally routable")

        # Initialize or enhance SSL Context with reliable CA store
        if not self._context:
            self._context = ssl.create_default_context()

        cert_file = os.environ.get("SSL_CERT_FILE")
        if cert_file and os.path.exists(cert_file):
            try:
                self._context.load_verify_locations(cafile=cert_file)
            except Exception:
                pass
        else:
            try:
                import certifi
                self._context.load_verify_locations(cafile=certifi.where())
            except Exception:
                pass

        # Wrap socket: server_hostname MUST be self.host (original domain), NOT the IP!
        # This preserves SNI, Host certificate verification, and prevents hostname mismatch errors
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(PinnedHTTPConnection, req)


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(PinnedHTTPSConnection, req)


class AntiSsrflRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Custom redirect handler that enforces destination restrictions and same-host boundary."""

    def __init__(self, allowed_host: str, same_host_only: bool = True) -> None:
        super().__init__()
        self.allowed_host = allowed_host.lower()
        self.same_host_only = same_host_only

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            parts = urllib.parse.urlsplit(newurl)
        except ValueError as exc:
            log(f"Redirect blocked: malformed redirect URL {newurl}")
            raise SecurityError(f"Redirect blocked: malformed URL {newurl}") from exc

        # Scheme check: only http and https
        if parts.scheme.lower() not in ALLOWED_SCHEMES:
            log(f"Redirect blocked: unapproved scheme '{parts.scheme}' in {newurl}")
            raise SecurityError(f"Redirect blocked: unapproved scheme '{parts.scheme}' in {newurl}")

        # Credentials check
        if parts.username is not None or parts.password is not None:
            log(f"Redirect blocked: embedded credentials in {newurl}")
            raise SecurityError(f"Redirect blocked: embedded credentials in {newurl}")

        # Port restriction
        if parts.port is not None and not private_targets_allowed():
            if parts.port not in ALLOWED_PORTS:
                log(f"Redirect blocked: non-standard port {parts.port} in {newurl}")
                raise SecurityError(f"Redirect blocked: non-standard port {parts.port} in {newurl}")
            if parts.scheme.lower() == "http" and parts.port != 80:
                log(f"Redirect blocked: port {parts.port} not permitted for http")
                raise SecurityError(f"Redirect blocked: port {parts.port} not permitted for http")
            if parts.scheme.lower() == "https" and parts.port != 443:
                log(f"Redirect blocked: port {parts.port} not permitted for https")
                raise SecurityError(f"Redirect blocked: port {parts.port} not permitted for https")

        redirect_host = (parts.hostname or "").lower()
        if not redirect_host:
            log(f"Redirect blocked: missing host name in {newurl}")
            raise SecurityError(f"Redirect blocked: missing host name in {newurl}")

        # Same-host check if required
        if self.same_host_only and redirect_host != self.allowed_host:
            log(f"Redirect blocked: destination '{redirect_host}' leaves start host '{self.allowed_host}'")
            raise SecurityError(f"Redirect blocked: destination '{redirect_host}' leaves start host '{self.allowed_host}'")

        # Pre-connection IP resolution and validation (ensures destination is safe)
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        resolve_and_validate_destination(redirect_host, port)

        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_safe_opener(allowed_host: str, same_host_only: bool = True) -> urllib.request.OpenerDirector:
    """Constructs an opener with pinned IP connections, Anti-SSRF redirect enforcement,

    and explicit ambient proxy disabling to guarantee destination verification.
    """
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),  # Explicitly disable ambient proxy bypass
        AntiSsrflRedirectHandler(allowed_host, same_host_only=same_host_only),
        PinnedHTTPHandler(),
        PinnedHTTPSHandler(),
    )


# ---------------------------------------------------------------------------
# HTML Parsing & Content Extraction
# ---------------------------------------------------------------------------

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
    """Return a normalized same-host URL or None if outside scope or violating restrictions."""
    try:
        absolute, _ = urllib.parse.urldefrag(urllib.parse.urljoin(base_url, href))
        parts = urllib.parse.urlsplit(absolute)
    except ValueError:
        return None

    # Scheme restriction: only http and https
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return None

    # Same host restriction
    if (parts.hostname or "").lower() != host.lower():
        return None

    # No embedded credentials
    if parts.username is not None or parts.password is not None:
        return None

    # Port restriction: only 80 and 443 (or None)
    if parts.port is not None and not private_targets_allowed():
        if parts.port not in ALLOWED_PORTS:
            return None
        if parts.scheme.lower() == "http" and parts.port != 80:
            return None
        if parts.scheme.lower() == "https" and parts.port != 443:
            return None

    if parts.path.lower().endswith(SKIP_EXTENSIONS):
        return None

    return canonical(absolute)


def fetch(url: str, host: str, timeout: float, same_host_only: bool = True) -> dict:
    """Fetch URL using the IP-pinned safe opener."""
    opener = build_safe_opener(host, same_host_only=same_host_only)
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
    """Fetch and parse robots.txt according to RFC 9309 rules.

    - 2xx: parse rules
    - 404/410: allowed
    - 401/403: disallowed
    - 5xx or connection error: disallowed (never fails open)
    - Blocked redirect: disallowed (never fails open)
    """
    parser = RobotFileParser()
    robots_url = f"{base_url}/robots.txt"
    try:
        # Cross-authority redirects permitted for robots.txt if meeting security policy
        response = fetch(robots_url, host, timeout, same_host_only=False)
        parser.parse(decode(response["body"], response["charset"]).splitlines())
        return parser, "found"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            parser.parse(["User-agent: *", "Disallow: /"])
            return parser, f"HTTP {exc.code}: access denied, crawling disallowed (RFC 9309)"
        elif exc.code in (404, 410):
            parser.parse([])
            return parser, f"HTTP {exc.code}: not found, crawling allowed (RFC 9309)"
        elif 400 <= exc.code < 500:
            # Other 4xx treated as unavailable -> crawling allowed per RFC 9309 2.3.1.3
            parser.parse([])
            return parser, f"HTTP {exc.code}: client error, crawling allowed (RFC 9309)"
        elif 500 <= exc.code < 600:
            # 5xx server error -> full disallow per RFC 9309 2.3.1.2
            parser.parse(["User-agent: *", "Disallow: /"])
            return parser, f"HTTP {exc.code}: server error, crawling disallowed (RFC 9309)"
        else:
            parser.parse(["User-agent: *", "Disallow: /"])
            return parser, f"HTTP {exc.code}: unexpected error, crawling disallowed (RFC 9309)"
    except SecurityError as exc:
        # Redirect or connection blocked by security policy -> full disallow per RFC 9309
        parser.parse(["User-agent: *", "Disallow: /"])
        return parser, f"security restriction ({exc}): crawling disallowed (RFC 9309)"
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # Network failure / unreachable -> full disallow per RFC 9309 2.3.1.2
        parser.parse(["User-agent: *", "Disallow: /"])
        return parser, f"unavailable ({getattr(exc, 'reason', exc)}): crawling disallowed (RFC 9309)"


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
            service_headings = [
                h for h in all_headings
                if any(k in h.lower() for k in ("service", "solution", "what we do", "features"))
            ]
            structured[field_clean] = service_headings[:5] if service_headings else None
        elif "product" in field_clean or "categor" in field_clean:
            prod_headings = [
                h for h in all_headings
                if any(k in h.lower() for k in ("product", "category", "offerings", "catalog"))
            ]
            structured[field_clean] = prod_headings[:5] if prod_headings else None
        else:
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
    try:
        start = urllib.parse.urlsplit(start_url)
    except ValueError as exc:
        return {
            "blocked": False,
            "pages": [],
            "errors": [{"url": start_url, "error": f"Malformed start URL: {exc}"}],
            "attempts": 0,
            "robots_note": "check blocked",
        }

    # Validate scheme
    if start.scheme.lower() not in ALLOWED_SCHEMES:
        return {
            "blocked": False,
            "pages": [],
            "errors": [{"url": start_url, "error": f"Scheme '{start.scheme}' not allowed (only http/https)"}],
            "attempts": 0,
            "robots_note": "check blocked",
        }

    # Validate credentials
    if start.username is not None or start.password is not None:
        return {
            "blocked": False,
            "pages": [],
            "errors": [{"url": start_url, "error": "URLs with embedded credentials are not allowed"}],
            "attempts": 0,
            "robots_note": "check blocked",
        }

    # Validate port
    if start.port is not None and not private_targets_allowed():
        if start.port not in ALLOWED_PORTS:
            return {
                "blocked": False,
                "pages": [],
                "errors": [{"url": start_url, "error": f"Port {start.port} not allowed (only 80 and 443 permitted)"}],
                "attempts": 0,
                "robots_note": "check blocked",
            }
        if start.scheme.lower() == "http" and start.port != 80:
            return {
                "blocked": False,
                "pages": [],
                "errors": [{"url": start_url, "error": f"Port {start.port} not allowed for http"}],
                "attempts": 0,
                "robots_note": "check blocked",
            }
        if start.scheme.lower() == "https" and start.port != 443:
            return {
                "blocked": False,
                "pages": [],
                "errors": [{"url": start_url, "error": f"Port {start.port} not allowed for https"}],
                "attempts": 0,
                "robots_note": "check blocked",
            }

    host = (start.hostname or "").lower()
    if not host:
        return {
            "blocked": False,
            "pages": [],
            "errors": [{"url": start_url, "error": "URL has no host name"}],
            "attempts": 0,
            "robots_note": "check blocked",
        }

    base_origin = f"{start.scheme}://{start.netloc}"

    # Pre-flight resolution and IP safety verification
    port = start.port or (443 if start.scheme.lower() == "https" else 80)
    try:
        resolve_and_validate_destination(host, port)
    except SecurityError as exc:
        log(f"Start URL blocked by destination validation: {exc}")
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
            response = fetch(url, host, timeout, same_host_only=True)
        except urllib.error.HTTPError as exc:
            errors.append({"url": url, "error": f"HTTP {exc.code}"})
            continue
        except SecurityError as exc:
            errors.append({"url": url, "error": f"Security restriction: {exc}"})
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
    if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.netloc:
        parser.error("--url must be an absolute http(s) URL")

    schema_fields = (
        [f.strip() for f in args.extract_schema.split(",") if f.strip()]
        if args.extract_schema
        else []
    )

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
