"""Input validation and SSRF defenses for target URLs.

Enforces strict defense-in-depth:
- Protocol schemes allowlist (http, https)
- Port allowlist (80, 443 only)
- No embedded credentials (user:pass@host)
- RFC 1918 / RFC 4193 / RFC 4291 / RFC 3927 private, loopback, link-local, and carrier-grade NAT blocks
- IPv4-mapped IPv6 normalization and validation
- DNS resolution inspection (all resolved addresses must be globally routable; rejects mixed answers)
- Startup safety enforcement for test bypass (BUILDSPRINT_ALLOW_PRIVATE_TARGETS=1).
"""
from __future__ import annotations

import ipaddress
import os
import socket
import urllib.parse

MAX_URL_LENGTH = 2048
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


class ValidationError(ValueError):
    """Raised with a user-facing explanation when input is rejected."""


def private_targets_allowed() -> bool:
    """Developer switch for local testing (crawl a fixture server on 127.0.0.1).

    Never set BUILDSPRINT_ALLOW_PRIVATE_TARGETS=1 on a public deployment: it
    lets users make the server fetch internal network addresses.
    """
    return os.environ.get("BUILDSPRINT_ALLOW_PRIVATE_TARGETS") == "1"


def enforce_startup_safety(bind_host: str) -> None:
    """Refuse server startup if private target bypass is enabled on a non-loopback host."""
    if private_targets_allowed():
        clean_host = bind_host.strip().lower()
        if clean_host not in ("127.0.0.1", "localhost", "::1"):
            raise RuntimeError(
                f"FATAL SECURITY CONFIGURATION: BUILDSPRINT_ALLOW_PRIVATE_TARGETS is set to 1, "
                f"but server is configured to bind to non-loopback address '{bind_host}'. "
                f"The private-target bypass is strictly permitted on loopback/localhost only."
            )


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


def is_ip_globally_routable(ip_str: str) -> bool:
    """Check if an IPv4 or IPv6 string is globally routable (not private, loopback, or reserved)."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    # Handle IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped

    # Check standard attributes
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

    # Check Documentation prefixes (RFC 5737 / RFC 3849)
    if isinstance(ip, ipaddress.IPv4Address) and (
        ip in ipaddress.IPv4Network("192.0.2.0/24")
        or ip in ipaddress.IPv4Network("198.51.100.0/24")
        or ip in ipaddress.IPv4Network("203.0.113.0/24")
        or ip in ipaddress.IPv4Network("198.18.0.0/15")
    ):
        return False

    return ip.is_global


def _require_global(ip: ipaddress._BaseAddress) -> None:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped

    if not is_ip_globally_routable(str(ip)):
        raise ValidationError("the address points to a private or internal network, which is not crawled")


def validate_public_url(raw: object) -> str:
    """Full check before crawling: syntax, blocked names, and public IP addresses only.

    Rejects any hostname whose DNS response includes private/internal addresses.
    """
    url = normalise_url(raw)
    if private_targets_allowed():
        return url
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in BLOCKED_HOSTS or any(host.endswith(sfx) for sfx in BLOCKED_SUFFIXES):
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
    if not infos:
        raise ValidationError(f"could not resolve '{host}'; no address records found")

    for info in infos:
        raw_ip = info[4][0].split("%")[0]
        _require_global(ipaddress.ip_address(raw_ip))
    return url
