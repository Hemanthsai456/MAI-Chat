"use strict";

(() => {
  const chat = document.getElementById("chat");
  const form = document.getElementById("composer");
  const promptBox = document.getElementById("prompt");
  const sendBtn = document.getElementById("send");
  const charCount = document.getElementById("charCount");
  const historyToggle = document.getElementById("historyToggle");
  const historyDrawer = document.getElementById("historyDrawer");
  const closeHistory = document.getElementById("closeHistory");
  const drawerOverlay = document.getElementById("drawerOverlay");
  const historyList = document.getElementById("historyList");
  const refreshHistory = document.getElementById("refreshHistory");
  const themeToggle = document.getElementById("themeToggle");
  const threadsToggle = document.getElementById("threadsToggle");
  const threadSidebar = document.getElementById("threadSidebar");
  const closeSidebar = document.getElementById("closeSidebar");
  const sidebarOverlay = document.getElementById("sidebarOverlay");
  const newThreadBtn = document.getElementById("newThreadBtn");
  const threadsList = document.getElementById("threadsList");

  const POLL_MS = 600;
  const MAX_POLL_FAILURES = 12;
  const DONE_STATES = new Set(["succeeded", "partial", "failed", "timed_out", "cancelled"]);
  const STAGE_ICONS = {
    completed: "✓",
    active: "⟳",
    pending: "○",
    failed: "✕",
    skipped: "—",
  };
  const STATUS_LABELS = {
    queued: "Queued",
    running: "Running",
    succeeded: "Task Succeeded",
    partial: "Partial Result",
    failed: "Task Failed",
    timed_out: "Stopped (Timed Out)",
    cancelled: "Cancelled",
  };

  let activeThreadId = null;
  let activeThreadData = null;

  // Safe DOM builder using textContent exclusively (strictly no innerHTML)
  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function scrollToEnd() {
    window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
  }

  function formatTime(timestamp) {
    if (!timestamp) return "";
    const now = Math.floor(Date.now() / 1000);
    const diff = Math.max(0, now - timestamp);
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return new Date(timestamp * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
  }

  // Theme Management
  function initTheme() {
    const saved = localStorage.getItem("mai_theme");
    if (saved) {
      document.documentElement.setAttribute("data-theme", saved);
    } else {
      const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      document.documentElement.setAttribute("data-theme", prefersDark ? "dark" : "light");
    }
  }

  themeToggle.addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("mai_theme", next);
  });

  // API helper
  async function api(path, options) {
    try {
      const response = await fetch(path, options);
      let data = {};
      try {
        data = await response.json();
      } catch (_) {
        data = {};
      }
      return { ok: response.ok, status: response.status, data };
    } catch (err) {
      return { ok: false, status: 0, data: { error: err.message || "Network error" } };
    }
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function addUserMessage(text) {
    const row = make("div", "msg user");
    const bubble = make("div", "bubble", text);
    row.appendChild(bubble);
    chat.appendChild(row);
    scrollToEnd();
  }

  function createAssistantBubbleRaw() {
    const row = make("div", "msg bot");
    const bubble = make("div", "bubble");
    row.appendChild(bubble);
    chat.appendChild(row);
    scrollToEnd();
    return bubble;
  }

  function renderContextBanner(host) {
    const banner = make("div", "thread-context-banner");
    const icon = make("span", "", "🌐");
    const text = make("span", "");
    text.appendChild(document.createTextNode("Browsing context: "));
    text.appendChild(make("strong", "", host));
    text.appendChild(document.createTextNode(" · Stored crawl active for follow-up questions"));
    banner.append(icon, text);
    chat.appendChild(banner);
  }

  function addAssistantAdvisory(text) {
    const bubble = createAssistantBubbleRaw();
    bubble.appendChild(make("div", "banner warning", "Website URL Required"));
    bubble.appendChild(make("p", "direct-answer-text", text));
    scrollToEnd();
  }

  function renderStages(container, stages) {
    if (!stages || !stages.length) return;
    let wrapper = container.querySelector(".stages-wrapper");
    if (!wrapper) {
      wrapper = make("div", "stages-wrapper");
      const title = make("div", "stages-title");
      title.appendChild(make("span", "", "Real-Time Execution Pipeline"));
      title.appendChild(make("span", "muted", "10 Verified Stages"));
      wrapper.appendChild(title);
      const list = make("ul", "stages-list");
      wrapper.appendChild(list);
      container.appendChild(wrapper);
    }
    const list = wrapper.querySelector(".stages-list");
    list.replaceChildren();

    for (const s of stages) {
      const li = make("li", `stage-item ${s.status}`);
      const icon = make("span", "stage-icon", STAGE_ICONS[s.status] || "○");
      const label = make("span", "stage-label", s.label);
      li.append(icon, label);
      if (s.detail) {
        li.title = s.detail;
      }
      if (s.status === "skipped") {
        const badge = make("span", "stage-skipped-badge", "Skipped");
        li.appendChild(badge);
      }
      list.appendChild(li);
    }
  }

  function renderDirectAnswer(container, intel) {
    if (!intel || !intel.direct_answer) return;
    const card = make("div", "intel-card direct-answer");
    const header = make("div", "intel-header");
    const title = make("h4", "intel-title", "Direct Answer");
    header.appendChild(title);

    const pillText = intel.generator === "llm" ? `AI Synthesis (${intel.model || "LLM"})` : "Deterministic Grounded";
    const pill = make("span", "intel-pill grounded", pillText);
    header.appendChild(pill);
    card.appendChild(header);

    const p = make("p", "direct-answer-text", intel.direct_answer);
    card.appendChild(p);
    container.appendChild(card);
  }

  function renderWebsiteOverview(container, overview) {
    if (!overview) return;
    const card = make("div", "intel-card website-overview");
    const header = make("div", "intel-header");
    const title = make("h4", "intel-title", "Website Overview");
    header.appendChild(title);

    if (overview.relevance_status) {
      const relLabel = overview.relevance_status.replace(/_/g, " ");
      const relPill = make("span", `intel-pill ${overview.relevance_status}`, relLabel);
      header.appendChild(relPill);
    }
    card.appendChild(header);

    const grid = make("div", "overview-grid");

    const repField = make("div", "overview-field");
    repField.appendChild(make("span", "overview-label", "Represents"));
    repField.appendChild(make("span", "overview-val", overview.represents || "N/A"));
    grid.appendChild(repField);

    const purpField = make("div", "overview-field");
    purpField.appendChild(make("span", "overview-label", "Primary Purpose"));
    purpField.appendChild(make("span", "overview-val", overview.purpose || "N/A"));
    grid.appendChild(purpField);

    const relField = make("div", "overview-field");
    relField.appendChild(make("span", "overview-label", "Relevance to Question"));
    relField.appendChild(make("span", "overview-val", overview.relevance || "N/A"));
    grid.appendChild(relField);

    if (overview.categories && overview.categories.length) {
      const catField = make("div", "overview-field");
      catField.appendChild(make("span", "overview-label", "Content Categories"));
      const catWrap = make("div", "categories-wrap");
      for (const cat of overview.categories) {
        catWrap.appendChild(make("span", "category-tag", cat));
      }
      catField.appendChild(catWrap);
      grid.appendChild(catField);
    }

    card.appendChild(grid);
    container.appendChild(card);
  }

  function renderEntities(container, entities) {
    if (!entities || !entities.length) return;
    const card = make("div", "intel-card entities-section");
    const header = make("div", "intel-header");
    const title = make("h4", "intel-title", `Relevant Entities (${entities.length})`);
    header.appendChild(title);
    card.appendChild(header);

    const grid = make("div", "entities-grid");
    for (const ent of entities) {
      const eCard = make("div", "entity-card");
      const eHeader = make("div", "entity-header");
      eHeader.appendChild(make("span", "entity-name", ent.name));
      if (ent.type) {
        eHeader.appendChild(make("span", "entity-badge", ent.type));
      }
      eCard.appendChild(eHeader);

      if (ent.description) {
        eCard.appendChild(make("p", "entity-desc", ent.description));
      }
      if (ent.relevance) {
        eCard.appendChild(make("p", "entity-relevance", ent.relevance));
      }
      if (ent.source_url) {
        const link = make("a", "entity-source", ent.source_url);
        link.href = ent.source_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        eCard.appendChild(link);
      }
      grid.appendChild(eCard);
    }
    card.appendChild(grid);
    container.appendChild(card);
  }

  function renderContextualFindings(container, findingsObj) {
    if (!findingsObj) return;
    const card = make("div", "intel-card contextual-findings");
    const header = make("div", "intel-header");
    header.appendChild(make("h4", "intel-title", "Contextual Findings"));
    card.appendChild(header);

    if (findingsObj.findings && findingsObj.findings.length) {
      card.appendChild(make("div", "overview-label", "Key Discoveries"));
      const ul = make("ul", "findings-list");
      for (const item of findingsObj.findings) {
        ul.appendChild(make("li", "", item));
      }
      card.appendChild(ul);
    }

    if (findingsObj.relationships && findingsObj.relationships.length) {
      card.appendChild(make("div", "overview-label", "Entity Relationships"));
      const ul = make("ul", "relationships-list");
      for (const rel of findingsObj.relationships) {
        ul.appendChild(make("li", "", rel));
      }
      card.appendChild(ul);
    }

    if ((findingsObj.limitations && findingsObj.limitations.length) || (findingsObj.unsupported_conclusions && findingsObj.unsupported_conclusions.length)) {
      const limBox = make("div", "limitations-box");
      limBox.appendChild(make("h4", "", "Limitations & Unsupported Conclusions"));
      const ul = make("ul");
      if (findingsObj.limitations) {
        for (const lim of findingsObj.limitations) {
          ul.appendChild(make("li", "", lim));
        }
      }
      if (findingsObj.unsupported_conclusions) {
        for (const uns of findingsObj.unsupported_conclusions) {
          ul.appendChild(make("li", "", `Unsupported: ${uns}`));
        }
      }
      limBox.appendChild(ul);
      card.appendChild(limBox);
    }

    container.appendChild(card);
  }

  function renderEvidence(container, evidenceList) {
    if (!evidenceList || !evidenceList.length) return;
    const card = make("div", "intel-card evidence-section");
    const header = make("div", "intel-header");
    header.appendChild(make("h4", "intel-title", "Evidence & Limitations"));
    header.appendChild(make("span", "intel-pill", `${evidenceList.length} Grounded Citations`));
    card.appendChild(header);

    const grid = make("div", "evidence-grid");
    for (const ev of evidenceList) {
      const evCard = make("div", "evidence-card");
      const evHead = make("div", "evidence-card-header");
      evHead.appendChild(make("span", "evidence-claim", ev.claim));

      const confSlug = (ev.confidence || "supported").toLowerCase().replace(/[^a-z0-9]+/g, "-");
      const tag = make("span", `confidence-tag ${confSlug}`, ev.confidence || "supported");
      evHead.appendChild(tag);
      evCard.appendChild(evHead);

      if (ev.supporting_text) {
        evCard.appendChild(make("blockquote", "evidence-quote", `"${ev.supporting_text}"`));
      }
      if (ev.source_url) {
        const link = make("a", "evidence-source-url", `Source: ${ev.source_url}`);
        link.href = ev.source_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        evCard.appendChild(link);
      }
      grid.appendChild(evCard);
    }
    card.appendChild(grid);
    container.appendChild(card);
  }

  function renderFinalResult(bubble, task, lead) {
    bubble.replaceChildren();
    const result = task.result || {};

    // Honest follow-up attribution tag
    const isFollowup = task.task_type === "thread_followup" || (task.intent && task.intent.task_type === "thread_followup");
    if (isFollowup) {
      let host = "";
      try {
        const url = (task.intent && task.intent.target_url) || task.target_url || (task.result && task.result.target_url);
        if (url) {
          host = new URL(url).hostname;
        }
      } catch (_) {}
      const labelText = host
        ? `Answered from stored crawl of ${host}. No new crawl.`
        : "Answered from stored crawl. No new crawl.";
      const honestyTag = make("div", "followup-honesty-tag", `ℹ ${labelText}`);
      bubble.appendChild(honestyTag);
    }

    if (lead) {
      const leadEl = make("p", "muted", `Plan: ${lead}`);
      bubble.appendChild(leadEl);
    }

    // Status Banner
    const bannerInfo = result.banner || { kind: "error", text: `Task ${task.status}` };
    bubble.appendChild(make("div", `banner ${bannerInfo.kind}`, bannerInfo.text));

    // Execution Stages
    if (task.stages) {
      renderStages(bubble, task.stages);
    }

    // Evidence-Grounded Intelligence (Direct Answer, Overview, Entities, Findings, Evidence)
    const intel = result.intelligence || null;
    if (intel) {
      const intelSection = make("div", "intel-section");
      renderDirectAnswer(intelSection, intel);
      if (!isFollowup) {
        renderWebsiteOverview(intelSection, intel.website_overview || result.overview);
      }
      renderEntities(intelSection, intel.entities || result.entities);
      renderContextualFindings(intelSection, intel.contextual_findings || result.findings);
      renderEvidence(intelSection, intel.evidence || result.evidence);
      bubble.appendChild(intelSection);
    } else if (result.answer) {
      const intelSection = make("div", "intel-section");
      renderDirectAnswer(intelSection, { direct_answer: result.answer, generator: "deterministic" });
      if (!isFollowup) {
        renderWebsiteOverview(intelSection, result.overview);
      }
      renderEntities(intelSection, result.entities);
      renderContextualFindings(intelSection, result.findings);
      renderEvidence(intelSection, result.evidence);
      bubble.appendChild(intelSection);
    }

    // Verified Extractive Summary (suppressed on followups or when identical to direct answer)
    const directAns = (intel && intel.direct_answer) || result.answer || "";
    if (result.summary && !isFollowup && result.summary !== directAns) {
      const sumCard = make("div", "intel-card");
      sumCard.appendChild(make("h4", "intel-title", "Verified Extractive Summary"));
      sumCard.appendChild(make("div", "prose", result.summary));
      bubble.appendChild(sumCard);
    }

    // Structured JSON Output
    if (result.json) {
      const jsonCard = make("div", "intel-card");
      const h3 = make("h4", "intel-title", "Structured JSON Output");
      jsonCard.appendChild(h3);

      const toolbar = make("div", "action-toolbar");

      const copyBtn = make("button", "btn-action", "Copy JSON");
      copyBtn.type = "button";
      copyBtn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(result.json);
          copyBtn.textContent = "Copied!";
          setTimeout(() => { copyBtn.textContent = "Copy JSON"; }, 2000);
        } catch (_) {
          copyBtn.textContent = "Copy failed";
        }
      });
      toolbar.appendChild(copyBtn);

      const dlJson = make("a", "btn-action", "Download JSON");
      dlJson.href = `/api/tasks/${task.id}/export?format=json`;
      dlJson.setAttribute("download", `task_${task.id.slice(0, 8)}.json`);
      toolbar.appendChild(dlJson);

      const dlMd = make("a", "btn-action", "Download Markdown");
      dlMd.href = `/api/tasks/${task.id}/export?format=md`;
      dlMd.setAttribute("download", `task_${task.id.slice(0, 8)}.md`);
      toolbar.appendChild(dlMd);

      const exportCsv = make("a", "btn-action", "Export CSV");
      exportCsv.href = `/api/tasks/${task.id}/export?format=csv`;
      exportCsv.setAttribute("download", `task_${task.id.slice(0, 8)}.csv`);
      toolbar.appendChild(exportCsv);

      jsonCard.appendChild(toolbar);

      const wrap = make("div", "code-wrap");
      const pre = make("pre", "code");
      pre.appendChild(make("code", "", result.json));
      wrap.appendChild(pre);
      jsonCard.appendChild(wrap);
      bubble.appendChild(jsonCard);
    }

    // Errors
    if (result.errors && result.errors.length) {
      bubble.appendChild(make("h3", "", `Reported Errors (${result.errors.length})`));
      const list = make("ul", "errors");
      for (const err of result.errors) {
        list.appendChild(make("li", "", `${err.url || "Target"}: ${err.error}`));
      }
      bubble.appendChild(list);
    }

    // Collapsible execution details
    const details = make("details", "exec");
    details.appendChild(make("summary", "", "Execution Details & Verified Logs"));
    const exec = Object.assign({}, result.execution || {});
    if (exec.stderr_tail !== undefined) {
      details.appendChild(make("p", "muted", "Captured Process Stderr:"));
      details.appendChild(make("pre", "code", exec.stderr_tail || "(no stderr output)"));
      delete exec.stderr_tail;
    }
    details.appendChild(make("p", "muted", "Process Metadata:"));
    details.appendChild(make("pre", "code", JSON.stringify(exec, null, 2)));

    if (task.progress && task.progress.length) {
      details.appendChild(make("p", "muted", "Execution Progress Trail:"));
      const ol = make("ol", "log");
      for (const line of task.progress) ol.appendChild(make("li", "", line));
      details.appendChild(ol);
    }
    bubble.appendChild(details);
    scrollToEnd();
  }

  function renderWelcomeMessage() {
    const row = make("div", "msg bot");
    const bubble = make("div", "bubble welcome-bubble");
    const header = make("div", "welcome-header");
    header.appendChild(make("h2", "", "Natural Language to Verified CLI Execution"));
    header.appendChild(make("p", "", "Every request is converted into a validated execution plan, launched as an approved CLI subprocess with shell=False, and results are verified before being returned."));
    bubble.appendChild(header);

    const quickPrompts = make("div", "quick-prompts");
    quickPrompts.appendChild(make("span", "prompts-label", "Try an example task:"));
    const examples = make("div", "examples");

    const chips = [
      { class: "chip chip-primary", text: "★ Mandatory Demo (example.com)", prompt: "Crawl https://example.com, extract the important information, give me a summary, and return the extracted data in JSON format." },
      { class: "chip", text: "Structured Schema Extraction", prompt: "Visit https://example.com and extract its company name, services, contact information and product categories in JSON." },
      { class: "chip", text: "Bounded Limit (2 pages)", prompt: "Crawl https://example.com up to 2 pages and summarise it." },
      { class: "chip chip-warn", text: "Unreachable Host (Failure Demo)", prompt: "Summarise https://this-domain-does-not-exist-buildsprint.com" }
    ];

    for (const c of chips) {
      const btn = make("button", c.class, c.text);
      btn.type = "button";
      btn.addEventListener("click", () => {
        promptBox.value = c.prompt;
        charCount.textContent = `${promptBox.value.length}/1000`;
        promptBox.focus();
        promptBox.style.height = "auto";
        promptBox.style.height = `${Math.min(promptBox.scrollHeight, 180)}px`;
      });
      examples.appendChild(btn);
    }
    quickPrompts.appendChild(examples);
    bubble.appendChild(quickPrompts);
    row.appendChild(bubble);
    chat.appendChild(row);
  }

  // Thread Management Functions
  function renderThreadsList(threads) {
    threadsList.replaceChildren();
    if (!threads || !threads.length) {
      threadsList.appendChild(make("div", "threads-empty", "No threads recorded yet."));
      return;
    }

    for (const t of threads) {
      const item = make("div", `thread-item ${t.id === activeThreadId ? "active" : ""}`);
      item.dataset.threadId = t.id;

      const btn = make("button", "thread-select-btn");
      btn.type = "button";
      btn.setAttribute("aria-current", t.id === activeThreadId ? "true" : "false");

      const title = make("span", "thread-title", t.title || "New Thread");
      btn.appendChild(title);

      const meta = make("div", "thread-meta");
      const time = make("span", "thread-time", formatTime(t.updated_at || t.created_at));
      meta.appendChild(time);

      if (t.target_url) {
        let hostLabel = "";
        try {
          hostLabel = new URL(t.target_url).hostname;
        } catch (_) {
          hostLabel = t.target_url;
        }
        const badge = make("span", "thread-crawl-badge", hostLabel);
        badge.title = t.target_url;
        meta.appendChild(badge);
      }
      btn.appendChild(meta);

      btn.addEventListener("click", () => {
        if (t.id !== activeThreadId) {
          selectThread(t.id);
        }
      });

      const delBtn = make("button", "thread-delete-btn", "✕");
      delBtn.type = "button";
      delBtn.title = "Delete Thread";
      delBtn.setAttribute("aria-label", "Delete Thread");
      delBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteThread(t.id);
      });

      item.append(btn, delBtn);
      threadsList.appendChild(item);
    }
  }

  async function refreshSidebar() {
    const res = await api("/api/threads");
    if (res.ok) {
      renderThreadsList(res.data.threads || []);
    }
  }

  async function loadThreads(preferredId) {
    const res = await api("/api/threads");
    if (!res.ok) {
      threadsList.replaceChildren();
      const errDiv = make("div", "threads-empty", "Could not load threads. ");
      const retryBtn = make("button", "btn-ghost small", "Retry");
      retryBtn.type = "button";
      retryBtn.addEventListener("click", () => loadThreads());
      errDiv.appendChild(retryBtn);
      threadsList.appendChild(errDiv);
      return;
    }

    const threads = res.data.threads || [];
    let targetId = preferredId || localStorage.getItem("mai.activeThread");
    const exists = threads.some((t) => t.id === targetId);

    if (!exists) {
      if (threads.length > 0) {
        targetId = threads[0].id;
      } else {
        // Auto-create first thread
        const createRes = await api("/api/threads", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        if (createRes.ok && createRes.data.id) {
          return loadThreads(createRes.data.id);
        }
        targetId = null;
      }
    }

    renderThreadsList(threads);
    if (targetId) {
      await selectThread(targetId);
    }
  }

  async function createThread() {
    newThreadBtn.disabled = true;
    try {
      const res = await api("/api/threads", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      if (res.status === 429) {
        alert("Thread limit reached (200 threads maximum). Please delete an older thread first.");
        return;
      }
      if (!res.ok) {
        alert(res.data.error || `Could not create thread (HTTP ${res.status}). Ensure server is online.`);
        return;
      }
      if (res.data.id) {
        await loadThreads(res.data.id);
        promptBox.focus();
      }
    } finally {
      newThreadBtn.disabled = false;
    }
  }

  async function deleteThread(threadId) {
    if (!window.confirm("Delete this thread? This cannot be undone.")) return;
    const res = await api(`/api/threads/${threadId}`, { method: "DELETE" });
    if (res.ok) {
      if (activeThreadId === threadId) {
        localStorage.removeItem("mai.activeThread");
        activeThreadId = null;
      }
      await loadThreads();
    }
  }

  async function selectThread(threadId) {
    activeThreadId = threadId;
    localStorage.setItem("mai.activeThread", threadId);

    // Update sidebar UI selection
    const items = threadsList.querySelectorAll(".thread-item");
    items.forEach((it) => {
      const isActive = it.dataset.threadId === threadId;
      it.classList.toggle("active", isActive);
      const b = it.querySelector(".thread-select-btn");
      if (b) b.setAttribute("aria-current", isActive ? "true" : "false");
    });

    // Close mobile sidebar if open
    threadSidebar.classList.remove("open");
    sidebarOverlay.classList.remove("active");

    // Fetch thread details
    chat.replaceChildren();
    const res = await api(`/api/threads/${threadId}`);
    if (!res.ok) {
      chat.appendChild(make("div", "banner error", "Failed to load thread messages."));
      return;
    }
    activeThreadData = res.data;

    // Render context banner if thread has crawl memory
    if (activeThreadData.has_crawl_memory && activeThreadData.target_url) {
      renderContextBanner(activeThreadData.host || activeThreadData.target_url);
    }

    const messages = activeThreadData.messages || [];
    if (messages.length === 0) {
      renderWelcomeMessage();
    } else {
      for (const msg of messages) {
        if (msg.role === "user") {
          addUserMessage(msg.content);
        } else if (msg.role === "assistant") {
          if (msg.task_id) {
            const bubble = createAssistantBubbleRaw();
            const taskRes = await api(`/api/tasks/${msg.task_id}`);
            if (taskRes.ok && taskRes.data) {
              renderFinalResult(bubble, taskRes.data, taskRes.data.interpretation);
            } else {
              bubble.replaceChildren(make("div", "banner error", "Could not load task details."));
            }
          } else {
            // Advisory message or reply without task
            const bubble = createAssistantBubbleRaw();
            bubble.appendChild(make("p", "direct-answer-text", msg.content));
          }
        }
      }
    }
    scrollToEnd();
  }

  // Execution via /api/threads/<id>/messages
  async function submitMessage(prompt) {
    if (!activeThreadId) {
      await loadThreads();
      if (!activeThreadId) {
        sendBtn.disabled = false;
        const bubble = createAssistantBubbleRaw();
        bubble.appendChild(make("div", "banner error", "Could not connect to conversation threads. Please ensure the server is online."));
        scrollToEnd();
        return;
      }
    }

    addUserMessage(prompt);
    sendBtn.disabled = true;

    const postRes = await api(`/api/threads/${activeThreadId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
    });

    if (postRes.status === 409) {
      sendBtn.disabled = false;
      const bubble = createAssistantBubbleRaw();
      bubble.appendChild(make("div", "banner error", "Wait for the current answer to finish before sending another message."));
      scrollToEnd();
      return;
    }

    if (!postRes.ok) {
      sendBtn.disabled = false;
      const bubble = createAssistantBubbleRaw();
      const errText = postRes.data.error || "The request could not be accepted.";
      bubble.appendChild(make("div", "banner error", errText));
      scrollToEnd();
      return;
    }

    // Check routing decision
    const route = postRes.data.route || postRes.data.decision;
    if (route === "needs_url") {
      sendBtn.disabled = false;
      addAssistantAdvisory(postRes.data.message || postRes.data.reply || "Please provide a valid website URL to crawl.");
      refreshSidebar();
      return;
    }

    // Decision: crawl or followup
    const taskId = postRes.data.id;
    const lead = postRes.data.interpretation;
    const isFollowup = route === "followup" || postRes.data.task_type === "thread_followup";

    const bubble = createAssistantBubbleRaw();
    const banner = make("div", "banner warning", isFollowup ? "Answering from stored crawl..." : "Task Queued · Awaiting worker process");
    bubble.appendChild(banner);

    if (lead) {
      bubble.appendChild(make("p", "muted", `Plan: ${lead}`));
    }
    for (const note of postRes.data.notes || []) {
      bubble.appendChild(make("p", "muted", `Note: ${note}`));
    }

    if (!isFollowup) {
      const cancelBtn = make("button", "btn-ghost small", "Cancel Execution");
      cancelBtn.type = "button";
      cancelBtn.addEventListener("click", async () => {
        cancelBtn.disabled = true;
        cancelBtn.textContent = "Stopping process...";
        await api(`/api/tasks/${taskId}/cancel`, { method: "POST" });
      });
      bubble.appendChild(cancelBtn);
    }

    const progressList = make("ol", "progress");
    bubble.appendChild(progressList);

    const startedTime = Date.now();
    let failures = 0;

    while (true) {
      await sleep(POLL_MS);
      let taskData;
      try {
        const pollRes = await api(`/api/tasks/${taskId}`);
        if (!pollRes.ok) throw new Error("Status check failed");
        taskData = pollRes.data;
        failures = 0;
      } catch (_) {
        failures++;
        if (failures >= MAX_POLL_FAILURES) {
          banner.className = "banner error";
          banner.textContent = "Lost contact with server. Execution may still be running in background.";
          sendBtn.disabled = false;
          return;
        }
        continue;
      }

      const elapsedSec = Math.round((Date.now() - startedTime) / 1000);
      const label = STATUS_LABELS[taskData.status] || taskData.status;
      banner.textContent = `${label} (${elapsedSec}s)`;

      if (taskData.stages) {
        renderStages(bubble, taskData.stages);
      }

      if (taskData.progress && taskData.progress.length) {
        progressList.replaceChildren(...taskData.progress.map((line) => make("li", "", line)));
      }

      if (DONE_STATES.has(taskData.status) && taskData.result) {
        sendBtn.disabled = false;
        renderFinalResult(bubble, taskData, lead);
        refreshSidebar();
        loadHistory();
        return;
      }
    }
  }

  // Persistent History Drawer (Backward-compatible task history)
  async function loadHistory() {
    historyList.replaceChildren(make("li", "history-empty", "Loading task history..."));
    const res = await api("/api/tasks?limit=30");
    if (!res.ok || !res.data.tasks || !res.data.tasks.length) {
      historyList.replaceChildren(make("li", "history-empty", "No previous tasks recorded yet."));
      return;
    }
    historyList.replaceChildren();
    for (const t of res.data.tasks) {
      const li = make("li", "history-item");
      const header = make("div", "history-item-header");
      const badge = make("span", `history-status-badge ${t.status}`, t.status);
      const timeStr = t.created_at ? new Date(t.created_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
      const timeSpan = make("span", "history-time", timeStr);
      header.append(badge, timeSpan);

      const promptDiv = make("div", "history-prompt", t.prompt);
      li.append(header, promptDiv);

      li.addEventListener("click", async () => {
        closeDrawer();
        const taskRes = await api(`/api/tasks/${t.id}`);
        if (taskRes.ok && taskRes.data) {
          const bubble = createAssistantBubbleRaw();
          renderFinalResult(bubble, taskRes.data, taskRes.data.interpretation);
        }
      });
      historyList.appendChild(li);
    }
  }

  function openDrawer() {
    historyDrawer.classList.add("open");
    drawerOverlay.classList.add("active");
    historyDrawer.setAttribute("aria-hidden", "false");
    loadHistory();
  }

  function closeDrawer() {
    historyDrawer.classList.remove("open");
    drawerOverlay.classList.remove("active");
    historyDrawer.setAttribute("aria-hidden", "true");
  }

  // Event Handlers
  historyToggle.addEventListener("click", openDrawer);
  closeHistory.addEventListener("click", closeDrawer);
  drawerOverlay.addEventListener("click", closeDrawer);
  refreshHistory.addEventListener("click", loadHistory);

  threadsToggle.addEventListener("click", () => {
    if (window.innerWidth >= 900) {
      document.querySelector(".app-layout").classList.toggle("sidebar-collapsed");
    } else {
      threadSidebar.classList.toggle("open");
      sidebarOverlay.classList.toggle("active");
    }
  });

  closeSidebar.addEventListener("click", () => {
    threadSidebar.classList.remove("open");
    sidebarOverlay.classList.remove("active");
  });

  sidebarOverlay.addEventListener("click", () => {
    threadSidebar.classList.remove("open");
    sidebarOverlay.classList.remove("active");
  });

  newThreadBtn.addEventListener("click", createThread);

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeDrawer();
      threadSidebar.classList.remove("open");
      sidebarOverlay.classList.remove("active");
    }
  });

  // Composer event handlers
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const prompt = promptBox.value.trim();
    if (!prompt) return;
    promptBox.value = "";
    charCount.textContent = "0/1000";
    promptBox.style.height = "auto";
    submitMessage(prompt).catch((err) => {
      console.error("Execution error:", err);
      sendBtn.disabled = false;
    });
  });

  promptBox.addEventListener("input", () => {
    charCount.textContent = `${promptBox.value.length}/1000`;
    promptBox.style.height = "auto";
    promptBox.style.height = `${Math.min(promptBox.scrollHeight, 180)}px`;
  });

  promptBox.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  // Example chips click delegation (works on initial HTML chips & dynamic chips)
  chat.addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (chip) {
      const promptText = chip.dataset.prompt || chip.getAttribute("data-prompt");
      if (promptText) {
        promptBox.value = promptText;
        charCount.textContent = `${promptBox.value.length}/1000`;
        promptBox.focus();
        promptBox.style.height = "auto";
        promptBox.style.height = `${Math.min(promptBox.scrollHeight, 180)}px`;
      }
    }
  });

  // Initialization
  initTheme();
  loadThreads();
})();
