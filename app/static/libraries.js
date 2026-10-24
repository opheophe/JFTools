"use strict";

// Jellyfin/Radarr/Sonarr base URLs for the row action pills (Open ▾, Sync ▾).
// Empty until /api/libraries answers; the pills then render without those links.
let libUrls = { jellyfin: "", radarr: "", sonarr: "" };

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
    libUrls = {
      jellyfin: data.jellyfin_url || "",
      radarr: data.radarr_url || "",
      sonarr: data.sonarr_url || "",
    };
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
    const btn = el("button", { class: "scan-btn btn-run", text: "Scan" });
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
  const btn = el("button", { class: "scan-btn btn-run", text: "Scan all" });
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
    body.appendChild(el("tr", {}, [el("td", { colspan: "6", class: "muted", text: "No recent additions." })]));
    return;
  }
  items.forEach((it) => {
    const tr = el("tr");
    tr.appendChild(el("td", { class: "muted nowrap", text: it.added || "—" }));
    tr.appendChild(el("td", { class: "muted", text: it.library || "—" }));
    const nameTd = el("td");
    nameTd.appendChild(el("div", { text: it.name }));
    const episodeLine = episodeSubLine(it);
    if (episodeLine) {
      nameTd.appendChild(el("div", { class: "muted name-sub", text: episodeLine }));
    }
    tr.appendChild(nameTd);
    tr.appendChild(el("td", { class: "muted", text: it.type }));
    tr.appendChild(el("td", { class: "muted", text: it.year || "—" }));
    tr.appendChild(rowActions(it, { urls: libUrls, google: true }));
    body.appendChild(tr);
    if (it.id) registerPopup(nameTd, { key: it.id, itemId: it.id });
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
  let res = null;
  try { res = await postJSON("/api/libraries/refresh", null); } catch (err) { /* ignore */ }
  if (res && res.started === false) {
    document.getElementById("lib-updated").textContent = res.message || "Busy.";
    btn.disabled = false;
    return;
  }
  pollLibrariesUntilDone();
}

document.addEventListener("DOMContentLoaded", () => {
  // The recent table has no subtitle column, so a subtitle change needs no
  // refresh of its own (the dialog still re-probes the file server-side).
  window.subdlRefresh = null;
  loadLibraries();
  document.getElementById("lib-refresh").addEventListener("click", refreshLibraries);
});