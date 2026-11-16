# MAI-Chat — System Architecture

**BuildSprint Round 2 · CLI-Driven AI Execution Platform**

---

## 1. Architectural Philosophy

MAI-Chat is designed around one core principle: **Deterministic, verified CLI tool orchestration driven by natural language.**

Modern AI chat interfaces often suffer from:
1. **Hallucination:** An LLM generates plausible text claiming it visited a website or ran a tool without actually executing it.
2. **Security Vulnerabilities:** Naive execution layers pass user-prompted text directly into shell commands (`os.system` or `subprocess.Popen(..., shell=True)`), leading directly to remote code execution (RCE).
3. **State Volatility:** Unbounded execution threads, memory blowouts from unbounded stdout buffers, and loss of state on server crashes.

MAI-Chat solves these issues through a strictly bounded, isolated, and audited execution pipeline:

```
┌────────────────────────────────────────────────────────────────────────┐
│                          User Web Workspace                            │
│    Responsive Chat UI · 10-Stage Pipeline Visualizer · History Drawer  │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ HTTP / REST JSON API
┌───────────────────────────────────▼────────────────────────────────────┐
│                  Enhanced HTTP Server (app/server.py)                  │
│    Rate Limiting · Strict CSP Security Headers · JSON Export Endpoints │
│    Coordinated Graceful Shutdown · Startup Safety Checks (Loopback)    │
└─────────────────┬──────────────────────────────────┬───────────────────┘
                  │                                  │
┌─────────────────▼────────────────┐   ┌─────────────▼───────────────────┐
│     Intent Engine (app/intent.py)│   │  Persistence (app/database.py)  │
│  - Deterministic Rule Fallback   │   │  - SQLite Database (WAL mode)   │
│  - Optional Structured LLM Plan  │   │  - Versioned Database Migration │
│  - Schema Field Discovery        │   │  - Stage Timeline Persistence   │
└─────────────────┬────────────────┘   └─────────────▲───────────────────┘
                  │                                  │
┌─────────────────▼──────────────────────────────────┴───────────────────┐
│               Task Manager & Worker Pool (app/tasks.py)                │
│    - Atomic Queue Admission (put_nowait, strict bounds, zero overflow) │
│    - Thread Pool Concurrency Limits (MAI_MAX_WORKERS)                  │
│    - Real-time 10-Stage Event Tracking & History Restoration           │
│    - Coordinated Graceful Shutdown (drains queue, terminates procs)    │
└─────────────────┬──────────────────────────────────────────────────────┘
                  │
┌─────────────────▼──────────────────────────────────────────────────────┐
│            Approved CLI Registry & Runner (app/runner.py)              │
│    - Approved Tool Registry & Strict Flag Validation (app/registry.py) │
│    - Subprocess with shell=False & Minimal Environment Allowlist       │
│    - Streaming Incremental Output Capture (In-flight Byte Limits)      │
│    - Cross-Platform Process-Tree Kill (Windows & POSIX)                │
└─────────────────┬──────────────────────────────────────────────────────┘
                  │ sys.executable -I tools/site_crawler.py [args]
┌─────────────────▼──────────────────────────────────────────────────────┐
│          Hardened Crawler Subprocess (tools/site_crawler.py)           │
│    - IP-Pinned Connection Layer (DNS Rebinding TOCTOU Elimination)     │
│    - Anti-SSRF Redirect Interceptor (Same-Host Boundary & Ports 80/443)│
│    - RFC 9309-Compliant Robots.txt Handling (5xx/Network Fail Disallow)│
│    - Extraction: H1-H6, Canonical, Meta, Text, Links, Word Counts      │
│    - Structured Schema Extraction with Honest Null Values              │
└─────────────────┬──────────────────────────────────────────────────────┘
                  │ stdout: Valid JSON Document  |  stderr: Logs & Progress
┌─────────────────▼──────────────────────────────────────────────────────┐
│                Report & Verification (app/report.py)                   │
│    - Exit Code Verification & Strict JSON Schema Parsing               │
│    - Synthesizes Verifiable Extractive Summary                         │
│    - Guarantees Syntactically Valid JSON Delivery                      │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Core Components

### 2.1 Intent Engine (`app/intent.py`)
- **Deterministic Rule Fallback:** Evaluates prompt using regex patterns for URL discovery, page limit clamping (1–10), and crawl keywords. Works offline without credentials.
- **Optional LLM Planner:** When `LLM_API_KEY` is present, queries an OpenAI-compatible completion API with strict system constraints.
- **Strict Plan Validation:** Every plan is verified by `TaskPlan`. Any plan specifying unauthorized task types or arbitrary commands is immediately rejected, falling back to deterministic rules.
- **Extraction Schema Parsing:** Detects requested target fields (e.g. company name, services, contact information) and passes them down as structured extraction targets.

### 2.2 Task Lifecycle & Bounded Queue (`app/tasks.py`)
- **Atomic Queue Admission:** Queue admission is performed atomically under lock using `self._queue.put_nowait()`. When full, immediately raises `QueueFullError` (HTTP 429) without corrupting memory or database state.
- **Worker Pool Limits:** Fixed number of worker threads (default 3, configurable via `MAI_MAX_WORKERS`) pulling from a bounded queue (`MAI_QUEUE_CAPACITY`, default 50).
- **10-Stage Real Execution Pipeline:**
  1. `request_received`: Validated request enqueued with unique UUID.
  2. `intent_interpreted`: Natural language converted to structured plan.
  3. `plan_prepared`: Tool arguments and constraints prepared.
  4. `input_validated`: Destination URL verified against SSRF and private IP policy.
  5. `cli_selected`: Tool verified in `ApprovedTool` registry.
  6. `process_started`: Subprocess launched in isolated environment (`shell=False`).
  7. `execution_running`: Tool actively executing real work.
  8. `output_captured`: Streaming reader captures stdout/stderr up to byte limits.
  9. `result_validated`: Output JSON document verified field-by-field.
  10. `task_completed`: Extractive summary synthesized and delivered.
- **Historical Stage Restoration:** Historical tasks retrieved from SQLite restore their actual completed stages from `stages_json`; completed tasks never display pending stages.
- **Coordinated Graceful Shutdown:** `TaskManager.shutdown()` stops submissions, drains unstarted tasks marking them cancelled, cancels running subprocesses, terminates process trees, joins worker threads, and cleanly closes SQLite connections.

### 2.3 Approved CLI Registry & Runner (`app/registry.py` & `app/runner.py`)
- **Tool Registry:** Central catalog (`app/registry.py`) holding tool metadata, script paths, and explicit argument specifications (`ArgumentSpec`). Enforces `required=True`, rejects missing values (`--flag=`), rejects duplicate flags, validates types and ranges, rejects unapproved flags and positional arguments, and verifies script paths reside in `tools/`.
- **Subprocess Isolation:** Processes run with `sys.executable -I` (isolated mode), `shell=False`, and a stripped environment allowlist (`CHILD_ENV_ALLOWLIST`). API keys and testing bypass flags are strictly excluded from production child processes.
- **Streaming Output Capture:** Standard pipes are drained chunk-by-chunk by daemon reader threads. Maximum output sizes are enforced *in-flight*, preventing memory exhaustion.
- **Cross-Platform Process Tree Termination:**
  - On Windows: Uses `taskkill /F /T /PID <pid>` with `CREATE_NEW_PROCESS_GROUP` to ensure no orphaned child processes remain.
  - On POSIX: Uses `os.killpg(os.getpgid(proc.pid), signal.SIGKILL)` with session detachment.

### 2.4 Persistent Storage & Versioned Migrations (`app/database.py`)
- **SQLite Engine:** Persistent SQLite database configured with Write-Ahead Logging (`WAL`) and synchronous normal for high throughput and durability.
- **Versioned Migrations:** Real migration framework using a `schema_versions` table and versioned migration functions (`v1`: base schema; `v2`: `stages_json` column).
- **Honest Restart Recovery:** On startup, `recover_interrupted_tasks()` scans for tasks left in `queued` or `running` state and updates them to `failed` with an explicit reason ("Server restarted while task was in progress; marked as interrupted.").

### 2.5 Hardened Crawler CLI (`tools/site_crawler.py`)
- **Zero Third-Party Dependencies:** Relies purely on the Python standard library.
- **IP-Pinned Connection Layer:** Single DNS resolution per attempt; connects directly to validated IP addresses; verifies connected peer with `sock.getpeername()`; preserves TLS SNI, Host headers, and certificate verification; rejects mixed public/private DNS answers; explicitly disables ambient proxy bypass.
- **Anti-SSRF Redirect Interceptor:** Custom `AntiSsrflRedirectHandler` validates every HTTP 301/302/307/308 redirect target IP, port (80/443 only), and host before the connection is established.
- **RFC 9309-Compliant Robots.txt Handling:** 2xx parses rules; 404/410 allows crawling; 401/403 disallows crawling; 5xx, network failures, and blocked redirects strictly disallow crawling (never fails open).
- **Comprehensive Content Extraction:** Extracts page title, meta description, meta keywords, canonical tags, all headings H1–H6, word counts, and separates internal vs. external links.
- **Structured Schema Extractor:** Evaluates pages against user-requested fields, using honest null representations (`null` / `(not found on page)`) rather than fabricating data.

### 2.6 Evidence-Grounded Website Intelligence (`app/intelligence.py`)
- **Post-Execution Separation:** Runs after crawler output is parsed; partial page results may also be analyzed. The crawler has zero dependency on AI reasoning.
- **Untrusted Input Handling:** Crawled page contents are wrapped in `<crawled_evidence>` delimiters and identified as untrusted. The intelligence module has no shell/CLI execution capability. Prompt delimiters are a mitigation, not a guarantee of model-level prompt-injection resistance.
- **Evidence Validation:** LLM output must pass schema checks, use URLs belonging to crawled pages, provide supporting quotes found in the cited page text, and use entity names present in page text or extracted structured data. Lexical checks reduce obvious fabrication but do not prove semantic entailment.\n- **Dual-Mode Synthesis:**
  - *Offline Deterministic Engine:* Operates without an external API key. Uses rule-based sentence alignment, keyword matching, entity type heuristics, and heading taxonomies.
  - *Optional LLM Synthesis:* When `LLM_API_KEY` is present, formats a bounded prompt to generate natural-language answers and entity classifications.
- **Fail-Safe Circuit Breaker:** Bounded by a 10.0-second network timeout and 32KB payload cap. If the LLM call times out, encounters a network error, or produces an invalid JSON schema, execution immediately falls back to the deterministic analysis without failing the crawl or discarding data.
- **Transparent Grounding:** Categorizes all confidence strictly into `"supported"`, `"partially supported"`, or `"not found in the crawled content"`. Hallucinated percentage scores and calibrated probabilities are forbidden.

### 2.7 Conversation Threads & Crawl Memory (`app/database.py`, `app/intent.py`, `app/tasks.py`)
- **Durable Conversation State:** Threads and messages persist in SQLite (migration `v3`), enforced by `PRAGMA foreign_keys = ON`. Thread deletion cascades to delete all messages.
- **Thread Concurrency Guard:** A thread can have at most one executing task at a time. If a user or client submits a task while a task is running in that thread, the request is rejected with HTTP 409 (`ThreadBusyError`).
- **Bounded Resource Limits:**
  - Maximum 200 threads per server instance (HTTP 429 if capacity is reached).
  - Crawl memory capped at 2 MB JSON payload per thread.
  - Conversation history stored in thread context is capped at 20 turns, each at 1000 characters maximum.
- **Contextual Message Routing:**
  - When a message contains a URL, it is routed as a new `crawl` operation.
  - When a message contains no URL and the thread has prior `crawl_doc` memory, it is routed as `followup`.
  - When a message contains no URL and the thread has no prior crawl memory, it is answered with an advisory `needs_url` prompt without spawning any background task.
- **Subprocess & Network Isolation for Follow-ups:**
  - Follow-up questions never launch child processes or make outbound network requests.
  - They execute in-process via `generate_intelligence(question, crawl_doc)` against the verified, stored crawl document.
  - Execution stages that are not required (`input_validated`, `cli_selected`, `process_started`, `execution_running`, `output_captured`) are explicitly recorded as `"skipped"` ("Not needed: answered from stored crawl").
  - The UI and API responses provide honest attribution: "Answered from stored crawl of {host}. No new crawl."

---

## 3. Tool-Extension Architecture

Adding new approved CLI tools requires zero changes to the orchestration engine:
1. Define tool constraints in `app/registry.py` using `ApprovedTool` and `ArgumentSpec`.
2. Register the tool via `register_tool(new_tool)`.
3. Provide an argument builder in `app/tasks.py`.
4. Validate outputs using `app/report.py`.
