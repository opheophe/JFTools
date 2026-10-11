"use strict";

let LOG_CURRENT = null;

function fmtSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

async function loadLogs() {
  const list = document.getElementById("logs-list");
  try {
    const data = await getJSON("/api/logs");
    list.innerHTML = "";
    if (!data.ok || !data.groups || data.groups.length === 0) {
      list.appendChild(el("p", { class: "muted", text: "No log files yet." }));
      return;
    }
    data.groups.forEach((group) => {
      list.appendChild(el("h3", { class: "logs-tab", text: group.tab }));
      group.files.forEach((f) => {
        const item = el("button", { class: "log-item", text: f.date + "  (" + fmtSize(f.size) + ")" });
        item.dataset.name = f.name;
        item.addEventListener("click", () => inspectLog(f.name));
        list.appendChild(item);
      });
    });
  } catch (err) {
    list.innerHTML = "";
    list.appendChild(el("p", { class: "error", text: String(err) }));
  }
}

async function inspectLog(name) {
  LOG_CURRENT = name;
  document.getElementById("log-name").textContent = name;
  document.getElementById("log-refresh").hidden = false;
  const dl = document.getElementById("log-download");
  dl.hidden = false;
  dl.href = "/api/logs/" + encodeURIComponent(name) + "/download";
  try {
    const data = await getJSON("/api/logs/" + encodeURIComponent(name));
    document.getElementById("log-content").textContent = data.ok
      ? (data.content || "(empty)")
      : (data.error || "Error loading log.");
  } catch (err) {
    document.getElementById("log-content").textContent = String(err);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  loadLogs();
  document.getElementById("log-refresh").addEventListener("click", () => {
    if (LOG_CURRENT) inspectLog(LOG_CURRENT); else loadLogs();
  });
});
