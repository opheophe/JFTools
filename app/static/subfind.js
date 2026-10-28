"use strict";

// Subfind dialog (Search, Movie Match and Libraries).
//
// Opened from the row's "Subfind" action: the OpenSubtitles search on the
// left, a live player on the right. Downloading a candidate makes the player
// play it (switching subtitles without restarting the video), and the
// Sync/Delete/Rename buttons act on whichever subtitle is playing in the
// player right now.
//
// The player runs in an iframe (/play) and the two sides talk over
// postMessage: the dialog sends "select" (play a freshly downloaded file) and
// "reload" (a sync/delete changed the files on disk), the player sends "track"
// messages whenever its subtitle selection changes. Closing the dialog
// re-probes the file when something was downloaded or deleted. Pages reload
// their own data by assigning `window.subdlRefresh`.

const SUBFIND_LANG_NAMES = {
  en: "English", sv: "Swedish",
  no: "Norwegian", nor: "Norwegian",
  nb: "Norwegian Bokmål", nob: "Norwegian Bokmål",
  nn: "Norwegian Nynorsk", nno: "Norwegian Nynorsk",
  da: "Danish", fi: "Finnish", de: "German", fr: "French", es: "Spanish",
  it: "Italian", nl: "Dutch", pt: "Portuguese", pl: "Polish", ru: "Russian",
  ja: "Japanese", zh: "Chinese", ko: "Korean", ar: "Arabic", hi: "Hindi",
  tr: "Turkish", el: "Greek", cs: "Czech", hu: "Hungarian", ro: "Romanian",
  bg: "Bulgarian", uk: "Ukrainian", th: "Thai", vi: "Vietnamese", id: "Indonesian",
  he: "Hebrew", fa: "Persian", is: "Icelandic", sk: "Slovak", hr: "Croatian",
  sr: "Serbian", sl: "Slovenian", et: "Estonian", lv: "Latvian", lt: "Lithuanian",
  fil: "Filipino", tl: "Tagalog", tgl: "Tagalog",
  und: "Unknown",
};

// The search buttons the dialog offers. The server runs only the method that
// was asked for; the default search (on open) tries the hash first and falls
// back to the Jellyfin name.
const SUBFIND_METHODS = [
  ["hash", "Search Hash"],
  ["filename", "Search Filename"],
  ["jellyfin", "Search Jellyfin name"],
];

const SUBFIND_METHOD_LABELS = {
  hash: "hash",
  name: "name",
  filename: "filename",
  jellyfin: "Jellyfin name",
};

// Pages assign this to reload their subtitle data after a change; Subfind
// reuses the subtitle dialog's hook when its own is not set.
window.subfindRefresh = null;

let subfindPath = null;       // Jellyfin path the dialog is about
let subfindCandidates = [];
let subfindFileMgmt = false;
let subfindChanged = false;   // a download, sync or delete happened this session
let subfindBack = null;
let subfindFrame = null;      // the /play iframe on the right
let subfindCurrent = null;    // {spec, label, kind, file} of the playing track
let subfindBusy = false;      // a sync/delete/rename request is running
let subfindPendingSpec = null; // spec to select once the player answers hello
let subfindLanguages = [];    // preferred language codes, for the Rename menu
let subfindRenameBtn = null;  // the Rename dropdown's button

// The language picked in the results filter. Remembered across searches — and
// page loads — so a new search or a new title keeps the same language instead
// of snapping back to "All languages".
const SUBFIND_LANG_FILTER_KEY = "jftools.subfind.lang";

function subfindLoadLangFilter() {
  try { return localStorage.getItem(SUBFIND_LANG_FILTER_KEY) || ""; } catch (e) { return ""; }
}

let subfindLangFilter = subfindLoadLangFilter();

function subfindSetLangFilter(value) {
  subfindLangFilter = value || "";
  try { localStorage.setItem(SUBFIND_LANG_FILTER_KEY, subfindLangFilter); } catch (e) { /* ignore */ }
}

function subfindLangName(code) {
  const c = (code || "").toLowerCase();
  if (SUBFIND_LANG_NAMES[c]) return SUBFIND_LANG_NAMES[c];
  return code ? code.toUpperCase() : "?";
}

// Close the dialog. ``apply`` runs the post-change rescan (see subfindResync).
function subfindClose(apply) {
  const path = subfindPath;
  const changed = subfindChanged;
  window.removeEventListener("message", subfindOnMessage);
  if (subfindBack) {
    document.body.removeChild(subfindBack);
    subfindBack = null;
  }
  document.removeEventListener("keydown", subfindOnKey);
  subfindPath = null;
  subfindCandidates = [];
  subfindChanged = false;
  subfindFrame = null;
  subfindCurrent = null;
  subfindBusy = false;
  subfindPendingSpec = null;
  subfindLanguages = [];
  subfindRenameBtn = null;
  if (apply && changed && path) subfindResync(path);
}

function subfindOnKey(e) {
  if (e.key === "Escape") subfindClose(true);
}

// After a download or delete: re-probe the file locally (clears "Not scanned")
// and ask Jellyfin to refresh that path, then reload the page's data.
async function subfindResync(path) {
  let failure = null;
  try {
    const data = await subfindPost("/api/subtitles/rescan", "path=" + encodeURIComponent(path));
    const entry = (data && data.entry) || {};
    if (!data.ok || entry.error) {
      failure = entry.error || data.error || "unknown error";
    }
  } catch (err) {
    failure = String(err);
  }
  try {
    await subfindPost("/api/media/rescan", "path=" + encodeURIComponent(path));
  } catch (err) {
    /* best-effort: the local scan is the important part */
  }
  if (failure) subfindBackgroundError("Subtitle rescan failed: " + failure);
  if (window.subfindRefresh) window.subfindRefresh();
  else if (window.subdlRefresh) window.subdlRefresh();
}

// The dialog is already gone, so put errors on the page when it has a spot.
function subfindBackgroundError(msg) {
  console.warn(msg);
  const statusEl = document.getElementById("se-status");
  if (statusEl) statusEl.textContent = msg;
}

// Write a message into the status line without disturbing the results list.
function subfindNotice(msg, kind) {
  const statusEl = document.getElementById("subfind-status");
  if (!statusEl) return;
  const cls = kind === "ok" ? "ok" : (kind === "error" ? "error" : "muted");
  statusEl.innerHTML = "";
  statusEl.appendChild(el("p", { class: cls, text: msg }));
}

function subfindError(msg, opts) {
  const statusEl = document.getElementById("subfind-status");
  statusEl.innerHTML = "";
  statusEl.appendChild(el("p", { class: "error", text: msg }));
  if (opts && opts.settings) {
    statusEl.appendChild(el("p", {}, [
      el("a", { href: "/settings", class: "pop-link", text: "Open the Settings page" }),
    ]));
  }
  const listEl = document.getElementById("subfind-list");
  if (listEl) listEl.innerHTML = "";
}

// A short success.wav once a sync or delete has finished. The element is
// unlocked with a muted play() inside the button-click gesture, so the real
// play after the (possibly long) sync/delete request is not blocked by the
// autoplay policy.
let subfindSound = null;

function subfindUnlockSound() {
  try {
    if (!subfindSound) subfindSound = new Audio("/resources/success.wav");
    subfindSound.volume = 0.9;
    subfindSound.muted = true;
    subfindSound.play().catch(() => { /* ignore */ });
    subfindSound.pause();
    subfindSound.currentTime = 0;
    subfindSound.muted = false;
  } catch (err) {
    /* ignore */
  }
}

function subfindPlayDone() {
  try {
    if (!subfindSound) subfindSound = new Audio("/resources/success.wav");
    subfindSound.muted = false;
    subfindSound.currentTime = 0;
    subfindSound.play().catch(() => { /* a blocked autoplay is not worth an error */ });
  } catch (err) {
    /* ignore */
  }
}

function subfindRenderMeta(data) {
  const metaEl = document.getElementById("subfind-meta");
  metaEl.innerHTML = "";
  if (!data) return;
  const bits = [];
  if (data.method) bits.push((SUBFIND_METHOD_LABELS[data.method] || data.method) + " search");
  if (data.query) bits.push("“" + data.query + "”");
  if (typeof data.total === "number") bits.push(data.total + " result" + (data.total === 1 ? "" : "s"));
  if (typeof data.remaining === "number") bits.push(data.remaining + " downloads left today");
  if (data.note) bits.push(data.note);
  if (bits.length) metaEl.appendChild(el("p", { class: "muted", text: bits.join(" · ") }));
  const langBox = document.getElementById("subfind-lang");
  if (!langBox) return;
  const candidates = data.candidates || [];
  const langs = [...new Set(candidates.map((c) => c.language).filter(Boolean))].sort();
  if (candidates.length && langs.length > 1) {
    // Keep the picked language in the list even when this search has no results
    // in it, so the picker still shows it (the list then says there are none).
    if (subfindLangFilter && !langs.includes(subfindLangFilter)) {
      langs.push(subfindLangFilter);
      langs.sort();
    }
    langBox.hidden = false;
    langBox.innerHTML = "";
    const sel = el("select", { class: "subdl-lang-sel" });
    sel.appendChild(el("option", { value: "", text: "All languages" }));
    langs.forEach((l) => sel.appendChild(el("option", { value: l, text: subfindLangName(l) })));
    sel.value = subfindLangFilter;
    sel.addEventListener("change", () => {
      subfindSetLangFilter(sel.value);
      subfindRenderList(subfindLangFilter);
    });
    langBox.appendChild(el("span", { class: "muted", text: "Language: " }));
    langBox.appendChild(sel);
  } else {
    langBox.hidden = true;
  }
}

function subfindMetaText(c) {
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

function subfindRenderList(langFilter) {
  const listEl = document.getElementById("subfind-list");
  listEl.innerHTML = "";
  const rows = subfindCandidates.filter((c) => !langFilter || c.language === langFilter);
  if (!rows.length) {
    listEl.appendChild(el("p", { class: "muted", text: langFilter ? "No results in that language." : "No results." }));
    return;
  }
  rows.forEach((c) => {
    const item = el("div", { class: "subdl-item" });
    const body = el("div", { class: "subdl-item-body" });
    const line = el("div", { class: "subdl-line" });
    line.appendChild(el("span", { class: "subdl-c-lang", text: subfindLangName(c.language) }));
    line.appendChild(el("span", { class: "subdl-name", text: c.file_name || "subtitle", title: c.file_name }));
    if (c.from_trusted) line.appendChild(el("span", { class: "subdl-badge", text: "trusted" }));
    body.appendChild(line);
    body.appendChild(el("div", { class: "muted subdl-sub", text: subfindMetaText(c) }));
    item.appendChild(body);
    const dl = el("button", { type: "button", class: "subdl-btn subdl-dl", text: "Download" });
    dl.addEventListener("click", () => subfindDownload(c, dl));
    item.appendChild(dl);
    listEl.appendChild(item);
  });
}

// ---- the player side ----------------------------------------------------------

// Talk to the /play iframe. ``extra`` becomes the message body.
function subfindSend(extra) {
  if (!subfindFrame || !subfindFrame.contentWindow) return;
  try {
    subfindFrame.contentWindow.postMessage({ jftools: true, ...extra }, window.location.origin);
  } catch (err) {
    /* ignore */
  }
}

// The player reports its subtitle selection: keep the Sync/Delete buttons
// pointed at the track that is playing right now.
function subfindOnMessage(ev) {
  if (!subfindFrame) return;
  if (ev.source !== subfindFrame.contentWindow) return;
  if (ev.origin !== window.location.origin) return;
  const msg = (ev.data && typeof ev.data === "object") ? ev.data : {};
  if (!msg.jftools) return;
  if (msg.action === "track" || msg.action === "ready") {
    subfindCurrent = {
      spec: msg.spec || "",
      label: msg.label || "",
      kind: msg.kind || "",
      file: msg.file || "",
    };
    if (msg.action === "track" && subfindPendingSpec && msg.spec === subfindPendingSpec) {
      subfindPendingSpec = null;
    }
    subfindUpdateNow();
  } else if (msg.action === "ready" && subfindPendingSpec) {
    // The download finished before the player page had loaded: send the
    // selection again now that it is listening.
    subfindSend({ action: "select", spec: subfindPendingSpec });
  }
}

// Enable/disable Sync, Delete and Rename for whatever is playing. Only
// downloaded (external) subtitle files can be changed — embedded streams are
// part of the video and are edited in the Tracks dialog instead.
function subfindUpdateNow() {
  const nameEl = document.getElementById("subfind-now-name");
  const syncBtn = document.getElementById("subfind-sync");
  const delBtn = document.getElementById("subfind-del");
  if (!nameEl) return;
  const cur = subfindCurrent;
  nameEl.textContent = (cur && cur.label) ? cur.label : "No subtitle selected";
  const usable = subfindFileMgmt && !subfindBusy && !!cur && cur.kind === "external" && !!cur.file;
  [syncBtn, delBtn].forEach((btn) => {
    if (!btn) return;
    btn.disabled = !usable;
    btn.title = !subfindFileMgmt
      ? "File management is off"
      : (subfindBusy ? "Another action is running"
        : (!cur ? "No subtitle is playing"
          : (cur.kind !== "external"
            ? "Embedded tracks can't be synced or deleted — download an external subtitle"
            : "Acts on the subtitle playing in the player")));
  });
  if (subfindRenameBtn) {
    subfindRenameBtn.disabled = !usable || !subfindLanguages.length;
    subfindRenameBtn.title = !subfindFileMgmt
      ? "File management is off"
      : (subfindBusy ? "Another action is running"
        : (!cur ? "No subtitle is playing"
          : (cur.kind !== "external"
            ? "Embedded tracks can't be renamed — download an external subtitle"
            : "Rename the playing subtitle to a preferred language")));
  }
  if (syncBtn && !subfindBusy) syncBtn.textContent = "Sync";
  if (delBtn && !subfindBusy) delBtn.textContent = "Delete";
}

// The Rename dropdown lists the preferred languages from the Settings page.
// Picking one renames the playing subtitle to ``<video>.<code><ext>`` — the
// same shape the downloader writes — and flags the file for a fresh scan.
function subfindBuildRename() {
  const slot = document.getElementById("subfind-rename-slot");
  if (!slot) return;
  slot.innerHTML = "";
  subfindRenameBtn = null;
  const items = (subfindLanguages || []).map((code) =>
    hoverMenuAction(subfindLangName(code), () => subfindRenameTrack(code))
  );
  const wrap = hoverMenuButton("Rename", items, "sfind", null);
  slot.appendChild(wrap);
  subfindRenameBtn = wrap.querySelector("button");
  subfindUpdateNow();
}

async function subfindRenameTrack(code) {
  const cur = subfindCurrent;
  if (!subfindFileMgmt || subfindBusy || !cur || cur.kind !== "external" || !cur.file) return;
  if (!code) return;
  const file = cur.file;
  if (!window.confirm(
    "Rename " + file + " to the video's name with language " + subfindLangName(code) + "?"
  )) return;
  subfindBusy = true;
  subfindUnlockSound();
  subfindUpdateNow();
  try {
    const data = await subfindPost("/api/subtitles/rename",
      "path=" + encodeURIComponent(subfindPath) +
      "&file=" + encodeURIComponent(file) +
      "&language=" + encodeURIComponent(code));
    if (!data.ok) {
      subfindNotice(data.error || "Rename failed.", "error");
      return;
    }
    subfindChanged = true;
    let msg = data.message || "Renamed.";
    if (data.jellyfin) msg += " (Jellyfin warning: " + data.jellyfin + ")";
    subfindNotice(msg, "ok");
    subfindPlayDone();
    // The file's name changed: let the player re-list its tracks. It keeps
    // playing, and re-picks the best (usually the renamed file) track.
    subfindSend({ action: "reload" });
    if (window.subfindRefresh) window.subfindRefresh();
    else if (window.subdlRefresh) window.subdlRefresh();
  } catch (err) {
    subfindNotice(String(err), "error");
  } finally {
    subfindBusy = false;
    subfindUpdateNow();
  }
}

async function subfindSyncTrack() {
  const cur = subfindCurrent;
  if (!subfindFileMgmt || subfindBusy || !cur || cur.kind !== "external" || !cur.file) return;
  const file = cur.file;
  const btn = document.getElementById("subfind-sync");
  if (!window.confirm("Sync " + file + " to the video?\nThis rewrites the file in place.")) return;
  subfindBusy = true;
  subfindUnlockSound();
  if (btn) { btn.disabled = true; btn.textContent = "Syncing…"; }
  try {
    const data = await subfindPost("/api/subtitles/sync",
      "path=" + encodeURIComponent(subfindPath) + "&file=" + encodeURIComponent(file));
    if (!data.ok) {
      subfindNotice(data.error || "Sync failed.", "error");
      return;
    }
    subfindChanged = true;
    subfindNotice(data.message || "Synced.", "ok");
    subfindPlayDone();
    // The file changed on disk: let the player re-read its captions. It keeps
    // the same track (and keeps playing) when the track still exists.
    subfindSend({ action: "reload" });
    if (window.subfindRefresh) window.subfindRefresh();
    else if (window.subdlRefresh) window.subdlRefresh();
  } catch (err) {
    subfindNotice(String(err), "error");
  } finally {
    subfindBusy = false;
    subfindUpdateNow();
  }
}

async function subfindDeleteTrack() {
  const cur = subfindCurrent;
  if (!subfindFileMgmt || subfindBusy || !cur || cur.kind !== "external" || !cur.file) return;
  const file = cur.file;
  const btn = document.getElementById("subfind-del");
  if (!window.confirm("Delete " + file + "?")) return;
  subfindBusy = true;
  subfindUnlockSound();
  if (btn) { btn.disabled = true; btn.textContent = "…"; }
  try {
    const data = await subfindPost("/api/subtitles/delete",
      "path=" + encodeURIComponent(subfindPath) + "&file=" + encodeURIComponent(file));
    if (!data.ok) {
      subfindNotice(data.error || "Delete failed.", "error");
      return;
    }
    subfindChanged = true;
    let msg = data.message || "Deleted.";
    if (data.jellyfin) msg += " (Jellyfin warning: " + data.jellyfin + ")";
    subfindNotice(msg, "ok");
    subfindPlayDone();
    // The playing track is gone: the player falls back to its default (or Off)
    // and reports the new selection back.
    subfindSend({ action: "reload" });
    if (window.subfindRefresh) window.subfindRefresh();
    else if (window.subdlRefresh) window.subdlRefresh();
  } catch (err) {
    subfindNotice(String(err), "error");
  } finally {
    subfindBusy = false;
    subfindUpdateNow();
  }
}

// ---- actions ----------------------------------------------------------------

async function subfindDownload(c, btn) {
  if (btn.disabled) return;
  btn.disabled = true;
  btn.textContent = "Downloading…";
  try {
    let data = await subfindPost("/api/subtitles/download",
      "path=" + encodeURIComponent(subfindPath) +
      "&file_id=" + encodeURIComponent(c.file_id) +
      "&sub_format=srt" +
      "&language=" + encodeURIComponent(c.language));
    if (!data.ok && data.exists &&
        window.confirm((data.error || "A subtitle file already exists.") + "\nOverwrite it?")) {
      data = await subfindPost("/api/subtitles/download",
        "path=" + encodeURIComponent(subfindPath) +
        "&file_id=" + encodeURIComponent(c.file_id) +
        "&sub_format=srt" +
        "&language=" + encodeURIComponent(c.language) +
        "&overwrite=1");
    }
    if (data.ok) {
      btn.textContent = "✓ Saved";
      btn.className = "subdl-btn subdl-ok";
      subfindChanged = true;
      const left = (typeof data.remaining === "number") ? " · " + data.remaining + " downloads left today" : "";
      subfindNotice("Downloaded" + left + " — playing it now.", "ok");
      // The player re-lists its tracks and switches to the new file without
      // restarting the video; if the video has not started yet, it starts now.
      if (data.file) {
        subfindPendingSpec = "external:" + data.file;
        subfindSend({ action: "select", spec: subfindPendingSpec });
      }
      if (window.subfindRefresh) window.subfindRefresh();
      else if (window.subdlRefresh) window.subdlRefresh();
    } else {
      btn.textContent = "Download";
      btn.disabled = false;
      subfindError(data.error || "Download failed.", { settings: data.file_mgmt === false });
    }
  } catch (err) {
    btn.textContent = "Download";
    btn.disabled = false;
    subfindError(String(err));
  }
}

async function subfindPost(url, body) {
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
function subfindMethodButtons() {
  return [...document.querySelectorAll(".subfind-method")];
}

// Keep the pressed button on the method whose results are on screen.
function subfindMarkMethod(method) {
  subfindMethodButtons().forEach((b) => {
    b.classList.toggle("subdl-method-on", !!method && b.dataset.method === method);
  });
}

// ``method`` is one of "hash", "filename", "jellyfin", or empty for the default
// (hash, then the Jellyfin name).
async function subfindSearch(method) {
  const statusEl = document.getElementById("subfind-status");
  statusEl.innerHTML = "";
  statusEl.appendChild(el("span", { class: "spinner" }));
  subfindMarkMethod(method || "");
  const buttons = subfindMethodButtons();
  buttons.forEach((b) => { b.disabled = true; });
  let body = "path=" + encodeURIComponent(subfindPath);
  if (method) body += "&method=" + encodeURIComponent(method);
  try {
    const data = await postJSON("/api/subtitles/search", body);
    statusEl.innerHTML = "";
    if (!data.ok) {
      subfindError(data.error || "Search failed.", {
        settings: data.configured === false || data.file_mgmt === false,
      });
      return;
    }
    subfindCandidates = data.candidates || [];
    subfindFileMgmt = !!data.file_mgmt;
    subfindLanguages = (data.languages || []).slice();
    subfindRenderMeta(data);
    const langBox = document.getElementById("subfind-lang");
    // The remembered language only filters while the picker is on screen; a
    // single-language search shows everything rather than an empty list.
    subfindRenderList(langBox && !langBox.hidden ? subfindLangFilter : "");
    subfindBuildRename();
    subfindUpdateNow();
    if (!subfindFileMgmt) {
      const warn = document.getElementById("subfind-fm");
      if (warn) warn.hidden = false;
    }
  } catch (err) {
    subfindError(String(err));
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
}

function subfindOpen(path) {
  if (!path) return;
  subfindClose(false);
  subfindPath = path;
  subfindCandidates = [];
  subfindChanged = false;
  subfindCurrent = null;
  subfindBusy = false;
  subfindPendingSpec = null;
  subfindLanguages = [];
  subfindRenameBtn = null;

  const back = el("div", { class: "subdl-back" });
  const box = el("div", { class: "subdl subfind" });
  box.appendChild(el("h3", { text: "Subfind" }));
  box.appendChild(el("p", { class: "muted", text: path.replace(/^.*[\\/]/, "") }));
  const methods = el("div", { class: "subdl-methods" });
  SUBFIND_METHODS.forEach(([id, label]) => {
    const btn = el("button", { type: "button", class: "subdl-method subfind-method", text: label, title: label });
    btn.dataset.method = id;
    btn.addEventListener("click", () => subfindSearch(id));
    methods.appendChild(btn);
  });
  const warn = el("p", {
    class: "muted subdl-fm",
    hidden: true,
    text: "File management is off — download, sync and delete are disabled. Enable it on the Settings page.",
  });
  warn.id = "subfind-fm";
  box.appendChild(warn);
  const statusEl = el("div", { id: "subfind-status", class: "subdl-status" });
  box.appendChild(statusEl);

  const cols = el("div", { class: "subdl-cols" });
  const results = el("div", { class: "subdl-col subdl-results" });
  results.appendChild(el("h4", { class: "subdl-col-title", text: "OpenSubtitles results" }));
  // The search buttons sit under the column header.
  results.appendChild(methods);
  results.appendChild(el("div", { id: "subfind-meta", class: "subdl-meta" }));
  const langBox = el("div", { id: "subfind-lang", class: "subdl-lang", hidden: true });
  results.appendChild(langBox);
  results.appendChild(el("div", { id: "subfind-list", class: "subdl-list" }));
  cols.appendChild(results);

  const side = el("div", { class: "subdl-col subfind-side" });
  const now = el("div", { class: "subfind-now" });
  now.appendChild(el("h4", { text: "Now playing" }));
  now.appendChild(el("span", {
    id: "subfind-now-name",
    class: "subfind-now-name muted",
    text: "No subtitle selected",
  }));
  side.appendChild(now);
  const actions = el("div", { class: "subdl-row-actions subfind-now-actions" });
  const sync = el("button", { type: "button", id: "subfind-sync", class: "subdl-mini subdl-sync", text: "Sync" });
  sync.addEventListener("click", subfindSyncTrack);
  const del = el("button", { type: "button", id: "subfind-del", class: "subdl-mini subdl-del", text: "Delete" });
  del.addEventListener("click", subfindDeleteTrack);
  actions.appendChild(sync);
  actions.appendChild(del);
  actions.appendChild(el("span", { id: "subfind-rename-slot" }));
  side.appendChild(actions);
  side.appendChild(el("p", {
    class: "muted subfind-hint",
    text: "Download a subtitle and the player picks it up; Sync, Delete and Rename act on whichever subtitle is playing.",
  }));
  const frame = el("iframe", {
    id: "subfind-frame",
    class: "subfind-frame",
    src: "/play?path=" + encodeURIComponent(path),
    allow: "autoplay; fullscreen",
    allowfullscreen: "",
  });
  side.appendChild(frame);
  cols.appendChild(side);
  box.appendChild(cols);

  back.appendChild(box);
  document.body.appendChild(back);
  back.addEventListener("mousedown", (e) => { if (e.target === back) subfindClose(true); });
  document.addEventListener("keydown", subfindOnKey);
  window.addEventListener("message", subfindOnMessage);
  subfindBack = back;
  subfindFrame = frame;
  // The player may have loaded before a download selected a track; the hello
  // reply resyncs state (and re-sends a pending selection).
  frame.addEventListener("load", () => subfindSend({ action: "hello" }));

  subfindSearch();
  subfindUpdateNow();
}
