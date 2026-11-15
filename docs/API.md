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
