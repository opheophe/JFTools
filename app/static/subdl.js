"use strict";

// Subtitle search & download dialog (Search + Movie Match tabs).
//
// Opened from the row's "Subtitles" action. Searches OpenSubtitles for
// the file (moviehash first, name fallback), lists the candidates, lists every
// subtitle track already on the video, and lets the user download, delete or
// re-sync one. The "Search Hash / Filename / Jellyfin name" buttons re-run the
// search one way instead of the default. Closing the dialog (click outside or
// Escape) re-probes the file when something was downloaded or deleted. Pages
// reload their own data by assigning `window.subdlRefresh`.

const SUBDL_LANG_NAMES = {
  en: "English", sv: "Swedish", no: "Norwegian", nb: "Norwegian", nn: "Norwegian",
  da: "Danish", fi: "Finnish", de: "German", fr: "French", es: "Spanish",
  it: "Italian", nl: "Dutch", pt: "Portuguese", pl: "Polish", ru: "Russian",
  ja: "Japanese", zh: "Chinese", ko: "Korean", ar: "Arabic", hi: "Hindi",
  tr: "Turkish", el: "Greek", cs: "Czech", hu: "Hungarian", ro: "Romanian",
  bg: "Bulgarian", uk: "Ukrainian", th: "Thai", vi: "Vietnamese", id: "Indonesian",
  he: "Hebrew", fa: "Persian", is: "Icelandic", sk: "Slovak", hr: "Croatian",
  sr: "Serbian", sl: "Slovenian", et: "Estonian", lv: "Latvian", lt: "Lithuanian",
  und: "Unknown",
};

// The search buttons the dialog offers. The server runs only the method that
// was asked for; the default search (on open) tries the hash first and falls
// back to the Jellyfin name.
const SUBDL_METHODS = [
  ["hash", "Search Hash"],
  ["filename", "Search Filename"],
  ["jellyfin", "Search Jellyfin name"],
];

const SUBDL_METHOD_LABELS = {
  hash: "hash",
  name: "name",
  filename: "filename",
  jellyfin: "Jellyfin name",
};

// Pages assign this to reload their subtitle data after a change.
window.subdlRefresh = null;

let subdlPath = null;      // Jellyfin path the dialog is about
let subdlCandidates = [];
let subdlInternal = [];    // embedded subtitle tracks on the video
let subdlExisting = [];    // external subtitle files next to the video
let subdlFileMgmt = false;
let subdlChanged = false;  // a download or delete happened this session
let subdlBack = null;

function subdlLangName(code) {
  const c = (code || "").toLowerCase();
  if (SUBDL_LANG_NAMES[c]) return SUBDL_LANG_NAMES[c];
  return code ? code.toUpperCase() : "?";
}

function subdlFmtSize(n) {
  if (n === null || n === undefined) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(1) + " MB";
}

// Close the dialog. ``apply`` runs the post-change rescan (see subdlResync).
function subdlClose(apply) {
  const path = subdlPath;
  const changed = subdlChanged;
  if (subdlBack) {
    document.body.removeChild(subdlBack);
    subdlBack = null;
  }
  document.removeEventListener("keydown", subdlOnKey);
  subdlPath = null;
  subdlCandidates = [];
  subdlInternal = [];
  subdlExisting = [];
  subdlChanged = false;
  if (apply && changed && path) subdlResync(path);
}

function subdlOnKey(e) {
  if (e.key === "Escape") subdlClose(true);
}

// After a download or delete: re-probe the file locally (clears "Not scanned")
// and ask Jellyfin to refresh that path, then reload the page's data.
async function subdlResync(path) {
  let failure = null;
  try {
    const data = await subdlPost("/api/subtitles/rescan", "path=" + encodeURIComponent(path));
    const entry = (data && data.entry) || {};
    if (!data.ok || entry.error) {
      failure = entry.error || data.error || "unknown error";
    }
  } catch (err) {
    failure = String(err);
  }
  try {
    await subdlPost("/api/media/rescan", "path=" + encodeURIComponent(path));
  } catch (err) {
    /* best-effort: the local scan is the important part */
  }
  if (failure) subdlBackgroundError("Subtitle rescan failed: " + failure);
  if (window.subdlRefresh) window.subdlRefresh();
}

// The dialog is already gone, so put errors on the page when it has a spot.
function subdlBackgroundError(msg) {
  console.warn(msg);
  const statusEl = document.getElementById("se-status");
  if (statusEl) statusEl.textContent = msg;
}

// Write a message into the status line without disturbing the results list.
function subdlNotice(msg, kind) {
  const statusEl = document.getElementById("subdl-status");
  if (!statusEl) return;
  const cls = kind === "ok" ? "ok" : (kind === "error" ? "error" : "muted");
  statusEl.innerHTML = "";
  statusEl.appendChild(el("p", { class: cls, text: msg }));
}

function subdlError(msg, opts) {
  const statusEl = document.getElementById("subdl-status");
  statusEl.innerHTML = "";
  statusEl.appendChild(el("p", { class: "error", text: msg }));
  if (opts && opts.settings) {
    statusEl.appendChild(el("p", {}, [
      el("a", { href: "/settings", class: "pop-link", text: "Open the Settings page" }),
    ]));
  }
  const listEl = document.getElementById("subdl-list");
  if (listEl) listEl.innerHTML = "";
}

function subdlRenderMeta(data) {
  const metaEl = document.getElementById("subdl-meta");
  metaEl.innerHTML = "";
  if (!data) return;
  const bits = [];
  if (data.method) bits.push((SUBDL_METHOD_LABELS[data.method] || data.method) + " search");
  if (data.query) bits.push("“" + data.query + "”");
  if (typeof data.total === "number") bits.push(data.total + " result" + (data.total === 1 ? "" : "s"));
  if (typeof data.remaining === "number") bits.push(data.remaining + " downloads left today");
  if (data.note) bits.push(data.note);
  if (bits.length) metaEl.appendChild(el("p", { class: "muted", text: bits.join(" · ") }));
  const langBox = document.getElementById("subdl-lang");
  if (!langBox || !data.candidates || !data.candidates.length) return;
  const langs = [...new Set(data.candidates.map((c) => c.language).filter(Boolean))].sort();
  if (langs.length > 1) {
    langBox.hidden = false;
    langBox.innerHTML = "";
    const sel = el("select", { class: "subdl-lang-sel" });
    sel.appendChild(el("option", { value: "", text: "All languages" }));
    langs.forEach((l) => sel.appendChild(el("option", { value: l, text: subdlLangName(l) })));
    sel.addEventListener("change", () => subdlRenderList(sel.value));
    langBox.appendChild(el("span", { class: "muted", text: "Language: " }));
    langBox.appendChild(sel);
  } else {
    langBox.hidden = true;
  }
}

function subdlMetaText(c) {
  const bits = [];
  if (c.download_count) bits.push(c.download_count + " dl");
  if (c.ratings) bits.push("★ " + c.ratings);
  if (c.hearing_impaired) bits.push("HI");
  if (c.ai_translated) bits.push("AI");
  else if (c.machine_translated) bits.push("MT");
  if (c.uploader) bits.push(c.uploader);
  if (c.year) bits.push(c.year);
  return bits.join(" · ");
}

function subdlRenderList(langFilter) {
  const listEl = document.getElementById("subdl-list");
  listEl.innerHTML = "";
  const rows = subdlCandidates.filter((c) => !langFilter || c.language === langFilter);
  if (!rows.length) {
    listEl.appendChild(el("p", { class: "muted", text: langFilter ? "No results in that language." : "No results." }));
    return;
  }
  rows.forEach((c) => {
    const item = el("div", { class: "subdl-item" });
    const line = el("div", { class: "subdl-line" });
    line.appendChild(el("span", { class: "subdl-c-lang", text: subdlLangName(c.language) }));
    line.appendChild(el("span", { class: "subdl-name", text: c.file_name || "subtitle", title: c.file_name }));
    if (c.from_trusted) line.appendChild(el("span", { class: "subdl-badge", text: "trusted" }));
    item.appendChild(line);
    item.appendChild(el("div", { class: "muted subdl-sub", text: subdlMetaText(c) }));
    const dl = el("button", { type: "button", class: "subdl-btn", text: "Download" });
    dl.addEventListener("click", () => subdlDownload(c, dl));
    item.appendChild(dl);
    listEl.appendChild(item);
  });
}

// ---- subtitle tracks (embedded + external) ----------------------------------

// Delete/Sync buttons for a track row. Embedded tracks pass no file, so both
// stay disabled — only files on disk can be changed. Play works for every row
// (it only reads), in the player popup with that track preselected.
function subdlTrackButtons(rowEl, file, spec) {
  const play = el("button", { type: "button", class: "subdl-mini subdl-play", text: "Play" });
  if (spec) play.addEventListener("click", () => playVideo(subdlPath, spec));
  else play.disabled = true;

  const del = el("button", { type: "button", class: "subdl-mini subdl-del", text: "Delete" });
  const sync = el("button", { type: "button", class: "subdl-mini subdl-sync", text: "Sync" });
  if (!file || !subdlFileMgmt) {
    del.disabled = true;
    sync.disabled = true;
  } else {
    del.addEventListener("click", () => subdlDelete(file, del));
    sync.addEventListener("click", () => subdlSync(file, sync));
  }
  rowEl.appendChild(play);
  rowEl.appendChild(del);
  rowEl.appendChild(sync);
}

function subdlRenderTracks() {
  const box = document.getElementById("subdl-existing");
  if (!box) return;
  box.innerHTML = "";
  box.appendChild(el("h4", { text: "Subtitle tracks" }));
  if (!subdlInternal.length && !subdlExisting.length) {
    box.appendChild(el("p", { class: "muted", text: "No subtitle tracks on this file." }));
    return;
  }
  subdlInternal.forEach((t, i) => {
    const rowEl = el("div", { class: "subdl-subrow" });
    rowEl.appendChild(el("span", { class: "subdl-tag", text: "embedded" }));
    let label = t.name || t.lang || "Unknown";
    if (t.forced && !/forced/i.test(label)) label += " (Forced)";
    rowEl.appendChild(el("span", { class: "subdl-name", text: label, title: label }));
    if (t.codec) rowEl.appendChild(el("span", { class: "muted", text: t.codec }));
    // "internal:<n>" is ffprobe's stream index; "sub:<n>" the n-th subtitle
    // stream, for stores written before the index was kept.
    const spec = (t.index === null || t.index === undefined) ? "sub:" + i : "internal:" + t.index;
    subdlTrackButtons(rowEl, null, spec);
    box.appendChild(rowEl);
  });
  subdlExisting.forEach((s) => {
    const rowEl = el("div", { class: "subdl-subrow" });
    rowEl.appendChild(el("span", { class: "subdl-tag", text: "external" }));
    let label = s.lang || "Unknown";
    if (s.forced) label += " (Forced)";
    if (s.ai) label += " (AI)";
    rowEl.appendChild(el("span", { class: "subdl-c-lang", text: label }));
    rowEl.appendChild(el("span", { class: "subdl-name", text: s.file, title: s.file }));
    rowEl.appendChild(el("span", { class: "muted", text: subdlFmtSize(s.size) }));
    subdlTrackButtons(rowEl, s.file, "external:" + s.file);
    box.appendChild(rowEl);
  });
}

async function subdlLoadTracks() {
  subdlInternal = [];
  subdlExisting = [];
  try {
    const data = await postJSON("/api/subtitles/existing", "path=" + encodeURIComponent(subdlPath));
    if (data && data.ok) {
      subdlInternal = data.internal || [];
      subdlExisting = data.external || [];
      if (typeof data.file_mgmt === "boolean") subdlFileMgmt = data.file_mgmt;
    }
  } catch (err) {
    /* leave the lists empty */
  }
  subdlRenderTracks();
}

// ---- actions ----------------------------------------------------------------

async function subdlDownload(c, btn) {
  if (btn.disabled) return;
  btn.disabled = true;
  btn.textContent = "Downloading…";
  try {
    let data = await subdlPost("/api/subtitles/download",
      "path=" + encodeURIComponent(subdlPath) +
      "&file_id=" + encodeURIComponent(c.file_id) +
      "&sub_format=srt" +
      "&language=" + encodeURIComponent(c.language));
    if (!data.ok && data.exists &&
        window.confirm((data.error || "A subtitle file already exists.") + "\nOverwrite it?")) {
      data = await subdlPost("/api/subtitles/download",
        "path=" + encodeURIComponent(subdlPath) +
        "&file_id=" + encodeURIComponent(c.file_id) +
        "&sub_format=srt" +
        "&language=" + encodeURIComponent(c.language) +
        "&overwrite=1");
    }
    if (data.ok) {
      btn.textContent = "✓ Saved";
      btn.className = "subdl-btn subdl-ok";
      subdlChanged = true;
      const left = (typeof data.remaining === "number") ? " · " + data.remaining + " downloads left today" : "";
      subdlNotice("Downloaded" + left + " — closing the dialog rescans the file.", "ok");
      await subdlLoadTracks();
      if (window.subdlRefresh) window.subdlRefresh();
    } else {
      btn.textContent = "Download";
      btn.disabled = false;
      subdlError(data.error || "Download failed.", { settings: data.file_mgmt === false });
    }
  } catch (err) {
    btn.textContent = "Download";
    btn.disabled = false;
    subdlError(String(err));
  }
}

async function subdlDelete(file, btn) {
  if (!subdlFileMgmt || !file) return;
  if (!window.confirm("Delete " + file + "?")) return;
  if (btn) { btn.disabled = true; btn.textContent = "…"; }
  try {
    const data = await subdlPost("/api/subtitles/delete",
      "path=" + encodeURIComponent(subdlPath) + "&file=" + encodeURIComponent(file));
    if (!data.ok) {
      subdlNotice(data.error || "Delete failed.", "error");
      if (btn) { btn.disabled = false; btn.textContent = "Delete"; }
      return;
    }
    subdlChanged = true;
    let msg = data.message || "Deleted.";
    if (data.jellyfin) msg += " (Jellyfin warning: " + data.jellyfin + ")";
    subdlNotice(msg, "ok");
    await subdlLoadTracks();
    if (window.subdlRefresh) window.subdlRefresh();
  } catch (err) {
    subdlNotice(String(err), "error");
    if (btn) { btn.disabled = false; btn.textContent = "Delete"; }
  }
}

async function subdlSync(file, btn) {
  if (!subdlFileMgmt || !file) return;
  if (!window.confirm("Sync " + file + " to the video?\nThis rewrites the file in place.")) return;
  if (btn) { btn.disabled = true; btn.textContent = "Syncing…"; }
  try {
    const data = await subdlPost("/api/subtitles/sync",
      "path=" + encodeURIComponent(subdlPath) + "&file=" + encodeURIComponent(file));
    if (!data.ok) {
      subdlNotice(data.error || "Sync failed.", "error");
      if (btn) { btn.disabled = false; btn.textContent = "Sync"; }
      return;
    }
    subdlNotice(data.message || "Synced.", "ok");
    await subdlLoadTracks();
    if (window.subdlRefresh) window.subdlRefresh();
  } catch (err) {
    subdlNotice(String(err), "error");
    if (btn) { btn.disabled = false; btn.textContent = "Sync"; }
  }
}

async function subdlPost(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", "X-JFTools": "1" },
    body: body,
  });
  if (resp.status === 401) {
    window.location.href = "/login";
    throw new Error("Not authenticated");
  }
  return resp.json();
}

// The search-method buttons in the open dialog.
function subdlMethodButtons() {
  return [...document.querySelectorAll(".subdl-method")];
}

// Keep the pressed button on the method whose results are on screen.
function subdlMarkMethod(method) {
  subdlMethodButtons().forEach((b) => {
    b.classList.toggle("subdl-method-on", !!method && b.dataset.method === method);
  });
}

// ``method`` is one of "hash", "filename", "jellyfin", or empty for the default
// (hash, then the Jellyfin name).
async function subdlSearch(method) {
  const statusEl = document.getElementById("subdl-status");
  statusEl.innerHTML = "";
  statusEl.appendChild(el("span", { class: "spinner" }));
  subdlMarkMethod(method || "");
  const buttons = subdlMethodButtons();
  buttons.forEach((b) => { b.disabled = true; });
  let body = "path=" + encodeURIComponent(subdlPath);
  if (method) body += "&method=" + encodeURIComponent(method);
  try {
    const data = await postJSON("/api/subtitles/search", body);
    statusEl.innerHTML = "";
    if (!data.ok) {
      subdlError(data.error || "Search failed.", {
        settings: data.configured === false || data.file_mgmt === false,
      });
      return;
    }
    subdlCandidates = data.candidates || [];
    subdlFileMgmt = !!data.file_mgmt;
    subdlRenderMeta(data);
    subdlRenderList("");
    subdlRenderTracks();  // picks up file_mgmt so per-row buttons enable/disable
    if (!subdlFileMgmt) {
      const warn = document.getElementById("subdl-fm");
      if (warn) warn.hidden = false;
    }
  } catch (err) {
    subdlError(String(err));
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
}

function subdlOpen(path) {
  if (!path) return;
  subdlClose(false);
  subdlPath = path;
  subdlCandidates = [];
  subdlInternal = [];
  subdlExisting = [];
  subdlChanged = false;

  const back = el("div", { class: "subdl-back" });
  const box = el("div", { class: "subdl" });
  box.appendChild(el("h3", { text: "Subtitles" }));
  box.appendChild(el("p", { class: "muted", text: path.replace(/^.*[\\/]/, "") }));
  const methods = el("div", { class: "subdl-methods" });
  SUBDL_METHODS.forEach(([id, label]) => {
    const btn = el("button", { type: "button", class: "subdl-method", text: label, title: label });
    btn.dataset.method = id;
    btn.addEventListener("click", () => subdlSearch(id));
    methods.appendChild(btn);
  });
  box.appendChild(methods);
  const warn = el("p", {
    class: "muted subdl-fm",
    hidden: true,
    text: "File management is off — download, sync and delete are disabled. Enable it on the Settings page.",
  });
  warn.id = "subdl-fm";
  box.appendChild(warn);
  const statusEl = el("div", { id: "subdl-status", class: "subdl-status" });
  box.appendChild(statusEl);
  box.appendChild(el("div", { id: "subdl-meta", class: "subdl-meta" }));
  const langBox = el("div", { id: "subdl-lang", class: "subdl-lang", hidden: true });
  box.appendChild(langBox);
  const list = el("div", { id: "subdl-list", class: "subdl-list" });
  box.appendChild(list);
  box.appendChild(el("div", { id: "subdl-existing", class: "subdl-existing" }));

  back.appendChild(box);
  document.body.appendChild(back);
  back.addEventListener("mousedown", (e) => { if (e.target === back) subdlClose(true); });
  document.addEventListener("keydown", subdlOnKey);
  subdlBack = back;

  subdlSearch();
  subdlLoadTracks();
}
