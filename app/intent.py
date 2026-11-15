"""Intent understanding: turn a free-text request into a structured task.

This layer is rule-based on purpose: it is deterministic, easy to test, and needs
no API key. parse_intent() is the seam where a language model could be plugged in
later without changing anything downstream.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .validation import ValidationError, normalise_url

TASK_CRAWL = "website_crawl"
DEFAULT_PAGES = 5
MAX_PAGES = 10
CRAWL_KEYWORDS = ("crawl", "summar", "extract", "scrape", "browse", "json", "website", "site", "page")

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_DOMAIN_RE = re.compile(
    r"(?<![\w@/.-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,24}(?:/[^\s<>\"']*)?)",
    re.IGNORECASE,
)
_PAGES_RE = re.compile(r"\b(\d{1,3})\s+pages?\b", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?)]}'\""


@dataclass(frozen=True)
class Intent:
    task_type: str
    url: str
    max_pages: int
    notes: tuple[str, ...] = ()

    def describe(self) -> str:
        return (
            f"Crawl {self.url} (up to {self.max_pages} pages), summarise the content, "
            "and return the extracted data as JSON."
        )


class IntentError(ValueError):
    """User-facing: the request could not be turned into a supported task."""


def extract_url(text: str) -> str | None:
    match = _URL_RE.search(text)
    if match:
        return match.group(0).rstrip(_TRAILING_PUNCTUATION)
    match = _DOMAIN_RE.search(text)
    if match:
        return "https://" + match.group(1).rstrip(_TRAILING_PUNCTUATION)
    return None


def parse_intent(prompt: object) -> Intent:
    if not isinstance(prompt, str) or not prompt.strip():
        raise IntentError("Please type a task first, for example: Crawl https://example.com and summarise it.")

    text = prompt.strip()
    lowered = text.lower()
    notes: list[str] = []

    raw_url = extract_url(text)
    if raw_url is None:
        raise IntentError(
            "I couldn't find a website address in that request. Include a full address such as https://example.com."
        )
    if len(_URL_RE.findall(text)) > 1:
        notes.append("More than one URL was given; only the first one will be crawled.")
    try:
        url = normalise_url(raw_url)
    except ValidationError as exc:
        raise IntentError(f"That address isn't usable: {exc}.") from None

    if not any(keyword in lowered for keyword in CRAWL_KEYWORDS):
        notes.append("No explicit task words found, so I'm assuming you want the site crawled and summarised.")

    max_pages = DEFAULT_PAGES
    match = _PAGES_RE.search(lowered)
    if match:
        requested = int(match.group(1))
        max_pages = min(max(requested, 1), MAX_PAGES)
        if requested != max_pages:
            notes.append(f"Page limit set to {max_pages} (allowed range is 1 to {MAX_PAGES}).")

    return Intent(task_type=TASK_CRAWL, url=url, max_pages=max_pages, notes=tuple(notes))
