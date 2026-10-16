"use strict";

function setUpdated(lastSync, fetching, error) {
  renderSyncLabel("lib-updated", { lastSync, fetching, error });
}

function pollLibrariesUntilDone() {
  startStatusPoll(
    "/api/libraries/status",
    (s) => setUpdated(s.last_sync, s.status === "running", s.error),
    () => {
      document.getElementById("lib-refresh").disabled = false;
      loadLibraries();
    }
  );
}

async function loadLibraries() {
  const body = document.getElementById("libraries-body");
  const errEl = document.getElementById("refresh-error");
  errEl.hidden = true;
  try {
    const data = await getJSON("/api/libraries");
    if (!data.ok) {
      body.innerHTML = "";
      errEl.textContent = data.error || "Failed to load libraries.";
      errEl.hidden = false;
      return;
    }
    renderLibraries(data.libraries || []);
    renderRecent(data.recent || []);
    setUpdated(data.last_sync, data.fetching, data.fetch_error);
    if (data.fetching) pollLibrariesUntilDone();
  } catch (err) {
    body.innerHTML = "";
    errEl.textContent = String(err);
    errEl.hidden = false;
  }
}

function renderLibraries(libs) {
  const body = document.getElementById("libraries-body");
  body.innerHTML = "";
  if (libs.length === 0) {
    body.appendChild(el("tr", {}, [el("td", { colspan: "4", class: "muted", text: "No libraries found." })]));
    return;
  }
  let total = 0;
  libs.forEach((lib) => {
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
}

function renderRecent(items) {
  const body = document.getElementById("recent-body");
  body.innerHTML = "";
  if (items.length === 0) {
    body.appendChild(el("tr", {}, [el("td", { colspan: "4", class: "muted", text: "No recent additions." })]));
    return;
  }
  items.forEach((it) => {
    const tr = el("tr");
    tr.appendChild(el("td", { class: "muted nowrap", text: it.added || "—" }));
    tr.appendChild(el("td", { text: it.name }));
    tr.appendChild(el("td", { class: "muted", text: it.type }));
    tr.appendChild(el("td", { class: "muted", text: it.year || "—" }));
    body.appendChild(tr);
    if (it.id) registerPopup(tr, { key: it.id, itemId: it.id });
  });
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
      refreshLibraries();
    });
  });
}

async function refreshLibraries() {
  const btn = document.getElementById("lib-refresh");
  btn.disabled = true;
  try { await postJSON("/api/libraries/refresh", null); } catch (err) { /* ignore */ }
  pollLibrariesUntilDone();
}

document.addEventListener("DOMContentLoaded", () => {
  loadLibraries();
  document.getElementById("lib-refresh").addEventListener("click", refreshLibraries);
});