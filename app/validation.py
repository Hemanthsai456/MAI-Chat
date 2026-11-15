"""Input validation for URLs that the backend will hand to the crawler."""
from __future__ import annotations

import ipaddress
import os
import socket
import urllib.parse

MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443})
BLOCKED_HOSTS = frozenset({"localhost"})
BLOCKED_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".lan")


class ValidationError(ValueError):
    """Raised with a user-facing explanation when input is rejected."""


def private_targets_allowed() -> bool:
    """Developer switch for local testing (crawl a fixture server on 127.0.0.1).

    Never set BUILDSPRINT_ALLOW_PRIVATE_TARGETS=1 on a public deployment: it
    lets users make the server fetch internal network addresses.
    """
    return os.environ.get("BUILDSPRINT_ALLOW_PRIVATE_TARGETS") == "1"


def normalise_url(raw: object) -> str:
    """Syntactic checks only (no DNS). Returns a cleaned absolute URL or raises."""
    if not isinstance(raw, str):
        raise ValidationError("the URL must be text")
    url = raw.strip()
    if not url:
        raise ValidationError("the URL is empty")
    if len(url) > MAX_URL_LENGTH:
        raise ValidationError(f"the URL is longer than {MAX_URL_LENGTH} characters")
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise ValidationError("the URL contains spaces or control characters")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValidationError(f"the URL is malformed ({exc})") from None
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValidationError("only http:// and https:// addresses are supported")
    if not parts.hostname:
        raise ValidationError("the URL has no host name")
    if parts.username is not None or parts.password is not None:
        raise ValidationError("URLs with embedded usernames or passwords are not allowed")
    if port is not None and port not in ALLOWED_PORTS and not private_targets_allowed():
        raise ValidationError("only the standard web ports (80 and 443) are allowed")
    path = parts.path or "/"
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, path, parts.query, ""))


def _require_global(ip: ipaddress._BaseAddress) -> None:
    if not ip.is_global:
        raise ValidationError("the address points to a private or internal network, which is not crawled")


def validate_public_url(raw: object) -> str:
    """Full check before crawling: syntax, blocked names, and public IP addresses only."""
    url = normalise_url(raw)
    if private_targets_allowed():
        return url
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if host in BLOCKED_HOSTS or host.endswith(BLOCKED_SUFFIXES):
        raise ValidationError("local or internal host names are not allowed")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        _require_global(literal)
        return url
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise ValidationError(
            f"could not resolve '{host}'; the site may not exist or may be offline"
        ) from None
    for info in infos:
        _require_global(ipaddress.ip_address(info[4][0].split("%")[0]))
    return url
