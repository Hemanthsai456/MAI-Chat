# MAI-Chat — Demonstration Guide & Video Presentation Script

**BuildSprint Round 2 · Demonstration & Verification Guide**

---

## 1. Quick Demonstration Steps

### Step 1: Start the Server
```bash
python -m app.server
```
Navigate to `http://127.0.0.1:8000`.

### Step 2: The Mandatory Demonstration Task
Enter the exact prompt:
```text
Crawl https://example.com, extract the important information, give me a summary, and return the extracted data in JSON format.
```
Or click the primary example chip: **★ Mandatory Demo (example.com)**.

### What to Observe:
1. **Interpretation & Plan:** The chat displays the parsed target URL, page limit, and execution steps.
2. **10-Stage Real Execution Pipeline:** Watch the stage visualizer progress through:
   - `Request received` → `Intent interpreted` → `Plan prepared` → `Input validated` → `Approved CLI selected` → `CLI process started` → `Execution running` → `Output captured` → `Result validated` → `Task completed`.
3. **Verified Summary:** A clean, accurate extractive summary synthesized directly from crawled text.
4. **Structured JSON Output:** Formatted JSON payload containing all page records, titles, and crawl statistics.
5. **Execution Details Accordion:** Click *Execution Details & Verified Logs* to verify the real exit code (`0`), tool name (`site-crawler`), CLI duration, and captured stderr trail.
6. **Export Actions:** Click **Download JSON**, **Download Markdown**, or **Export CSV** to verify instant file generation.

---

## 2. Edge Case Demonstrations

### Failure Scenario 1: Unreachable Host
Prompt:
```text
Summarise https://this-domain-does-not-exist-buildsprint.com
```
- **Result:** The system fails at the `input_validated` stage. A red error banner is displayed: "could not resolve 'this-domain-does-not-exist-buildsprint.com'; the site may not exist or may be offline. The crawler was not started."
- **Integrity Check:** Zero subprocesses run. No summary or JSON is fabricated.

### Failure Scenario 2: SSRF Attack Block
Prompt:
```text
Crawl http://169.254.169.254/latest/meta-data
```
- **Result:** URL validation rejects the request immediately: "the address points to a private or internal network, which is not crawled."

### Scenario 3: Task Cancellation
Prompt:
```text
Crawl https://example.com up to 10 pages and summarise it.
```
Click **Cancel Execution** while the crawler is active.
- **Result:** Process group is terminated immediately without orphaned background processes. The banner updates to "Cancelled by you."

### Scenario 4: Structured Schema Extraction
Prompt:
```text
Visit https://example.com and extract its company name, services, contact information in JSON.
```
- **Result:** Extracts `"company_name": "Example Domain"`, while `"services"` and `"contact_information"` are set to honest `null` / `(not found on page)`.

---

## 3. Video Presentation Script (4–7 Minutes)

### [0:00 – 0:45] Problem Introduction
> **Visual:** Opening title slide showing "MAI-Chat: Reliable CLI-Driven AI Execution" followed by the browser showing the chat interface.  
> **Speaker:**  
> "Welcome to MAI-Chat, built for BuildSprint Round 2. Today, most AI chat applications respond with generated text that merely simulates tool execution. When asked to crawl a website or execute an operation, they often hallucinate facts or, even worse, run unsanitized shell commands that introduce severe remote code execution vulnerabilities.  
> MAI-Chat takes a completely different, production-grade approach: natural-language instructions are translated into strictly validated execution plans, executed as isolated CLI subprocesses, verified byte-by-byte, and delivered through an interactive chat interface."

### [0:45 – 1:45] System Architecture
> **Visual:** Architecture diagram from `docs/ARCHITECTURE.md`.  
> **Speaker:**  
> "Here is our architecture. When a user submits a prompt, it enters our dual-layer Intent Engine—featuring an optional structured LLM planner with a deterministic, zero-dependency rule fallback.  
> The task is passed to our bounded Task Queue and Worker Pool, which prevents server denial-of-service.  
> The approved tool runner verifies the tool in our Approved Registry, creates an isolated subprocess with `shell=False`, and streams stdout and stderr incrementally.  
> Every task is recorded in SQLite for crash resilience, and our reporting engine guarantees that only genuine, validated results are returned to the user."

### [1:45 – 2:45] The Mandatory Demonstration
> **Visual:** Switching to the browser at `http://127.0.0.1:8000`. Clicking the 'Mandatory Demo' chip.  
> **Speaker:**  
> "Let's perform the mandatory BuildSprint demonstration:  
> 'Crawl https://example.com, extract the important information, give me a summary, and return the extracted data in JSON format.'  
> Notice what happens immediately:  
> The backend parses the intent and generates a 5-step plan.  
> Look at our real-time 10-stage execution visualizer: it tracks real backend events—from URL validation and tool selection, to subprocess launch and streaming output capture."

### [2:45 – 4:00] Verification of Live Results & Exports
> **Visual:** Scrolling through the green success banner, the summary, and the JSON code block. Opening the Execution Details accordion.  
> **Speaker:**  
> "The crawl has finished.  
> Here is the verified extractive summary: it read 1 page on example.com with 25 words, opened with the exact documentation disclaimer, and clearly states: 'Summary method: extractive; no language model was used'.  
> Below is the valid structured JSON output.  
> In the Execution Details, we can see the exact tool executed—`site-crawler`—with an exit code of 0, running in 1.4 seconds, along with the actual stderr stream from the crawler.  
> With one click, users can Copy JSON, Download JSON, Download a formatted Markdown report, or Export CSV."

### [4:00 – 5:00] Failure Handling & Security Controls
> **Visual:** Clicking the 'Unreachable Host' example, then demonstrating task history.  
> **Speaker:**  
> "Now let's verify honest failure handling. I will request an unreachable domain:  
> The system catches the DNS resolution failure during validation, stops immediately without launching the CLI, and displays an honest red banner explaining the failure.  
> Furthermore, if an attacker attempts an SSRF against internal IP addresses or cloud metadata endpoints like 169.254.169.254, our Anti-SSRF defenses reject the destination before any outbound connection can occur.  
> Even if an off-site redirect is attempted, our custom redirect interceptor in `site_crawler.py` blocks it at the network layer."

### [5:00 – 6:00] Persistence, Recovery & Conclusion
> **Visual:** Opening the Task History drawer, showing past runs and theme toggle.  
> **Speaker:**  
> "Finally, let's open the Persistent Task History drawer. Every task, duration, and status is durably stored in SQLite. If the server were to crash or restart, our startup recovery engine automatically inspects interrupted jobs and flags them as interrupted rather than losing track of them.  
> In summary, MAI-Chat is reliable, completely sandboxed, cross-platform, and fully verified by our automated test suite. Thank you."
