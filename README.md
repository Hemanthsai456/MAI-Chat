# Website Task Chat: chat-driven CLI execution

A chat app where a natural-language request ("Crawl https://example.com, summarise it and return JSON") is
turned into a controlled, validated call to an approved command-line tool. The backend runs the CLI as a real
process, captures its stdout, stderr and exit code, validates what it produced, and shows the verified result
in the chat. Failures, timeouts and cancellations are reported as such, never dressed up as success.

Built for BuildSprint Round 2 (CLI-driven AI chat execution).

## Quick start

Requirements: **Python 3.10+**. There are no third-party packages; everything is standard library.

```bash
git clone <this repo> buildsprint-cli-chat && cd buildsprint-cli-chat
python -m app.server            # then open http://127.0.0.1:8000
```

Run the tests (offline, about 5 seconds):

```bash
python -m unittest discover -s tests
```

Optional live test against the public internet:

```bash
RUN_LIVE_TESTS=1 python -m unittest discover -s tests -k LiveExample
```

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind address. Keep it on localhost unless you add authentication. |
| `PORT` | `8000` | HTTP port. |
| `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, `SSL_CERT_FILE` | unset | Passed through to the crawler so it works behind corporate proxies. |
| `BUILDSPRINT_ALLOW_PRIVATE_TARGETS` | unset | **Testing only.** Allows crawling `127.0.0.1` and other private addresses. Never set this on a public deployment. |

No API keys are needed. None are read, passed to the crawler, or logged.

## Demo walkthrough (mandatory use case)

1. Start the server and open the page.
2. Click **Crawl example.com**, or type:
   `Crawl https://example.com, extract the important information, give me a summary, and return the extracted data in JSON format.`
3. The chat shows the interpretation (target URL, page limit), then live progress lines from the backend
   ("Starting approved tool site-crawler…", "Crawler exited with code 0 after 0.9s").
4. The final message contains a green **Task succeeded** banner, a readable summary, the structured JSON
   (with a Copy button), and an *Execution details* panel with the exit code, duration and the crawler's stderr.
5. Edge cases to show:
   - **Unreachable site:** click *Unreachable site (error demo)*. The result is a red *Task failed* with the
     reason, and no summary or JSON is shown.
   - **Partial result:** a site where some links fail. The banner is amber and lists the failed URLs.
   - **Cancel:** start a crawl and press *Cancel task* while it runs. The process group is killed.
   - **Bad input:** "tell me a joke" is rejected with an explanation before anything runs.

## Architecture

```
 Browser (static/app.js)
    │  POST /api/tasks {prompt}            GET /api/tasks/<id> (poll every 0.8 s)
    ▼
 app/server.py ── HTTP layer, size limits, JSON API, security headers
    │
    ├─ app/intent.py      natural language → Intent(task_type, url, max_pages)   [rule-based]
    │                     rejects requests without a valid web address
    ├─ app/tasks.py       TaskManager: worker thread per task, progress log, cancel
    │     │
    │     ├─ app/validation.py   scheme, port, credentials, DNS, public-IP-only check
    │     ├─ app/runner.py       approved-tool registry; subprocess with argv list,
    │     │                      shell=False, env allowlist, timeout, process-group kill
    │     │          │
    │     │          ▼  python -I tools/site_crawler.py --url=… --max-pages=…
    │     │       tools/site_crawler.py   robots.txt-aware, same-host, bounded crawler
    │     │       → stdout: one JSON document;  stderr: progress;  exit code: 0/2/3/4
    │     │
    │     └─ app/report.py       validates the CLI output, decides success / partial /
    │                            failed, builds the summary and the JSON payload
    ▼
 chat message: banner, summary, JSON, errors, execution details
```

### Why this CLI

`site_crawler.py` is a small, purpose-built CLI in the project (`tools/`). It uses only the standard library,
so the demo runs anywhere with Python 3.10+ and no install step. It has the properties the brief asks for:

- **Bounded:** page limit (1 to 10), depth limit (0 to 3), per-request timeout, 1.5 MB body cap.
- **Polite:** honours `robots.txt` (including "disallow all" on 401/403), uses a clear User-Agent, and waits between pages.
- **Same-host only:** it follows links on the start host only, and drops redirects that leave that host.
- **Safe with untrusted content:** it parses HTML as text. It never executes scripts, and page text cannot change
  its arguments or the app's instructions.
- **Machine-readable:** one JSON document on stdout, with a `status` field and documented exit codes.

To use a different CLI (Playwright, Firecrawl, a headless-browser agent), add an entry to `APPROVED_TOOLS` in
`app/runner.py` and a builder in `app/tasks.py` (`crawler_args`). The validation and reporting layers stay the same.

### The intent layer

`app/intent.py` is deliberately rule-based. It extracts the URL, detects the task type (website crawl is the only
supported type), reads an optional page count, and explains its assumptions in the chat when it has to guess. This
makes the pipeline deterministic and testable without an API key. `parse_intent()` is the seam where a language
model could be used for intent classification or summarisation without changing anything else.

Summaries are **extractive**: built from the page titles, descriptions, headings and opening text the crawler
captured. The JSON says `"summary_method": "extractive"`, so nothing is presented as model output when it isn't.

## How results are verified

A result is shown as successful only when all of these hold:

1. The process exited with code `0`.
2. stdout is valid JSON with the expected structure (checked field by field in `app/report.py`).
3. The crawler reported status `success` or `partial`, and at least one page was read.

Anything else is reported as failed, with the reason taken from the crawler's own error list. Timeouts and
cancellations have their own statuses. The JSON shown to the user is re-parsed before it is sent.

## Error handling

| Situation | Where it is caught | What the user sees |
|---|---|---|
| No URL, empty or oversized prompt | `intent.py`, `server.py` (HTTP 400) | Explanation; nothing runs |
| Non-http(s) scheme, credentials, odd port | `validation.py` | "That address isn't usable: …" |
| `localhost`, private or internal IP (at run time) | `validation.py` | Failed; "crawler was not started" |
| Hostname does not resolve | `validation.py` | "could not resolve … offline" |
| `robots.txt` disallows the start page | crawler exit 3 | Failed; robots reason |
| Page returns 404/500, times out, resets | crawler, per page | Partial result, or failed if none readable |
| Off-site redirect, non-HTML content | crawler, per page | Listed under errors |
| CLI runs past its time limit | `runner.py` kills process group | "Stopped (timed out)"; no result |
| User cancels | `runner.py` kills process group | "Cancelled by you" |
| CLI prints non-JSON or malformed JSON | `report.py` | Failed; the exit code is shown |
| Unexpected exception in the backend | `tasks.py` | Generic failure; details only in the server log |

## Security controls

| Risk | Control |
|---|---|
| Shell injection from user or model text | No shell. Arguments are a list. The URL is validated before it becomes one argument. |
| Arbitrary command execution | Only tools in `APPROVED_TOOLS` can run. Tool name is a registry key, not free text. |
| SSRF into internal networks | Scheme and port allowlist; IP must be globally routable (checked for literal IPs and every DNS answer); crawler stays on the start host. |
| Runaway crawls | Page, depth, time and output-size caps, enforced in both the app and the CLI. |
| Secrets leaking to a child process | Environment allowlist. Only proxy and CA variables pass through. |
| Prompt-injection from web pages | Page content is data. It is parsed as text and never used to choose commands or instructions. |
| XSS in the chat | Output is rendered with `textContent` only. Strict CSP (`script-src 'self'`). |
| Oversized requests | 8 KB body limit, 1000-character prompt limit. |

## Limitations (honest list)

- **DNS rebinding:** the address is validated before the crawl, and the crawler then resolves it again. A
  hostile DNS server could answer differently the second time. Closing this needs the crawler to pin the
  validated IP, which is a planned improvement.
- **Redirects:** the crawler fetches the redirect target and then discards it if it is off-site. It does not
  block the connection in advance.
- **Platform:** process-group killing uses POSIX signals, so the app is for Linux and macOS.
- **State:** tasks live in memory. Restarting the server loses them.
- **Summaries are extractive.** They are accurate to the crawled text but not abstractive. An LLM step could be
  added at `report._summary_text`.
- **Intent parsing is rule-based.** Unusual phrasings still work if they contain a URL, but the classifier is simple.
- **Live site test:** the automated suite runs against a local fixture site. The `example.com` check is behind
  `RUN_LIVE_TESTS=1` because it needs open internet access.

## Project layout

```
app/
  server.py      HTTP server, JSON API, static files, security headers
  tasks.py       task lifecycle, worker threads, cancellation
  intent.py      natural-language → Intent
  validation.py  URL and network-target checks
  runner.py      approved-tool registry, subprocess runner
  report.py      output validation, verdicts, chat payload
static/          index.html, app.js, style.css (no build step, no CDN)
tools/
  site_crawler.py  the CLI that does the real work
tests/
  test_app.py    unit, CLI, runner and end-to-end tests
```

## AI tools and external services disclosure

- **Built with:** Claude (Anthropic), used as the coding assistant for design, implementation and tests.
- **Runtime:** no language model or external AI API is called. The "AI" layer is the rule-based intent parser,
  and summaries are extractive.
- **External services:** only the public websites the user asks to crawl. No accounts, keys or third-party APIs.
- **Verified in build sandbox:** all 27 offline tests pass (plus 1 live test that is skipped by default); the API flow was run end to end against a local site
  (success, partial, unreachable, invalid input, and loopback refusal). The live `example.com` run could not be
  verified from the build sandbox, which has no outbound access to that host. Run `RUN_LIVE_TESTS=1` to check it.

## Suggested explanation video outline (4 to 7 minutes)

1. **Problem and solution (0:00–0:45):** chat models answer with text; this app runs real tools.
2. **Architecture (0:45–1:45):** walk the diagram: server, intent, task manager, validation, runner, CLI, report.
3. **Prompt to CLI (1:45–2:45):** show `app/intent.py` and the exact argv that `app/tasks.py` builds. Point out `shell=False`.
4. **Live run (2:45–4:15):** enter the mandatory prompt. Show progress lines, the banner, the summary, the JSON, and the execution panel with the exit code.
5. **Edge case (4:15–5:00):** the unreachable-site demo, then explain how the verdict is decided.
6. **Security and close (5:00–6:00):** the controls table, limitations, and the AI disclosure.
