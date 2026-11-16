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
3. **Evidence-Grounded Intelligence Cards:**
   - **Direct Answer:** A natural-language answer generated from crawled text; LLM output is subject to conservative URL, quote, and lexical checks, not full semantic proof.
   - **Website Overview:** Explains what the site represents, its purpose, categories, and relevance to the query.
   - **Relevant Entities:** Structured cards showing heuristic entity candidates from crawler metadata, structured fields, and a limited technology vocabulary, with source links.
   - **Contextual Findings:** Crawl-derived observations, question relevance, limitations, and unsupported conclusions. Relationship claims are omitted unless explicitly supported.
   - **Evidence & Limitations:** Bounded citations with supporting quotes and transparent confidence tags (`supported`, `partially supported`, `not found in the crawled content`).
4. **Verified Summary:** A clean, accurate extractive summary synthesized directly from crawled text.
5. **Structured JSON Output:** Formatted JSON payload containing all page records, titles, and crawl statistics.
6. **Execution Details Accordion:** Click *Execution Details & Verified Logs* to verify the real exit code (`0`), tool name (`site-crawler`), CLI duration, and captured stderr trail.
7. **Export Actions:** Click **Download JSON**, **Download Markdown**, or **Export CSV** to verify instant file generation.

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

### [2:45 – 3:45] Verification of Live Results, Grounded Intelligence & Exports
> **Visual:** Scrolling through the green success banner, the Evidence-Grounded Intelligence cards (Direct Answer, Website Overview, Entities, Findings, Evidence), the summary, and the JSON code block. Opening the Execution Details accordion.  
> **Speaker:**  
> "The crawl has finished.  
> Notice our Evidence-Grounded Website Intelligence layer:  
> First, it generates a natural-language direct answer targeted to the user's question.  
> Second, it provides a structured Website Overview—explaining what the site represents, its primary purpose, categories, and relevance.  
> Third, it reports heuristic entity candidates based on extracted metadata and structured fields, with source URLs.  
> Fourth, it provides contextual crawl findings, limitations, and transparent evidence citations with labels like 'supported' or 'not found in the crawled content'—strictly avoiding hallucinated percentage scores.  
> Below that is our verified extractive summary and the valid structured JSON output.  
> In the Execution Details, we can see the exact tool executed—`site-crawler`—with an exit code of 0, along with the actual stderr stream from the crawler.  
> With one click, users can Copy JSON, Download JSON, Download a formatted Markdown report, or Export CSV."

### [3:45 – 4:30] Interactive Follow-up Questions (Thread Memory)
> **Visual:** Typing a follow-up question in the same chat: `"What is this domain used for?"` and hitting Enter.  
> **Speaker:**  
> "Now, notice one of our standout features: Conversation Threads and Stored Crawl Memory.  
> I can ask a natural follow-up question without re-specifying the URL.  
> The backend recognizes that this thread already has verified crawl memory. It routes the question as a follow-up, skips launching any new CLI subprocess, and avoids making duplicate network requests.  
> The direct answer is synthesized instantly from stored memory, with full citations and focused JSON.  
> If I ask for something not present on the site, such as a contact phone number, the system honestly states that the information is absent from the crawled pages, rather than hallucinating."

### [4:30 – 5:15] Failure Handling & Security Controls
> **Visual:** Clicking the 'Unreachable Host' example, then demonstrating task history.  
> **Speaker:**  
> "Now let's verify honest failure handling. If I request an unreachable domain:  
> The system catches the DNS resolution failure during validation, stops immediately without launching the CLI, and displays an honest red banner explaining the failure.  
> Furthermore, if an attacker attempts an SSRF against internal IP addresses or cloud metadata endpoints like 169.254.169.254, our Anti-SSRF defenses reject the destination before any outbound connection can occur.  
> Our connection layer resolves DNS once, validates all IP addresses against private ranges, and pins the socket connection to prevent DNS rebinding."

### [5:15 – 6:00] Persistence, Recovery & Conclusion
> **Visual:** Showing the Threads sidebar, switching between chats, and toggling dark/light theme.  
> **Speaker:**  
> "Finally, let's look at the sidebar. Every thread, conversation history, and crawl memory is durably persisted in SQLite with Write-Ahead Logging. Even if the server restarts, everything is restored cleanly.  
> In summary, MAI-Chat combines constrained CLI orchestration, crawler-side network protections, persistent thread memory, and evidence-checked website intelligence. Thank you."
