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

document.addEventListener("DOMContentLoaded", () => {
  bindActionButtons();
  bindSettings();
});
