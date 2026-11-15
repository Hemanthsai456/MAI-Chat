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
└─────────────────┬──────────────────────────────────┬───────────────────┘
                  │                                  │
┌─────────────────▼────────────────┐   ┌─────────────▼───────────────────┐
│     Intent Engine (app/intent.py)│   │  Persistence (app/database.py)  │
│  - Deterministic Rule Fallback   │   │  - SQLite Database (WAL mode)   │
│  - Optional Structured LLM Plan  │   │  - Automatic Schema Migration   │
│  - Schema Field Discovery        │   │  - Honest Crash Recovery Engine │
└─────────────────┬────────────────┘   └─────────────▲───────────────────┘
                  │                                  │
┌─────────────────▼──────────────────────────────────┴───────────────────┐
│               Task Manager & Worker Pool (app/tasks.py)                │
│    - Bounded Task Queue (rejects queue flooding)                       │
│    - Thread Pool Concurrency Limits                                    │
│    - Real-time 10-Stage Event Tracking                                 │
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
│    - Strict Same-Host Crawl Boundary & Robots.txt Enforcement          │
│    - Anti-SSRF Redirect Interceptor with Pre-Connection IP Checking    │
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
- **Bounded Worker Pool:** Fixed number of worker threads (default 3, configurable via `MAI_MAX_WORKERS`) pulling from a bounded queue (`MAI_QUEUE_CAPACITY`, default 50).
- **Concurrency Protection:** Protects server resources from thread exhaustion and process storms.
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

### 2.3 Approved CLI Registry & Runner (`app/registry.py` & `app/runner.py`)
- **Tool Registry:** Central catalog (`app/registry.py`) holding tool metadata, script paths, and explicit argument specifications (`ArgumentSpec`).
- **Subprocess Isolation:** Processes run with `sys.executable -I` (isolated mode), `shell=False`, and a stripped environment allowlist.
- **Streaming Output Capture:** Standard pipes are drained chunk-by-chunk by daemon reader threads. Maximum output sizes are enforced *in-flight*, preventing memory exhaustion.
- **Cross-Platform Process Tree Termination:**
  - On Windows: Uses `taskkill /F /T /PID <pid>` with `CREATE_NEW_PROCESS_GROUP` to ensure no orphaned child processes remain.
  - On POSIX: Uses `os.killpg(os.getpgid(proc.pid), signal.SIGKILL)` with session detachment.

### 2.4 Persistent Storage & Recovery (`app/database.py`)
- **SQLite Engine:** Persistent SQLite database configured with Write-Ahead Logging (`WAL`) and synchronous normal for high throughput and durability.
- **Honest Restart Recovery:** On startup, `recover_interrupted_tasks()` scans for tasks left in `queued` or `running` state and updates them to `failed` with an explicit reason ("Interrupted by server restart before completion").

### 2.5 Hardened Crawler CLI (`tools/site_crawler.py`)
- **Zero Third-Party Dependencies:** Relies purely on the Python standard library.
- **Anti-SSRF Redirect Interceptor:** Custom `AntiSsrflRedirectHandler` validates every HTTP 301/302/307/308 redirect target IP and host before the connection is established.
- **Comprehensive Content Extraction:** Extracts page title, meta description, meta keywords, canonical tags, all headings H1–H6, word counts, and separates internal vs. external links.
- **Structured Schema Extractor:** Evaluates pages against user-requested fields, using honest null representations (`null` / `(not found on page)`) rather than fabricating data.

---

## 3. Tool-Extension Architecture

Adding new approved CLI tools requires zero changes to the orchestration engine:
1. Define tool constraints in `app/registry.py` using `ApprovedTool` and `ArgumentSpec`.
2. Register the tool via `register_tool(new_tool)`.
3. Provide an argument builder in `app/tasks.py`.
4. Validate outputs using `app/report.py`.
