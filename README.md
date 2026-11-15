# MAI-Chat — CLI-Driven AI Execution Platform

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests: 65 Passed (1 Skipped)](https://img.shields.io/badge/tests-65%20passed%20%7C%201%20skipped-brightgreen.svg)](tests/)
[![Security: Hardened](https://img.shields.io/badge/security-Anti--SSRF%20%7C%20IP--Pinned%20%7C%20RFC%209309-red.svg)](docs/SECURITY.md)

**MAI-Chat** is a hardened, audited, and extensible CLI-driven AI execution platform developed for **BuildSprint Round 2**. 

It converts free-form natural language instructions into strictly validated execution plans, invokes approved CLI tools in isolated subprocesses (`shell=False`), captures streaming outputs under strict memory bounds, validates structured data, and delivers verifiable results through an interactive chat interface.

---

## 1. Project Overview

In traditional AI chat implementations, conversational assistants generate simulated text answers without performing verifiable work. When asked to crawl a website or execute an operation, they hallucinate content or resort to dangerous shell commands that risk remote code execution (RCE).

**MAI-Chat provides an audited, end-to-end engineering solution:**
- Natural language instructions are translated into deterministic execution plans.
- Only approved CLI tools registered in a central catalog can run.
- Subprocesses execute with explicit argument lists, `shell=False`, and stripped environments.
- Stdout and stderr are captured incrementally with bounded in-flight memory.
- Results are verified against expected JSON schemas before delivery.
- Persistent SQLite storage guarantees durability across server restarts.

---

## 2. Key Features

- **Approved CLI Tool Registry:** Modular tool registry (`app/registry.py`) with parameter specifications, type checking, boundary limits, and strict schema validation.
- **Dual-Layer Intent Engine:** Deterministic rule-based engine (100% offline, zero-dependency) with an optional structured LLM planner interface.
- **Real-Time 10-Stage Pipeline Visualizer:** Interactive frontend timeline tracking real backend execution states without simulated progress.
- **Persistent Task History:** Durable SQLite storage (with WAL mode) supporting task browsing, inspection, versioned migrations, and restart recovery.
- **Atomic Bounded Queue & Worker Pool:** Fixed worker pool (`MAI_MAX_WORKERS`) and atomic bounded queue admission (`MAI_QUEUE_CAPACITY`, via `put_nowait`) preventing thread exhaustion, race conditions, and queue overflows.
- **Coordinated Graceful Shutdown:** `TaskManager.shutdown()` cancels running subprocess trees, drains unstarted tasks, joins workers, and closes database connections cleanly.
- **Streaming Bounded Output Capture:** Daemon stream readers cap stdout/stderr in-flight, preventing pipe deadlocks and memory flooding.
- **DNS Rebinding Prevention & Anti-SSRF:** IP-pinned socket connection layer (`PinnedHTTPConnection` / `PinnedHTTPSConnection`) resolving host once, verifying all IP addresses, and connecting directly to pinned IP. Rejects mixed DNS answers and disables ambient proxy bypass.
- **RFC 9309 Robots.txt Policy:** Full RFC 9309 compliance: 2xx parses rules, 404/410 allows crawling, 401/403 disallows crawling, and 5xx / network failures strictly disallow crawling (never fails open).
- **Cross-Platform Lifecycle Termination:** Clean process tree termination on both Windows (`taskkill /F /T`) and POSIX (`killpg`).
- **Export Formats:** One-click downloads for JSON payloads, formatted Markdown reports, and tabular CSV data.
- **Modern Responsive UI:** Clean vanilla web workspace with Dark and Light themes, accessibility controls, and keyboard navigation.

---

## 3. Architecture Diagram

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

## 4. Technology Stack

- **Runtime & Language:** Python 3.10+ (Standard Library: `urllib`, `sqlite3`, `subprocess`, `http.server`, `html.parser`, `threading`, `queue`).
- **Database:** SQLite 3 with Write-Ahead Logging (`WAL`).
- **Frontend:** Vanilla JavaScript (ES6+), CSS3 Variables design system, HTML5 (zero frameworks, zero build steps, strict CSP).
- **Tooling & Tests:** Python `unittest` framework with deterministic local fixture servers.

---

## 5. Execution Workflow: The 10 Discrete Stages

Every task advances through 10 auditable backend stages:
1. `request_received`: Validated request enqueued with unique UUID.
2. `intent_interpreted`: Request parsed into task parameters and constraints.
3. `plan_prepared`: Multi-step execution plan synthesized.
4. `input_validated`: Target URL evaluated against Anti-SSRF and port policies.
5. `cli_selected`: Tool checked against approved registry.
6. `process_started`: Subprocess launched in isolated environment (`shell=False`).
7. `execution_running`: Tool executes real work; progress streamed to stderr.
8. `output_captured`: Streaming reader drains pipes up to in-flight limits.
9. `result_validated`: Output JSON document parsed and validated field-by-field.
10. `task_completed`: Extractive summary generated and verified payload delivered.

---

## 6. Approved CLI Integration & Extension

The system uses `app/registry.py` to register authorized CLI tools. Adding a new tool is straightforward:

```python
from app.registry import ApprovedTool, ArgumentSpec, register_tool

register_tool(
    ApprovedTool(
        name="pdf-extractor",
        script=TOOLS_DIR / "pdf_extractor.py",
        description="Extracts metadata and text from local documents.",
        max_timeout_s=60.0,
        max_output_bytes=1_000_000,
        argument_specs={
            "--file": ArgumentSpec(flag="--file", required=True),
            "--max-pages": ArgumentSpec(flag="--max-pages", arg_type=int, min_value=1, max_value=50),
        },
    )
)
```

Unapproved tools or arbitrary shell commands submitted by users or language models are rejected prior to execution.

---

## 7. Security Controls & System Boundaries

Detailed threat models and audit reports are documented in [docs/SECURITY.md](docs/SECURITY.md).

### Implemented Controls:
- **Approved CLI Catalog & Schema Enforcement:** Only tools registered in `app/registry.py` can be executed. Flags, types, ranges, allowed values, and required parameters are strictly validated; positional arguments, duplicate flags, and unapproved parameters are rejected.
- **`shell=False` Execution:** All subprocesses are launched with structured argument lists (`argv`) and `shell=False`. Raw user inputs or model-generated commands are never passed to a system shell, eliminating shell injection vectors.
- **Restricted Environment Allowlist:** The child process receives an explicit minimal environment allowlist (`CHILD_ENV_ALLOWLIST`), stripping system secrets, API keys, and internal developer test flags.
- **Bounded Streaming Output Capture:** Daemon stream readers cap stdout/stderr incrementally while reading chunks, preventing pipe deadlocks and memory flooding.
- **Process-Tree Termination:** Subprocesses are terminated cleanly on timeout or cancellation without leaving orphaned processes (Windows: `taskkill /F /T /PID`; POSIX: `os.killpg`).
- **Network-Layer SSRF & DNS Rebinding Controls:** IP-pinned socket connections (`PinnedHTTPConnection` / `PinnedHTTPSConnection`) resolve hostnames once per attempt, validate all returned IPv4/IPv6 addresses, reject mixed public/private answers, verify connected peer addresses via `getpeername()`, preserve TLS SNI/host verification, and disable ambient proxies. RFC 9309 compliant `robots.txt` enforcement disallows crawling on 5xx or connection failures.
- **Strict Destination Restrictions:** Initial URLs, discovered links, and redirects enforce HTTP/HTTPS schemes only, standard ports 80/443 only, no embedded credentials, and same-host boundaries.

### Real System Boundaries & Limitations:
- **Subprocess Isolation vs. OS Sandboxing:** Execution takes place inside standard OS child processes with a minimal environment and working directory isolation. The application does **not** provide kernel-level containerization (e.g., Docker/cgroups), hypervisor virtualization (e.g., gVisor/Firecracker), or OS-level restricted security tokens (e.g., Windows AppContainer, Linux seccomp-bpf). Child tools execute with the permissions of the host user running the Python server.
- **Deployment Assumptions & Localhost Default:** The application defaults to binding strictly to `127.0.0.1`. It is designed for single-user local deployment or deployment behind an authenticated, TLS-terminating reverse proxy. It does not provide multi-tenant isolation.
- **Offline Testing Bypass:** `BUILDSPRINT_ALLOW_PRIVATE_TARGETS=1` is restricted to local offline testing on loopback; startup safety checks prevent the server from binding to non-loopback interfaces if enabled.

---

## 8. Configuration & Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `HOST` | `127.0.0.1` | Server bind host. Keep on localhost unless behind authenticated reverse-proxy. |
| `PORT` | `8000` | HTTP port. |
| `MAI_DB_PATH` | `data/mai_chat.db` | Location of persistent SQLite database. |
| `MAI_MAX_WORKERS` | `3` | Worker pool concurrency limit. |
| `MAI_QUEUE_CAPACITY` | `50` | Maximum queued tasks before returning HTTP 429. |
| `LLM_API_KEY` | *unset* | Optional API key for structured LLM planner (OpenAI-compatible). |
| `LLM_API_BASE` | `https://api.openai.com/v1` | Optional API base URL for LLM planner. |
| `LLM_MODEL` | `gpt-3.5-turbo` | Optional model name for LLM planner. |
| `BUILDSPRINT_ALLOW_PRIVATE_TARGETS` | *unset* | **Offline testing only.** Allows crawling `127.0.0.1` fixture servers. |

---

## 9. Installation & Running

### Requirements
- **Python 3.10+** (standard library only; no pip dependencies required for base execution).
- (Optional) `certifi` for systems with non-standard root certificate stores.

### Quick Start
```bash
# 1. Clone repository
git clone https://github.com/example/MAI-Chat.git && cd MAI-Chat

# 2. Run the application
python -m app.server

# 3. Open browser
http://127.0.0.1:8000
```

---

## 10. Automated Testing

The automated test suite runs offline without requiring open internet access:

```bash
# Run all tests (66 tests covering execution, crawling, DNS rebinding, concurrency, and persistence)
python -m unittest discover -s tests -p "test_*.py"
```

To run the optional public internet verification against `example.com`:

```bash
python -c "import os, subprocess, sys; proc = subprocess.run([sys.executable, '-m', 'unittest', 'tests.test_app.LiveExampleDotComTest'], env=dict(os.environ, RUN_LIVE_TESTS='1'), check=True)"
```

---

## 11. Mandatory Demonstration Instructions

1. Start the server: `python -m app.server`.
2. Open `http://127.0.0.1:8000`.
3. Submit the prompt:
   ```text
   Crawl https://example.com, extract the important information, give me a summary, and return the extracted data in JSON format.
   ```
4. Observe:
   - The generated execution plan.
   - The 10-stage real-time progress visualizer.
   - The verified extractive summary.
   - The formatted structured JSON payload.
   - The execution metadata (exit code 0, tool duration, stderr output).
5. Click **Download JSON**, **Download Markdown**, or **Export CSV** to verify result export.

---

## 12. Additional Documentation

- [System Architecture](docs/ARCHITECTURE.md)
- [Security Architecture & Threat Model](docs/SECURITY.md)
- [REST API Reference](docs/API.md)
- [Demonstration Guide & Video Script](docs/DEMO_GUIDE.md)

---

## 13. AI Tools & External Services Disclosure

- **Development Assistant:** Antigravity / Gemini (Google DeepMind), utilized for codebase audit, refactoring, cross-platform stabilization, and test expansion.
- **Runtime Dependencies:** Zero external AI services required at runtime. Uses a deterministic rule-based intent parser and extractive summarization by default. An optional LLM planner is available when configured by the user via environment variables.
- **Target Network:** Public websites explicitly requested by the user. No telemetry, third-party analytics, or external CDNs.
