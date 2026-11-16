"""Intent understanding and execution planning for MAI-Chat.

Turns free-text requests into validated, structured task execution plans.
Supports:
1. Deterministic rule-based parsing (default, testable, zero-dependency, works offline).
2. Optional structured LLM planner via standard OpenAI-compatible or generic REST APIs
   configured via environment variables (LLM_API_KEY, LLM_API_BASE, LLM_MODEL).
3. Strict plan validation: rejects unapproved task types, arbitrary commands, or malformed plans.
4. Structured extraction field discovery (e.g. company name, services, contacts).
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .validation import ValidationError, normalise_url

log = logging.getLogger("buildsprint.intent")

TASK_CRAWL = "website_crawl"
TASK_FOLLOWUP = "thread_followup"
DEFAULT_PAGES = 5
MAX_PAGES = 10
MAX_DEPTH = 3
CRAWL_KEYWORDS = ("crawl", "summar", "extract", "scrape", "browse", "json", "website", "site", "page")

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_DOMAIN_RE = re.compile(
    r"(?<![\w@/.-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,24}(?:/[^\s<>\"']*)?)",
    re.IGNORECASE,
)
_PAGES_RE = re.compile(r"\b(\d{1,3})\s+pages?\b", re.IGNORECASE)
_DEPTH_RE = re.compile(r"\b(?:depth|levels?)\s+(\d)\b", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?)]}'\""

# Extraction keywords pattern
_EXTRACT_RE = re.compile(r"(?:extract|find|get)\s+(?:its\s+)?([^.]+?)(?:(?:and\s+return)|(?:in\s+json)|$)", re.IGNORECASE)


@dataclass(frozen=True)
class TaskPlan:
    """Strictly validated execution plan."""
    task_type: str
    target_url: str
    requested_operation: str
    expected_output_format: str
    max_pages: int
    max_depth: int
    extraction_fields: tuple[str, ...] = ()
    confirmation_needed: bool = False
    execution_steps: tuple[str, ...] = ()
    source: str = "rules"  # 'rules' or 'llm'

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type,
            "target_url": self.target_url,
            "requested_operation": self.requested_operation,
            "expected_output_format": self.expected_output_format,
            "max_pages": self.max_pages,
            "max_depth": self.max_depth,
            "extraction_fields": list(self.extraction_fields),
            "confirmation_needed": self.confirmation_needed,
            "execution_steps": list(self.execution_steps),
            "source": self.source,
        }


@dataclass(frozen=True)
class Intent:
    """High-level user intent with backward-compatible interface and attached plan."""
    task_type: str
    url: str
    max_pages: int
    notes: tuple[str, ...] = ()
    plan: TaskPlan | None = None

    def describe(self) -> str:
        if self.task_type == TASK_FOLLOWUP:
            q = self.notes[0] if self.notes else "Follow-up question"
            return f"Answer follow-up question from stored crawl of {self.url}: '{q}'"
        fields_desc = ""
        if self.plan and self.plan.extraction_fields:
            fields_desc = f", extract specific fields ({', '.join(self.plan.extraction_fields)})"
        return (
            f"Crawl {self.url} (up to {self.max_pages} pages){fields_desc}, summarise the content, "
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


def _detect_extraction_fields(text: str) -> list[str]:
    """Detect if the user requested specific schema fields to extract."""
    fields: list[str] = []
    match = _EXTRACT_RE.search(text)
    if match:
        raw_items = match.group(1)
        parts = re.split(r",|\band\b", raw_items)
        for part in parts:
            cleaned = part.strip().strip(_TRAILING_PUNCTUATION).lower()
            if cleaned.startswith("the "):
                cleaned = cleaned[4:].strip()
            elif cleaned.startswith("a "):
                cleaned = cleaned[2:].strip()
            elif cleaned.startswith("its "):
                cleaned = cleaned[4:].strip()
            if cleaned and cleaned not in (
                "important information", "information", "data", "summary",
                "give me a summary", "content", "details", "everything",
            ):
                fields.append(cleaned)
    return fields[:8]


def _parse_with_rules(prompt: str) -> Intent:
    """Deterministic, rule-based intent parser."""
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

    max_depth = 2
    depth_match = _DEPTH_RE.search(lowered)
    if depth_match:
        max_depth = min(max(int(depth_match.group(1)), 0), MAX_DEPTH)

    extraction_fields = _detect_extraction_fields(text)
    if extraction_fields:
        notes.append(f"Target extraction schema: {', '.join(extraction_fields)}.")

    plan = TaskPlan(
        task_type=TASK_CRAWL,
        target_url=url,
        requested_operation="crawl_and_extract",
        expected_output_format="json",
        max_pages=max_pages,
        max_depth=max_depth,
        extraction_fields=tuple(extraction_fields),
        confirmation_needed=False,
        execution_steps=(
            "Validate target URL against SSRF policy",
            f"Select approved tool 'site-crawler' (limit: {max_pages} pages, depth: {max_depth})",
            "Launch isolated CLI subprocess and stream captured output",
            "Validate JSON document structure and extract requested schema fields",
            "Synthesize verified summary and format execution report",
        ),
        source="rules",
    )

    return Intent(task_type=TASK_CRAWL, url=url, max_pages=max_pages, notes=tuple(notes), plan=plan)


def _call_optional_llm(prompt: str) -> dict[str, Any] | None:
    """Optional LLM integration for structured plan generation.

    Configured via:
        LLM_API_KEY: Authentication key
        LLM_API_BASE: Base URL (default: https://api.openai.com/v1)
        LLM_MODEL: Model identifier (default: gpt-3.5-turbo or gemini-1.5-flash)
    Never exposes keys. Gracefully returns None on any failure so deterministic rules take over.
    """
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        return None

    api_base = os.environ.get("LLM_API_BASE", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL", "gpt-3.5-turbo")

    system_prompt = (
        "You are an AI task planner for an approved CLI execution engine. "
        "Convert the user's natural language request into a structured JSON execution plan. "
        "Allowed task_type is ONLY 'website_crawl'. You must NEVER output executable commands or shell code. "
        "Output ONLY a valid JSON object with keys: "
        "'task_type' (string 'website_crawl'), 'target_url' (string), 'max_pages' (integer 1-10), "
        "'max_depth' (integer 0-3), 'extraction_fields' (array of strings, or empty), "
        "'requested_operation' (string), 'notes' (array of strings)."
    )

    url = f"{api_base}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 500,
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            # Extract JSON object from LLM response
            start_idx = content.find("{")
            end_idx = content.rfind("}")
            if start_idx != -1 and end_idx != -1:
                return json.loads(content[start_idx : end_idx + 1])
    except Exception as exc:
        log.warning("Optional LLM planner skipped/failed (%s); falling back to deterministic rules", exc)
        return None
    return None


def parse_intent(prompt: object) -> Intent:
    """Parse user prompt into an Intent and structured TaskPlan.

    Attempts optional LLM planner if configured and valid;
    always falls back safely and deterministically to the rule-based engine.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise IntentError("Please type a task first, for example: Crawl https://example.com and summarise it.")

    text = prompt.strip()

    # If LLM is configured, attempt structured planning
    llm_doc = None
    if os.environ.get("LLM_API_KEY"):
        llm_doc = _call_optional_llm(text)

    if llm_doc and isinstance(llm_doc, dict):
        try:
            task_type = llm_doc.get("task_type")
            if task_type != TASK_CRAWL:
                raise ValueError("Unsupported task_type from LLM plan")

            raw_url = llm_doc.get("target_url") or extract_url(text)
            if not raw_url:
                raise ValueError("No target URL in LLM plan")
            url = normalise_url(raw_url)

            max_pages = min(max(int(llm_doc.get("max_pages", DEFAULT_PAGES)), 1), MAX_PAGES)
            max_depth = min(max(int(llm_doc.get("max_depth", 2)), 0), MAX_DEPTH)
            fields = [str(f) for f in llm_doc.get("extraction_fields", []) if isinstance(f, str)]
            notes = [str(n) for n in llm_doc.get("notes", []) if isinstance(n, str)]
            notes.append("Structured plan generated via LLM planner and verified by backend.")

            plan = TaskPlan(
                task_type=TASK_CRAWL,
                target_url=url,
                requested_operation=str(llm_doc.get("requested_operation", "crawl_and_extract")),
                expected_output_format="json",
                max_pages=max_pages,
                max_depth=max_depth,
                extraction_fields=tuple(fields),
                confirmation_needed=False,
                execution_steps=(
                    "Validate target URL against SSRF policy",
                    f"Select approved tool 'site-crawler' (limit: {max_pages} pages, depth: {max_depth})",
                    "Launch isolated CLI subprocess and stream captured output",
                    "Validate JSON document structure and extract requested schema fields",
                    "Synthesize verified summary and format execution report",
                ),
                source="llm",
            )
            return Intent(task_type=TASK_CRAWL, url=url, max_pages=max_pages, notes=tuple(notes), plan=plan)
        except Exception as exc:
            log.warning("LLM plan rejected during validation (%s); falling back to rule parser", exc)

    # Deterministic rule-based parsing
    return _parse_with_rules(text)


@dataclass(frozen=True)
class FollowupIntent:
    """Follow-up question intent for threads with stored crawl memory."""
    question: str
    task_type: str = TASK_FOLLOWUP
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteDecision:
    """Routing decision for thread messages."""
    action: str  # 'crawl' | 'followup' | 'needs_url'
    intent: Intent | None = None
    question: str | None = None
    message: str | None = None


def parse_followup(prompt: str) -> FollowupIntent:
    """Parse a follow-up question within a thread having crawl memory."""
    text = (prompt or "").strip()
    if not text:
        raise IntentError("Prompt cannot be empty.")
    if len(text) > 1000:
        raise IntentError("Prompt is too long (maximum 1000 characters).")
    return FollowupIntent(question=text)


def route_thread_message(prompt: str, thread_context: dict[str, Any] | None) -> RouteDecision:
    """Route a thread message to crawl, followup, or needs_url.

    Rules:
    - Empty or >1000 chars: raises IntentError (400 rejection).
    - Prompt contains a valid URL: route to crawl.
    - No URL and thread has crawl_doc: route to followup.
    - No URL and thread has no crawl_doc: route to needs_url.
    """
    text = (prompt or "").strip()
    if not text:
        raise IntentError("Prompt cannot be empty.")
    if len(text) > 1000:
        raise IntentError("Prompt is too long (maximum 1000 characters).")

    raw_url = extract_url(text)
    if raw_url is not None:
        # Prompt contains a URL attempt; parse with parse_intent (which validates the URL)
        crawl_intent = parse_intent(text)
        return RouteDecision(action="crawl", intent=crawl_intent)

    ctx = thread_context or {}
    has_crawl_doc = bool(ctx.get("crawl_doc"))

    if has_crawl_doc:
        followup_intent = parse_followup(text)
        return RouteDecision(action="followup", question=followup_intent.question)

    return RouteDecision(
        action="needs_url",
        message="Give me a website to work on first, e.g. Crawl https://example.com.",
    )
