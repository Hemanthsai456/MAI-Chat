"use strict";

(() => {
  const chat = document.getElementById("chat");
  const form = document.getElementById("composer");
  const promptBox = document.getElementById("prompt");
  const POLL_MS = 800;
  const MAX_POLL_FAILURES = 10;
  const DONE = new Set(["succeeded", "partial", "failed", "timed_out", "cancelled"]);
  const LABELS = {
    queued: "Queued",
    running: "Running",
    succeeded: "Done",
    partial: "Done (partial)",
    failed: "Done (failed)",
    timed_out: "Stopped (timed out)",
    cancelled: "Cancelled",
  };

  // Build DOM with textContent only: task output is data and is never parsed as HTML.
  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function scrollToEnd() {
    window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
  }

  function addUserMessage(text) {
    const row = make("div", "msg user");
    row.appendChild(make("div", "bubble", text));
    chat.appendChild(row);
    scrollToEnd();
  }

  function addAssistantBubble() {
    const row = make("div", "msg bot");
    const bubble = make("div", "bubble");
    bubble.appendChild(make("p", "status", "Queued"));
    row.appendChild(bubble);
    chat.appendChild(row);
    scrollToEnd();
    return bubble;
  }

  async function api(path, options) {
    const response = await fetch(path, options);
    let data = {};
    try {
      data = await response.json();
    } catch (_) {
      data = {};
    }
    return { ok: response.ok, data };
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function renderBanner(bubble, result) {
    bubble.appendChild(make("div", `banner ${result.banner.kind}`, result.banner.text));
  }

  function renderFinal(bubble, result, progress, lead) {
    bubble.replaceChildren();
    if (lead) bubble.appendChild(make("p", "muted", lead));
    renderBanner(bubble, result);

    if (result.summary) {
      bubble.appendChild(make("h3", "", "Summary"));
      bubble.appendChild(make("div", "prose", result.summary));
    }

    if (result.json) {
      bubble.appendChild(make("h3", "", "Structured JSON"));
      const wrap = make("div", "code-wrap");
      const copy = make("button", "ghost small", "Copy JSON");
      copy.type = "button";
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(result.json);
          copy.textContent = "Copied";
        } catch (_) {
          copy.textContent = "Copy not available";
        }
      });
      const pre = make("pre", "code");
      pre.appendChild(make("code", "", result.json));
      wrap.append(copy, pre);
      bubble.appendChild(wrap);
    }

    if (result.errors && result.errors.length) {
      bubble.appendChild(make("h3", "", `Errors (${result.errors.length})`));
      const list = make("ul", "errors");
      for (const err of result.errors) {
        list.appendChild(make("li", "", `${err.url}: ${err.error}`));
      }
      bubble.appendChild(list);
    }

    const details = make("details", "exec");
    details.appendChild(make("summary", "", "Execution details"));
    const execution = Object.assign({}, result.execution || {});
    if (execution.stderr_tail !== undefined) {
      details.appendChild(make("p", "muted", "Captured process output (stderr):"));
      details.appendChild(make("pre", "code", execution.stderr_tail || "(empty)"));
      delete execution.stderr_tail;
    }
    details.appendChild(make("pre", "code", JSON.stringify(execution, null, 2)));
    if (progress && progress.length) {
      details.appendChild(make("p", "muted", "Progress log:"));
      const ol = make("ol", "log");
      for (const line of progress) ol.appendChild(make("li", "", line));
      details.appendChild(ol);
    }
    bubble.appendChild(details);
    scrollToEnd();
  }

  function renderProgress(bubble, task, started) {
    const seconds = Math.round((Date.now() - started) / 1000);
    bubble.querySelector(".status").textContent = `${LABELS[task.status] || task.status} (${seconds}s)`;
    const list = bubble.querySelector(".progress");
    if (list) {
      list.replaceChildren(...task.progress.map((line) => make("li", "", line)));
    }
  }

  async function runTask(prompt) {
    addUserMessage(prompt);
    const bubble = addAssistantBubble();

    const created = await api("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
    });
    if (!created.ok) {
      renderFinal(bubble, {
        task_status: "failed",
        banner: { kind: "error", text: created.data.error || "The request could not be accepted." },
        summary: null, json: null, errors: [], execution: {},
      }, [], null);
      return;
    }

    const id = created.data.id;
    const lead = created.data.interpretation;
    bubble.replaceChildren(
      make("p", "status", "Queued"),
      make("p", "muted", lead),
    );
    for (const note of created.data.notes || []) bubble.appendChild(make("p", "muted", `Note: ${note}`));
    bubble.appendChild(make("ol", "progress"));

    const cancel = make("button", "ghost small", "Cancel task");
    cancel.type = "button";
    cancel.addEventListener("click", async () => {
      cancel.disabled = true;
      cancel.textContent = "Cancelling...";
      await api(`/api/tasks/${id}/cancel`, { method: "POST" });
    });
    bubble.appendChild(cancel);

    const started = Date.now();
    let failures = 0;
    while (true) {
      await sleep(POLL_MS);
      let task;
      try {
        const response = await api(`/api/tasks/${id}`);
        if (!response.ok) throw new Error("task lookup failed");
        task = response.data;
        failures = 0;
      } catch (_) {
        failures += 1;
        if (failures >= MAX_POLL_FAILURES) {
          bubble.querySelector(".status").textContent = "Lost contact with the server. Reload to check again.";
          return;
        }
        continue;
      }
      renderProgress(bubble, task, started);
      if (DONE.has(task.status) && task.result) {
        renderFinal(bubble, task.result, task.progress, lead);
        return;
      }
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const prompt = promptBox.value.trim();
    if (!prompt) return;
    promptBox.value = "";
    runTask(prompt).catch(() => {
      // runTask handles server errors itself; this catches unexpected DOM or network faults.
    });
  });

  promptBox.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  document.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      promptBox.value = chip.dataset.prompt || "";
      promptBox.focus();
    });
  });
})();
