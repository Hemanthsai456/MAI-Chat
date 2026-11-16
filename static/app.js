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

  const POLL_MS = 600;
  const MAX_POLL_FAILURES = 12;
  const DONE_STATES = new Set(["succeeded", "partial", "failed", "timed_out", "cancelled"]);
  const STAGE_ICONS = {
    completed: "✓",
    active: "⟳",
    pending: "○",
    failed: "✕",
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

  function createAssistantBubble() {
    const row = make("div", "msg bot");
    const bubble = make("div", "bubble");
    bubble.appendChild(make("div", "banner warning", "Initializing task..."));
    row.appendChild(bubble);
    chat.appendChild(row);
    scrollToEnd();
    return bubble;
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

    // Represents
    const repField = make("div", "overview-field");
    repField.appendChild(make("span", "overview-label", "Represents"));
    repField.appendChild(make("span", "overview-val", overview.represents || "N/A"));
    grid.appendChild(repField);

    // Purpose
    const purpField = make("div", "overview-field");
    purpField.appendChild(make("span", "overview-label", "Primary Purpose"));
    purpField.appendChild(make("span", "overview-val", overview.purpose || "N/A"));
    grid.appendChild(purpField);

    // Relevance
    const relField = make("div", "overview-field");
    relField.appendChild(make("span", "overview-label", "Relevance to Question"));
    relField.appendChild(make("span", "overview-val", overview.relevance || "N/A"));
    grid.appendChild(relField);

    // Categories
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
      renderWebsiteOverview(intelSection, intel.website_overview || result.overview);
      renderEntities(intelSection, intel.entities || result.entities);
      renderContextualFindings(intelSection, intel.contextual_findings || result.findings);
      renderEvidence(intelSection, intel.evidence || result.evidence);
      bubble.appendChild(intelSection);
    } else if (result.answer) {
      const intelSection = make("div", "intel-section");
      renderDirectAnswer(intelSection, { direct_answer: result.answer, generator: "deterministic" });
      renderWebsiteOverview(intelSection, result.overview);
      renderEntities(intelSection, result.entities);
      renderContextualFindings(intelSection, result.findings);
      renderEvidence(intelSection, result.evidence);
      bubble.appendChild(intelSection);
    }

    // Verified Extractive Summary
    if (result.summary) {
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

      // Action toolbar: Copy, Download JSON, Download Markdown, Export CSV
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

  async function executeTask(prompt) {
    addUserMessage(prompt);
    const bubble = createAssistantBubble();
    sendBtn.disabled = true;

    const postRes = await api("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
    });

    if (!postRes.ok) {
      sendBtn.disabled = false;
      const errText = postRes.data.error || "The request could not be accepted.";
      renderFinalResult(bubble, {
        id: "",
        status: "failed",
        stages: [],
        progress: [],
        result: {
          banner: { kind: "error", text: errText },
          summary: null, json: null, errors: [], execution: {},
        },
      }, null);
      return;
    }

    const taskId = postRes.data.id;
    const lead = postRes.data.interpretation;

    bubble.replaceChildren();
    const banner = make("div", "banner warning", "Task Queued · Awaiting worker process");
    bubble.appendChild(banner);

    if (lead) {
      bubble.appendChild(make("p", "muted", `Plan: ${lead}`));
    }
    for (const note of postRes.data.notes || []) {
      bubble.appendChild(make("p", "muted", `Note: ${note}`));
    }

    // Cancel Button
    const cancelBtn = make("button", "btn-ghost small", "Cancel Execution");
    cancelBtn.type = "button";
    cancelBtn.addEventListener("click", async () => {
      cancelBtn.disabled = true;
      cancelBtn.textContent = "Stopping process...";
      await api(`/api/tasks/${taskId}/cancel`, { method: "POST" });
    });
    bubble.appendChild(cancelBtn);

    const progressList = make("ol", "progress");
    bubble.appendChild(progressList);

    const startedTime = Date.now();
    let failures = 0;

    // Polling Loop
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
        loadHistory();
        return;
      }
    }
  }

  // Persistent History Drawer
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
          const bubble = createAssistantBubble();
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

  historyToggle.addEventListener("click", openDrawer);
  closeHistory.addEventListener("click", closeDrawer);
  drawerOverlay.addEventListener("click", closeDrawer);
  refreshHistory.addEventListener("click", loadHistory);

  // Composer event handlers
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const prompt = promptBox.value.trim();
    if (!prompt) return;
    promptBox.value = "";
    charCount.textContent = "0/1000";
    promptBox.style.height = "auto";
    executeTask(prompt).catch((err) => {
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

  document.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      promptBox.value = chip.dataset.prompt || "";
      charCount.textContent = `${promptBox.value.length}/1000`;
      promptBox.focus();
      promptBox.style.height = "auto";
      promptBox.style.height = `${Math.min(promptBox.scrollHeight, 180)}px`;
    });
  });

  initTheme();
})();
