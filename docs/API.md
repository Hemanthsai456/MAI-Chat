# MAI-Chat — REST API Reference

**BuildSprint Round 2 · Verified CLI Execution API**

Base URL: `http://127.0.0.1:8000`

---

## 1. System & Engine Endpoints

### `GET /api/health`
Returns system status, active version, and registered CLI tools.

**Response (200 OK):**
```json
{
  "ok": true,
  "engine": "MAI-Chat CLI Execution Engine",
  "version": "2.0.0",
  "tools": [
    {
      "id": "site-crawler",
      "name": "site-crawler",
      "description": "Bounded, polite, same-host website crawler with robots.txt enforcement and structured extraction.",
      "max_timeout_s": 120.0,
      "max_output_bytes": 2000000,
      "supported_flags": ["--url", "--max-pages", "--max-depth", "--timeout", "--delay", "--extract-schema"]
    }
  ]
}
```

### `GET /api/tools`
Lists all approved CLI tools and their parameter constraints.

**Response (200 OK):**
```json
{
  "tools": [
    {
      "id": "site-crawler",
      "name": "site-crawler",
      "description": "Bounded, polite, same-host website crawler with robots.txt enforcement and structured extraction.",
      "max_timeout_s": 120.0,
      "max_output_bytes": 2000000,
      "supported_flags": ["--url", "--max-pages", "--max-depth", "--timeout", "--delay", "--extract-schema"]
    }
  ]
}
```

---

## 2. Task Management Endpoints

### `POST /api/tasks`
Submits a natural language task.

**Request Body:**
```json
{
  "prompt": "Crawl https://example.com, extract the important information, give me a summary, and return the extracted data in JSON format."
}
```

**Response (202 Accepted):**
```json
{
  "id": "a1b2c3d4e5f6789012345678abcdef01",
  "status": "queued",
  "interpretation": "Crawl https://example.com/ (up to 5 pages), summarise the content, and return the extracted data as JSON.",
  "plan": {
    "task_type": "website_crawl",
    "target_url": "https://example.com/",
    "requested_operation": "crawl_and_extract",
    "expected_output_format": "json",
    "max_pages": 5,
    "max_depth": 2,
    "extraction_fields": [],
    "confirmation_needed": false,
    "execution_steps": [
      "Validate target URL against SSRF policy",
      "Select approved tool 'site-crawler' (limit: 5 pages, depth: 2)",
      "Launch isolated CLI subprocess and stream captured output",
      "Validate JSON document structure and extract requested schema fields",
      "Synthesize verified summary and format execution report"
    ],
    "source": "rules"
  },
  "notes": []
}
```

**Error Responses:**
- `400 Bad Request`: Empty prompt, prompt > 1000 characters, or invalid URL.
- `413 Payload Too Large`: Request body exceeds 8192 bytes.
- `429 Too Many Requests`: Queue capacity full or rate limit exceeded.

---

### `GET /api/tasks`
Retrieves persistent task history from SQLite.

**Query Parameters:**
- `limit` (integer, default 50, max 200)
- `offset` (integer, default 0)

**Response (200 OK):**
```json
{
  "tasks": [
    {
      "id": "a1b2c3d4e5f6789012345678abcdef01",
      "prompt": "Crawl https://example.com",
      "task_type": "website_crawl",
      "target_url": "https://example.com/",
      "status": "succeeded",
      "tool": "site-crawler",
      "created_at": 1731671234.5,
      "started_at": 1731671234.6,
      "completed_at": 1731671236.2,
      "duration_s": 1.6,
      "exit_code": 0
    }
  ],
  "count": 1
}
```

---

### `GET /api/tasks/<id>`
Polls task status, real-time 10-stage execution pipeline, progress log, and final verified result.

**Response (200 OK):**
```json
{
  "id": "a1b2c3d4e5f6789012345678abcdef01",
  "status": "succeeded",
  "prompt": "Crawl https://example.com",
  "interpretation": "Crawl https://example.com/ (up to 5 pages), summarise the content, and return the extracted data as JSON.",
  "stages": [
    {"stage": "request_received", "label": "Request received and enqueued", "status": "completed", "timestamp": 1731671234.5, "detail": "Request accepted and assigned ID"},
    {"stage": "intent_interpreted", "label": "Intent interpreted from natural language", "status": "completed", "timestamp": 1731671234.5, "detail": "..."},
    {"stage": "plan_prepared", "label": "Execution plan prepared", "status": "completed", "timestamp": 1731671234.5, "detail": "..."},
    {"stage": "input_validated", "label": "Target URL and parameters validated", "status": "completed", "timestamp": 1731671234.6, "detail": "Target URL is valid and public"},
    {"stage": "cli_selected", "label": "Approved CLI tool selected", "status": "completed", "timestamp": 1731671234.6, "detail": "Tool site-crawler selected"},
    {"stage": "process_started", "label": "CLI subprocess launched", "status": "completed", "timestamp": 1731671234.6, "detail": "Subprocess launched with isolated environment"},
    {"stage": "execution_running", "label": "Tool execution running", "status": "completed", "timestamp": 1731671236.0, "detail": "Subprocess finished with exit code 0"},
    {"stage": "output_captured", "label": "Process output captured safely", "status": "completed", "timestamp": 1731671236.0, "detail": "Captured stdout and stderr"},
    {"stage": "result_validated", "label": "Output document and schema validated", "status": "completed", "timestamp": 1731671236.1, "detail": "JSON schema and page structures verified"},
    {"stage": "task_completed", "label": "Task completed and verified", "status": "completed", "timestamp": 1731671236.1, "detail": "Task succeeded: the crawler read 1 page(s) and returned valid JSON."}
  ],
  "progress": [
    "Validating the target URL",
    "Starting approved tool site-crawler (up to 5 pages, 2 link levels, 90s limit)",
    "Crawler exited with code 0 after 1.4s",
    "Finished: Task succeeded: the crawler read 1 page(s) and returned valid JSON."
  ],
  "result": {
    "task_status": "succeeded",
    "banner": {"kind": "success", "text": "Task succeeded: the crawler read 1 page(s) and returned valid JSON."},
    "summary": "Read 1 page(s) on example.com (about 25 words).\nFirst page title: \"Example Domain\".\nOpening text: This domain is for use in documentation examples...\nSummary method: extractive (built from the crawled text; no language model was used).",
    "answer": "Successfully crawled example.com across 1 page(s). The site represents 'Example Domain'...",
    "overview": {
      "represents": "Example Domain",
      "purpose": "This domain is established to be used for illustrative examples in documents...",
      "categories": ["Documentation & Standards"],
      "relevance": "The website content directly addresses topics and terms present in your query regarding example.com.",
      "relevance_status": "relevant"
    },
    "entities": [
      {
        "name": "Example Domain Authority",
        "type": "Organization",
        "description": "Subject of the website at example.com",
        "relevance": "Primary entity and subject of the crawled domain.",
        "source_url": "https://example.com"
      }
    ],
    "findings": {
      "findings": ["Retrieved 1 page(s) totaling 25 words from https://example.com."],
      "relationships": ["Content on this site focuses centrally on Example Domain."],
      "answers_question": "Directly answers the query using extracted text.",
      "limitations": ["Evidence is strictly bounded to 1 crawled page(s)."],
      "unsupported_conclusions": ["Cannot verify claims not published directly within crawled pages."]
    },
    "evidence": [
      {
        "claim": "Primary content of Example Domain",
        "source_url": "https://example.com",
        "supporting_text": "This domain is for use in documentation examples...",
        "confidence": "supported"
      }
    ],
    "intelligence": {
      "direct_answer": "...",
      "website_overview": {...},
      "entities": [...],
      "contextual_findings": {...},
      "evidence": [...],
      "generator": "deterministic",
      "model": null
    },
    "json": "{\n  \"task\": \"website_crawl\",\n  \"status\": \"succeeded\",\n  \"pages\": [...]\n}",
    "errors": [],
    "execution": {
      "tool": "site-crawler",
      "args": ["--url=https://example.com/", "--max-pages=5", "--max-depth=2", "--timeout=10", "--delay=0.3"],
      "exit_code": 0,
      "duration_s": 1.4,
      "timed_out": false,
      "cancelled": false,
      "stderr_tail": "[site_crawler] robots.txt: found\n[site_crawler] fetching page 1/5..."
    }
  }
}
```

---

### `POST /api/tasks/<id>/cancel`
Stops an active task and terminates its child subprocess tree.

**Response (200 OK):**
Returns the updated task view object with `status: "cancelled"`.

---

### `GET /api/tasks/<id>/export?format=json|csv|md`
Exports verified results for offline analysis or documentation.

**Supported Formats:**
- `format=json`: Application JSON document.
- `format=csv`: Tabular CSV of crawled pages, titles, word counts, and status codes.
- `format=md`: Formatted Markdown report.

---

## 3. Conversation Threads Endpoints

### `POST /api/threads`
Creates a new conversation thread. The thread provides isolated conversation history and durable crawl memory across server restarts.

**Request Body (optional):**
```json
{
  "title": "Optional Custom Thread Title"
}
```
*(An empty body `{}` creates a thread with an automatic title derived from its first task).*

**Response (201 Created):**
```json
{
  "id": "7b8f9e2d3c4b5a61728394a5b6c7d8e9",
  "title": "New Thread",
  "created_at": 1700000000,
  "updated_at": 1700000000
}
```

**Error Responses:**
- `413 Payload Too Large`: Request body exceeds 8192 bytes.
- `429 Too Many Requests`: Thread capacity limit reached (maximum 200 threads).

---

### `GET /api/threads`
Lists all active conversation threads ordered by most recently updated.

**Response (200 OK):**
```json
{
  "count": 2,
  "threads": [
    {
      "id": "7b8f9e2d3c4b5a61728394a5b6c7d8e9",
      "title": "example.com — Research",
      "created_at": 1700000000,
      "updated_at": 1700000120,
      "message_count": 3,
      "has_crawl_memory": true,
      "target_url": "https://example.com"
    }
  ]
}
```

---

### `GET /api/threads/<id>`
Retrieves full details for a conversation thread, including its chronological messages and active running task (if any).

**Response (200 OK):**
```json
{
  "id": "7b8f9e2d3c4b5a61728394a5b6c7d8e9",
  "title": "example.com — Research",
  "created_at": 1700000000,
  "updated_at": 1700000120,
  "has_crawl_memory": true,
  "target_url": "https://example.com",
  "host": "example.com",
  "active_task_id": null,
  "messages": [
    {
      "id": "m1a2b3c4...",
      "role": "user",
      "content": "Crawl https://example.com and summarize it",
      "task_id": "t1a2b3...",
      "created_at": 1700000001
    },
    {
      "id": "m2a3b4c5...",
      "role": "assistant",
      "content": "Example Domain serves as illustrative documentation...",
      "task_id": "t1a2b3...",
      "created_at": 1700000010
    }
  ]
}
```

**Error Responses:**
- `400 Bad Request`: Invalid thread ID format (must be 32 lowercase hex characters).
- `404 Not Found`: Thread not found.

---

### `DELETE /api/threads/<id>`
Permanently deletes a thread, its conversation messages, and associated crawl memory. If a task is currently executing in the thread, it is automatically cancelled.

**Response (200 OK):**
```json
{
  "deleted": true,
  "id": "7b8f9e2d3c4b5a61728394a5b6c7d8e9"
}
```

**Error Responses:**
- `400 Bad Request`: Invalid thread ID format.
- `404 Not Found`: Thread not found.

---

### `POST /api/threads/<id>/messages`
Submits a user prompt to a conversation thread. The engine validates the prompt, enforces concurrency guards, and routes the message based on thread context:
1. **URL present in prompt**: Routes as `crawl`, spawning an approved CLI subprocess to crawl the target.
2. **No URL, crawl memory present**: Routes as `followup`, answering directly from stored crawl evidence without spawning subprocesses or contacting the network.
3. **No URL, no crawl memory**: Routes as `needs_url`, returning an immediate advisory reply without queuing a task.

**Request Body:**
```json
{
  "prompt": "What domains or organizations are mentioned on this site?"
}
```

**Response (202 Accepted — for `crawl` and `followup`):**
```json
{
  "decision": "followup",
  "id": "t9876543210abcdef0123456789abcdef",
  "status": "queued",
  "task_type": "thread_followup",
  "interpretation": "Follow-up question about stored crawl of example.com: 'What domains or organizations are mentioned on this site?'",
  "notes": [
    "Answered from stored crawl of example.com. Subprocess execution bypassed."
  ]
}
```

**Response (200 OK — for `needs_url`):**
```json
{
  "decision": "needs_url",
  "reply": "I need a website URL to begin. For example: 'Crawl https://example.com and summarize it.'"
}
```

**Error Responses:**
- `400 Bad Request`: Empty prompt, prompt > 1000 characters, or invalid thread ID.
- `404 Not Found`: Thread not found.
- `409 Conflict`: Thread is busy executing another task. Concurrent tasks in the same thread are rejected.
- `413 Payload Too Large`: Request body exceeds 8192 bytes.
- `429 Too Many Requests`: Global task queue capacity reached.
