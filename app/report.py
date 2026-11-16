"""Validate crawler output and turn it into verified chat results.

Rule: a result is presented as successful only when the CLI exited 0, its JSON
parsed and matched the expected structure, and it contains at least one page.
Everything else is reported as partial, failed, timed out or cancelled, with the reason.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any

from .intelligence import generate_intelligence
from .intent import Intent
from .runner import CliResult

log = logging.getLogger("buildsprint.report")

SUCCEEDED = "succeeded"
PARTIAL = "partial"
FAILED = "failed"
TIMED_OUT = "timed_out"
CANCELLED = "cancelled"

MAX_SECTIONS = 10
STDERR_TAIL_CHARS = 1500


class CrawlerOutputError(ValueError):
    """The CLI produced output we cannot trust."""


def parse_crawler_output(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        raise CrawlerOutputError("the crawler produced no output")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        raise CrawlerOutputError("the crawler's output was not valid JSON") from None
    if not isinstance(doc, dict):
        raise CrawlerOutputError("the crawler's output was not a JSON object")

    status = doc.get("status")
    if status not in ("success", "partial", "failed", "blocked"):
        raise CrawlerOutputError(f"the crawler reported an unknown status {status!r}")
    pages, errors = doc.get("pages"), doc.get("errors")
    if not isinstance(pages, list) or not isinstance(errors, list):
        raise CrawlerOutputError("the crawler output is missing its pages or errors list")
    if status in ("success", "partial") and not pages:
        raise CrawlerOutputError("the crawler reported success but returned no pages")

    for page in pages:
        if not (
            isinstance(page, dict)
            and isinstance(page.get("url"), str)
            and isinstance(page.get("title"), str)
            and isinstance(page.get("meta_description"), str)
            and isinstance(page.get("excerpt"), str)
            and isinstance(page.get("headings"), list)
            and isinstance(page.get("word_count"), int)
        ):
            raise CrawlerOutputError("a page in the crawler output has an unexpected structure")
        if not all(isinstance(h, dict) and isinstance(h.get("text"), str) for h in page["headings"]):
            raise CrawlerOutputError("a heading in the crawler output has an unexpected structure")
    for err in errors:
        if not (isinstance(err, dict) and isinstance(err.get("error"), str)):
            raise CrawlerOutputError("an error entry in the crawler output has an unexpected structure")
    return doc


def build_result(intent: Intent, cli: CliResult, original_prompt: str | None = None) -> dict[str, Any]:
    execution = {
        "tool": cli.tool,
        "args": cli.args,
        "exit_code": cli.exit_code,
        "duration_s": cli.duration_s,
        "timed_out": cli.timed_out,
        "cancelled": cli.cancelled,
        "stdout_truncated": getattr(cli, "stdout_truncated", False),
        "stderr_truncated": getattr(cli, "stderr_truncated", False),
        "stderr_tail": cli.stderr[-STDERR_TAIL_CHARS:],
    }

    if cli.cancelled:
        return build_message(
            CANCELLED, "warning",
            "Cancelled by you. The crawler process was stopped, so no result is reported.",
            execution=execution,
        )
    if cli.timed_out:
        return build_message(
            TIMED_OUT, "error",
            f"The crawler did not finish within {cli.timeout_s:.0f} seconds and was stopped. "
            "No result is reported for this run.",
            execution=execution,
        )

    try:
        doc = parse_crawler_output(cli.stdout)
    except CrawlerOutputError as exc:
        return build_message(
            FAILED, "error",
            f"Task failed: {exc} (exit code {cli.exit_code}).",
            execution=execution,
        )

    question = original_prompt or intent.describe()

    if cli.exit_code != 0 or doc["status"] in ("blocked", "failed"):
        intel = None
        try:
            intel = generate_intelligence(question, doc)
        except Exception as exc:
            log.warning("Intelligence generation on failed crawl encountered error: %s", exc)
        return build_message(
            FAILED, "error",
            f"Task failed: {_failure_reason(doc, cli.exit_code)}.",
            errors=doc["errors"],
            execution=execution,
            intelligence=intel,
        )

    status = SUCCEEDED if doc["status"] == "success" else PARTIAL

    # Generate evidence-grounded intelligence
    intel = None
    try:
        intel = generate_intelligence(question, doc)
    except Exception as exc:
        log.warning("Intelligence generation on successful crawl encountered error: %s", exc)

    payload = _payload(intent, status, doc, execution, intelligence=intel)
    pages_read = len(doc["pages"])
    if status == SUCCEEDED:
        kind = "success"
        banner = f"Task succeeded: the crawler read {pages_read} page(s) and returned valid JSON."
    else:
        kind = "warning"
        banner = (
            f"Partial result: {pages_read} page(s) read, {len(doc['errors'])} could not be read. "
            "Errors are listed below."
        )
    return build_message(
        status, kind, banner,
        summary=payload["summary"],
        payload=payload,
        errors=doc["errors"],
        execution=execution,
        intelligence=intel,
    )


def build_message(
    status: str,
    kind: str,
    text: str,
    *,
    summary: str | None = None,
    payload: dict[str, Any] | None = None,
    errors: list[dict] | None = None,
    execution: dict[str, Any] | None = None,
    intelligence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    json_text = None
    if payload is not None:
        json_text = json.dumps(payload, ensure_ascii=False, indent=2)
        json.loads(json_text)  # guarantee that what the user sees is valid JSON
    msg: dict[str, Any] = {
        "task_status": status,
        "banner": {"kind": kind, "text": text},
        "summary": summary,
        "json": json_text,
        "payload": payload,
        "errors": errors or [],
        "execution": execution or {},
    }
    if intelligence:
        msg["intelligence"] = intelligence
        msg["answer"] = intelligence.get("direct_answer")
        msg["overview"] = intelligence.get("website_overview")
        msg["entities"] = intelligence.get("entities")
        msg["findings"] = intelligence.get("contextual_findings")
        msg["evidence"] = intelligence.get("evidence")
    return msg


def _failure_reason(doc: dict[str, Any], exit_code: int | None) -> str:
    if doc["status"] == "blocked":
        return "robots.txt disallows crawling this site's start page"
    if doc["errors"]:
        return "no page could be read (" + doc["errors"][0]["error"] + ")"
    return f"the crawler exited with code {exit_code}"


def _payload(
    intent: Intent,
    status: str,
    doc: dict[str, Any],
    execution: dict[str, Any],
    intelligence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pages = doc["pages"]
    host = urllib.parse.urlsplit(intent.url).hostname or ""
    sections = _sections(pages)
    description = next((p["meta_description"] for p in pages if p["meta_description"]), "")
    structured_data = doc.get("structured_data", {})
    payload_data: dict[str, Any] = {
        "task": intent.task_type,
        "status": status,
        "input": {"url": intent.url, "max_pages": intent.max_pages},
        "summary": _summary_text(host, pages, sections, doc["errors"], structured_data),
        "summary_method": "extractive",
        "site": {
            "host": host,
            "title": pages[0]["title"],
            "description": description,
            "main_sections": sections,
        },
        "structured_data": structured_data,
        "pages": [
            {
                "url": p["url"],
                "title": p["title"],
                "description": p["meta_description"],
                "headings": [h["text"] for h in p["headings"]],
                "word_count": p["word_count"],
                "excerpt": p["excerpt"],
                "canonical_url": p.get("canonical_url", p["url"]),
                "internal_links_count": len(p.get("internal_links", [])),
                "external_links_count": len(p.get("external_links", [])),
            }
            for p in pages
        ],
        "errors": doc["errors"],
        "crawl_stats": doc.get("stats", {}),
        "execution": {k: v for k, v in execution.items() if k != "stderr_tail"},
    }
    if intelligence:
        payload_data["intelligence"] = intelligence
    return payload_data


def _sections(pages: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for page in pages:
        for heading in page["headings"]:
            text = heading["text"]
            if heading.get("level", 9) <= 2 and text not in seen:
                seen.add(text)
                out.append(text)
                if len(out) >= MAX_SECTIONS:
                    return out
    return out


def _summary_text(
    host: str,
    pages: list[dict[str, Any]],
    sections: list[str],
    errors: list[dict],
    structured_data: dict[str, Any] | None = None,
) -> str:
    words = sum(p["word_count"] for p in pages)
    first = pages[0]
    lines = [f"Read {len(pages)} page(s) on {host} (about {words} words)."]
    if first["title"]:
        lines.append(f'First page title: "{first["title"]}".')
    description = next((p["meta_description"] for p in pages if p["meta_description"]), "")
    if description:
        lines.append(f"Description: {description}")
    if sections:
        lines.append("Main sections: " + "; ".join(sections) + ".")
    if first["excerpt"]:
        lines.append(f"Opening text: {first['excerpt']}")

    if structured_data:
        struct_lines = []
        for key, val in structured_data.items():
            if val is not None:
                if isinstance(val, list):
                    struct_lines.append(f"{key.replace('_', ' ').capitalize()}: {', '.join(str(v) for v in val)}")
                elif isinstance(val, dict):
                    parts = [f"{k}: {v}" for k, v in val.items()]
                    struct_lines.append(f"{key.replace('_', ' ').capitalize()}: {', '.join(parts)}")
                else:
                    struct_lines.append(f"{key.replace('_', ' ').capitalize()}: {val}")
            else:
                struct_lines.append(f"{key.replace('_', ' ').capitalize()}: (not found on page)")
        if struct_lines:
            lines.append("Extracted schema fields:\n  • " + "\n  • ".join(struct_lines))

    if errors:
        lines.append(f"{len(errors)} page(s) could not be read; see the errors list.")
    lines.append("Summary method: extractive (built from the crawled text; no language model was used).")
    return "\n".join(lines)
