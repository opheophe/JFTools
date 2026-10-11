"use strict";

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else node.setAttribute(k, v);
    }
  }
  (children || []).forEach((c) => node.appendChild(c));
  return node;
}

function renderResult(container, data) {
  container.hidden = false;
  container.innerHTML = "";

  if (data.error) {
    container.appendChild(el("p", { class: "error", text: data.error }));
    return;
  }

  if (data.title) container.appendChild(el("h3", { text: data.title }));

  if (data.type === "table") {
    const table = el("table");
    const thead = el("thead");
    const headRow = el("tr");
    data.columns.forEach((c) => headRow.appendChild(el("th", { text: c })));
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = el("tbody");
    data.rows.forEach((row) => {
      const tr = el("tr");
      row.forEach((cell) => tr.appendChild(el("td", { text: cell === null || cell === undefined ? "" : String(cell) })));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    container.appendChild(table);
  } else if (data.type === "kv") {
    const grid = el("div", { class: "kv" });
    data.items.forEach(([k, v]) => {
      grid.appendChild(el("span", { class: "k", text: k }));
      grid.appendChild(el("span", { text: v === null || v === undefined ? "" : String(v) }));
    });
    container.appendChild(grid);
  } else if (data.type === "message") {
    const cls = data.level === "error" ? "error" : data.level === "ok" ? "ok" : "muted";
    container.appendChild(el("p", { class: cls, text: data.text }));
  } else {
    container.appendChild(el("pre", { text: JSON.stringify(data, null, 2) }));
  }
}

async function postJSON(url, body) {
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body }
    : { method: "POST" };
  const resp = await fetch(url, opts);
  if (resp.status === 401) {
    window.location.href = "/login";
    throw new Error("Not authenticated");
  }
  return resp.json();
}

async function getJSON(url) {
  const resp = await fetch(url);
  if (resp.status === 401) {
    window.location.href = "/login";
    throw new Error("Not authenticated");
  }
  return resp.json();
}

function bindActionButtons() {
  document.querySelectorAll(".action-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const moduleId = btn.dataset.module;
      const actionId = btn.dataset.action;
      const resultEl = document.getElementById("result-" + moduleId);
      btn.disabled = true;
      resultEl.hidden = false;
      resultEl.innerHTML = "";
      resultEl.appendChild(el("span", { class: "spinner" }));
      try {
        const data = await postJSON("/api/actions/" + moduleId + "/" + actionId, null);
        renderResult(resultEl, data);
      } catch (err) {
        if (err.message !== "Not authenticated") {
          resultEl.innerHTML = "";
          resultEl.appendChild(el("p", { class: "error", text: String(err) }));
        }
      } finally {
        btn.disabled = false;
      }
    });
  });
}

function serializeForm(form) {
  const parts = [];
  new FormData(form).forEach((value, key) => {
    parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(value));
  });
  return parts.join("&");
}

function showSpinner(resultEl) {
  resultEl.hidden = false;
  resultEl.innerHTML = "";
  resultEl.appendChild(el("span", { class: "spinner" }));
}

function showMessage(resultEl, data) {
  resultEl.innerHTML = "";
  resultEl.appendChild(el("p", {
    class: data.ok ? "ok" : "error",
    text: data.message || data.error || "Done.",
  }));
}

function bindFormSubmit(formId, url, resultId) {
  const form = document.getElementById(formId);
  const resultEl = document.getElementById(resultId);
  if (!form || !resultEl) return;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showSpinner(resultEl);
    try {
      const data = await postJSON(url, serializeForm(form));
      showMessage(resultEl, data);
    } catch (err) {
      resultEl.innerHTML = "";
      resultEl.appendChild(el("p", { class: "error", text: String(err) }));
    }
  });
}

function bindSettings() {
  bindFormSubmit("jellyfin-form", "/settings", "jellyfin-result");
  bindFormSubmit("auth-form", "/settings/auth", "auth-result");
  bindFormSubmit("integrations-form", "/settings/integrations", "integrations-result");
  bindFormSubmit("timezone-form", "/settings/timezone", "timezone-result");

  const testBtn = document.getElementById("test-btn");
  const resultEl = document.getElementById("jellyfin-result");
  if (!testBtn || !resultEl) return;

  testBtn.addEventListener("click", async () => {
    const form = document.getElementById("jellyfin-form");
    showSpinner(resultEl);
    testBtn.disabled = true;
    try {
      const data = await postJSON("/api/settings/test", serializeForm(form));
      resultEl.innerHTML = "";
      renderResult(resultEl, data);
    } catch (err) {
      resultEl.innerHTML = "";
      resultEl.appendChild(el("p", { class: "error", text: String(err) }));
    } finally {
      testBtn.disabled = false;
    }
  });
}

async function loadLibraries() {
  const body = document.getElementById("libraries-body");
  const errEl = document.getElementById("refresh-error");
  try {
    const data = await getJSON("/api/jellyfin/libraries");
    if (!data.ok) {
      body.innerHTML = "";
      errEl.textContent = data.error || "Failed to load libraries.";
      errEl.hidden = false;
      return;
    }
    body.innerHTML = "";
    let total = 0;
    data.libraries.forEach((lib) => {
      const count = typeof lib.items === "number" ? lib.items : 0;
      total += count;
      const tr = el("tr");
      tr.appendChild(el("td", { text: lib.name }));
      tr.appendChild(el("td", { class: "muted", text: lib.type }));
      tr.appendChild(el("td", { text: typeof lib.items === "number" ? String(lib.items) : "—" }));
      const td = el("td");
      const btn = el("button", { class: "scan-btn", text: "Scan" });
      btn.dataset.libraryId = lib.id;
      btn.dataset.name = lib.name;
      td.appendChild(btn);
      tr.appendChild(td);
      body.appendChild(tr);
    });
    // total row
    const tr = el("tr", { class: "total-row" });
    tr.appendChild(el("td", { text: "Total" }));
    tr.appendChild(el("td", { text: "" }));
    tr.appendChild(el("td", { text: String(total) }));
    const td = el("td");
    const btn = el("button", { class: "scan-btn primary", text: "Scan all" });
    btn.dataset.scanAll = "1";
    btn.dataset.name = "all libraries";
    td.appendChild(btn);
    tr.appendChild(td);
    body.appendChild(tr);

    bindScanButtons();
  } catch (err) {
    body.innerHTML = "";
    errEl.textContent = String(err);
    errEl.hidden = false;
  }
}

function bindScanButtons() {
  document.querySelectorAll(".scan-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const url = btn.dataset.scanAll
        ? "/api/jellyfin/scan-all"
        : "/api/jellyfin/scan/" + btn.dataset.libraryId;
      const original = btn.textContent;
      btn.disabled = true;
      btn.textContent = "Starting…";
      try {
        await postJSON(url, null);
        btn.textContent = "Started";
      } catch (err) {
        btn.textContent = "Failed";
      } finally {
        setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 1500);
      }
      refreshActivity();
    });
  });
}

function renderRecentScans(body, scans) {
  scans.forEach((s) => {
    const age = s.age < 60 ? s.age + "s ago" : Math.round(s.age / 60) + " min ago";
    const row = el("div", { class: "task" });
    row.appendChild(el("div", { class: "task-label", text: "Scan " + s.name }));
    row.appendChild(el("div", { class: "task-progress muted", text: "Started " + age }));
    body.appendChild(row);
  });
}

function renderRunningTasks(body, running) {
  running.forEach((task) => {
    const pct = (task.progress === null || task.progress === undefined)
      ? null
      : Math.max(0, Math.min(100, Math.round(task.progress)));
    const row = el("div", { class: "task" });
    row.appendChild(el("div", {
      class: "task-label",
      text: pct === null ? task.name : task.name + " — " + pct + "%",
    }));
    if (pct === null) {
      row.appendChild(el("div", { class: "task-progress muted", text: "Running…" }));
    } else {
      const bar = el("div", { class: "progress" });
      bar.appendChild(el("div", { class: "progress-fill", style: "width:" + pct + "%" }));
      row.appendChild(bar);
    }
    body.appendChild(row);
  });
}

function renderActivityLog(body, act) {
  if (!act.ok) {
    body.appendChild(el("p", { class: "error", text: act.error }));
    return;
  }
  if (!act.entries || act.entries.length === 0) {
    body.appendChild(el("p", { class: "muted", text: "No recent activity." }));
    return;
  }
  const table = el("table");
  const thead = el("thead");
  const hr = el("tr");
  hr.appendChild(el("th", { text: "When" }));
  hr.appendChild(el("th", { text: "Activity" }));
  thead.appendChild(hr);
  table.appendChild(thead);
  const tbody = el("tbody");
  act.entries.forEach((e) => {
    const tr = el("tr");
    tr.appendChild(el("td", { class: "muted nowrap", text: e.date || "" }));
    const detail = e.short_overview ? e.name + " — " + e.short_overview : e.name;
    tr.appendChild(el("td", { text: detail }));
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  body.appendChild(table);
}

function renderActivity(body, tasks, act) {
  body.innerHTML = "";
  const running = tasks.ok && tasks.running ? tasks.running : [];
  const scans = tasks.ok && tasks.recent_scans ? tasks.recent_scans : [];

  if (running.length === 0 && scans.length === 0) {
    body.appendChild(el("p", { class: "muted", text: "No active tasks." }));
  } else {
    renderRecentScans(body, scans);
    renderRunningTasks(body, running);
  }

  body.appendChild(el("h3", { class: "activity-heading", text: "Recent activity" }));
  renderActivityLog(body, act);
}

async function refreshActivity() {
  const body = document.getElementById("activity-body");
  try {
    const [tasks, act] = await Promise.all([
      getJSON("/api/jellyfin/tasks"),
      getJSON("/api/jellyfin/activity"),
    ]);
    renderActivity(body, tasks, act);
  } catch (err) {
    body.innerHTML = "";
    body.appendChild(el("p", { class: "error", text: String(err) }));
  }
}

function startActivityPolling() {
  refreshActivity();
  setInterval(() => {
    if (!document.hidden) refreshActivity();
  }, 3000);
}

document.addEventListener("DOMContentLoaded", () => {
  bindActionButtons();
  bindSettings();
  if (document.getElementById("libraries-body")) {
    loadLibraries();
    startActivityPolling();
  }
});
