"use strict";

const SE = {
  rows: [], jellyfin: "", radarr: "", sonarr: "",
  scans: {}, subtitlesOk: new Set(),
  fetched: false, page: 1, sort: { col: null, dir: 1 },
  filters: { lib: [], type: [], file: "", jf: "", fav: [], subs: [], subsRe: "", audio: [], audioRe: "", subsok: [] },
  ms: {},
};

const LS_KEY = "jftools.search.filters";
const SORT_KEY = "jftools.search.sort";
const PAGE_SIZE = 50;

// Sentinel values for the "Favorited by" dropdown.
const FAV_ANY = "__any__";
const FAV_NONE = "__none__";

// Subtitle filters, grouped for the dropdown. The same value may appear in
// more than one section; the shared MultiSelect keeps those checkboxes in
// sync, and SUBS_PREDS gives that value one meaning everywhere.
const SUBS_OPTIONS = [
  { section: "Language" },
  { value: "preferred", label: "Preferred languages" },
  { value: "foreign", label: "Foreign languages" },
  { value: "unknown", label: "Unknown" },
  { value: "nosubs", label: "No subtitles" },
  { section: "Acceptable" },
  { value: "acceptable", label: "Acceptable" },
  { value: "acceptable_ai", label: "Acceptable AI" },
  { value: "unknown", label: "Unknown" },
  { value: "not_acceptable", label: "Not acceptable" },
  { section: "Quality" },
  { value: "good", label: "Good subtitle" },
  { value: "forced", label: "Forced subtitles" },
  { value: "tiny", label: "Tiny subtitles" },
  { value: "nosubs", label: "No subtitles" },
  { value: "not_scanned", label: "Not scanned" },
  { value: "failed", label: "Failed probe" },
  { section: "Scan" },
  { value: "not_scanned", label: "Not scanned" },
  { value: "failed", label: "Failed probe" },
  { value: "scanned", label: "Scanned" },
];
const SUBS_VALUES = new Set(SUBS_OPTIONS.filter((o) => o.value !== undefined).map((o) => o.value));
const SUBS_ALIASES = {
  "Acceptable": "acceptable",
  "AI Sub": "acceptable_ai",
  "No acceptable": "not_acceptable",
  "No acceptable (inc. AI)": "not_acceptable",
  "Forced": "forced",
  "Foreign": "foreign",
  "No subs": "nosubs",
  "Not scanned": "not_scanned",
};
const SUBSOK_OPTIONS = [{ value: "yes", label: "Yes" }, { value: "no", label: "No" }];

// Migrate saved filters and profiles written before the grouped options.
function migrateSubs(values) {
  const out = [];
  (values || []).forEach((v) => {
    const mapped = SUBS_ALIASES[v] || v;
    if (SUBS_VALUES.has(mapped) && !out.includes(mapped)) out.push(mapped);
  });
  return out;
}

function seScanState(r) {
  const e = seSubsEntry(r);
  if (e && e.error) return "failed";
  // A series folder has no media file of its own; its episodes hold the tracks.
  if (!e || !e.scanned || e.folder) return "not-scanned";
  return "scanned";
}

// Each group is exhaustive when all its values are selected:
// Language: preferred / foreign / unknown, plus nosubs for a scanned file with
// no tracks. A not-scanned, failed or folder row is "unknown" language.
// Acceptable: acceptable / acceptable_ai, unknown (not scanned, failed or no
// detectable language) and not_acceptable, which is everything else.
// Quality: good / forced / tiny, plus nosubs, not_scanned and failed.
const SUBS_PREDS = {
  preferred: (st, f) => st === "scanned" && !!(f && f.preferred),
  foreign: (st, f) => st === "scanned" && !!(f && f.foreign),
  unknown: (st, f) => st !== "scanned" || !!(f && f.unknown),
  nosubs: (st, f) => st === "scanned" && (!f || !!f.nosubs),
  acceptable: (st, f) => st === "scanned" && !!(f && f.acceptable),
  acceptable_ai: (st, f) => st === "scanned" && !!(f && f.acceptable_ai),
  not_acceptable: (st, f) => st !== "scanned" || !(f && f.acceptable),
  good: (st, f) => st === "scanned" && !!(f && f.good),
  forced: (st, f) => st === "scanned" && !!(f && f.forced),
  tiny: (st, f) => st === "scanned" && !!(f && f.tiny),
  not_scanned: (st) => st === "not-scanned",
  failed: (st) => st === "failed",
  scanned: (st) => st === "scanned",
};

// Audio filter: what the scan found for a file. "Probe failed" covers the three
// error labels the Audio column shows (Not reachable, Not a file, ffprobe
// error); "Not scanned" is reserved for files the scan has not attempted yet.
const AUDIO_OPTIONS = ["No track", "Single track", "Multi track", "Not scanned", "Probe failed"]
  .map((v) => ({ value: v, label: v }));

function seScanEntry(r) { return SE.scans[r.path] || null; }
function seSubsEntry(r) { return seScanEntry(r); }
function seFlags(r) { const e = seSubsEntry(r); return e && e.flags ? e.flags : null; }
function seSubsOk(r) { return SE.subtitlesOk.has(r.path); }

// A failed probe keeps the item unscanned so a later scan retries it, but the
// column should say what went wrong rather than "Not scanned" again. The full
// error (an ffprobe dump, say) stays in the cell's tooltip.
const SE_SUBS_FAIL = { "unreachable": "Not reachable", "not a file": "Not a file" };

function seSubsText(r) {
  const e = seSubsEntry(r);
  if (!e) return { text: "Not scanned", title: "" };
  if (e.folder) {
    return { text: "—", title: "Series folder — its episodes hold the tracks" };
  }
  if (!e.scanned) {
    // A failed probe keeps the entry unscanned so a later scan retries it; say
    // what went wrong only then. No error means it simply hasn't been probed.
    if (e.error) {
      return { text: SE_SUBS_FAIL[e.error] || "Probe failed", title: e.error };
    }
    return { text: "Not scanned", title: "" };
  }
  const text = (e.subs || []).join(", ");
  return { text, title: text };
}

// Everything a subtitle regex can sensibly match: the labels shown in the
// Subtitles column plus the track names and external file names behind them.
function seSubsSearchText(r) {
  const e = seSubsEntry(r);
  if (!e || !e.scanned) return "";
  const bits = [...(e.subs || [])];
  (e.internal || []).forEach((t) => {
    bits.push(t.name, t.lang, t.codec);
    if (t.forced) bits.push("forced");
  });
  (e.external || []).forEach((s) => {
    // Keep the file name last so a regex like ``srt$`` matches it.
    bits.push([s.lang, s.forced ? "forced" : "", s.ai ? "ai" : "", s.file]
      .filter(Boolean).join(" "));
  });
  return bits.filter(Boolean).join(" ");
}

function seAudioText(r) {
  const e = seScanEntry(r);
  if (!e) return { text: "Not scanned", title: "The media scan has not probed this file yet" };
  if (e.folder) {
    return { text: "—", title: "Series folder — its episodes hold the tracks" };
  }
  if (e.error) {
    return { text: SE_SUBS_FAIL[e.error] || "Probe failed", title: e.error };
  }
  if (!e.scanned) {
    return { text: "Not scanned", title: "The media scan has not probed this file yet" };
  }
  const tracks = e.audio || [];
  if (!tracks.length) return { text: "No track", title: "ffprobe found no audio track" };
  const text = tracks.map((t) => t.name || "").join(", ");
  return { text, title: text };
}

// A row matches when *any* selected audio option matches, like the other
// multiselects. The audio regex is applied separately (ANDed) in seVisible().
function seAudioMatch(r, value) {
  const e = seScanEntry(r);
  if (e && e.folder) return false;
  const tracks = (e && e.audio) || [];
  if (value === "Not scanned") return !e || (!e.scanned && !e.error);
  if (value === "Probe failed") return !!(e && e.error);
  if (value === "No track") return !!(e && e.scanned) && tracks.length === 0;
  if (value === "Single track") return tracks.length === 1;
  if (value === "Multi track") return tracks.length > 1;
  return false;
}

// ---- helpers ---------------------------------------------------------------

function seSafeRe(text) {
  if (!text || !text.trim()) return null;
  try { return new RegExp(text, "i"); } catch (e) { return null; }
}

function seBuildActions(r) {
  // Same pill set as Movie Match, including the Google menu.
  return rowActions(r, {
    urls: { jellyfin: SE.jellyfin, radarr: SE.radarr, sonarr: SE.sonarr },
    google: true,
  });
}

async function seToggleSubsOk(path, marked) {
  try {
    await postJSON("/api/subtitles/ok", "path=" + encodeURIComponent(path) + "&marked=" + (marked ? "true" : "false"));
    if (marked) SE.subtitlesOk.add(path); else SE.subtitlesOk.delete(path);
    renderSeTable();
  } catch (e) {}
}

// ---- filtering -------------------------------------------------------------

function seVisible() {
  const f = SE.filters;
  const fileRe = seSafeRe(f.file);
  const jfRe = seSafeRe(f.jf);
  const subsRe = seSafeRe(f.subsRe);
  const audioRe = seSafeRe(f.audioRe);
  return SE.rows.filter((r) => {
    if (f.lib.length && !f.lib.includes(r.library)) return false;
    if (f.type.length && !f.type.includes(r.type)) return false;
    if (f.fav.length) {
      const favSet = new Set(r.favorited_by || []);
      const ok = f.fav.some((v) => {
        if (v === FAV_ANY) return favSet.size > 0;
        if (v === FAV_NONE) return favSet.size === 0;
        return favSet.has(v);
      });
      if (!ok) return false;
    }
    if (f.subs.length) {
      const state = seScanState(r);
      const flags = seFlags(r);
      const ok = f.subs.some((v) => SUBS_PREDS[v] && SUBS_PREDS[v](state, flags));
      if (!ok) return false;
    }
    if (subsRe && !subsRe.test(seSubsSearchText(r))) return false;
    if (f.audio.length && !f.audio.some((v) => seAudioMatch(r, v))) return false;
    if (audioRe) {
      const e = seScanEntry(r);
      const tracks = (e && e.audio) || [];
      if (!tracks.some((t) => audioRe.test(t.name || ""))) return false;
    }
    if (f.subsok.length) {
      const ok = seSubsOk(r) ? "yes" : "no";
      if (!f.subsok.includes(ok)) return false;
    }
    if (fileRe && !fileRe.test(r.file)) return false;
    if (jfRe && !jfRe.test(r.display || r.name)) return false;
    return true;
  });
}

// ---- persistence -----------------------------------------------------------

function saveFilters() {
  try { localStorage.setItem(LS_KEY, JSON.stringify(SE.filters)); } catch (e) {}
}

function loadFilters() {
  try {
    const s = localStorage.getItem(LS_KEY);
    if (!s) return;
    const f = JSON.parse(s);
    if (f && typeof f === "object") {
      SE.filters.lib = Array.isArray(f.lib) ? f.lib : [];
      SE.filters.type = Array.isArray(f.type) ? f.type : [];
      SE.filters.file = typeof f.file === "string" ? f.file : "";
      SE.filters.jf = typeof f.jf === "string" ? f.jf : "";
      SE.filters.fav = Array.isArray(f.fav) ? f.fav : [];
      SE.filters.subs = migrateSubs(f.subs);
      SE.filters.subsRe = typeof f.subsRe === "string" ? f.subsRe : "";
      SE.filters.audio = Array.isArray(f.audio) ? f.audio : [];
      SE.filters.audioRe = typeof f.audioRe === "string" ? f.audioRe : "";
      SE.filters.subsok = Array.isArray(f.subsok) ? f.subsok : [];
    }
  } catch (e) {}
}

// ---- sorting ---------------------------------------------------------------

const SORTABLE = {
  type: (r) => r.type || "",
  path: (r) => r.path || "",
  file: (r) => r.file || "",
  display: (r) => r.display || r.name || "",
  added: (r) => r.added || "",
  fav: (r) => (r.favorited_by || []).join(", "),
  subsok: (r) => (seSubsOk(r) ? 1 : 0),
  subs: (r) => { const e = seSubsEntry(r); return (e && e.subs || []).join(", "); },
  audio: (r) => seAudioText(r).text,
};

function saveSort() {
  try { localStorage.setItem(SORT_KEY, JSON.stringify(SE.sort)); } catch (e) {}
}

function loadSort() {
  try {
    const s = localStorage.getItem(SORT_KEY);
    if (!s) return;
    const o = JSON.parse(s);
    if (o && typeof o === "object" && SORTABLE[o.col]) {
      SE.sort = { col: o.col, dir: o.dir === -1 ? -1 : 1 };
    }
  } catch (e) {}
}

function compareRows(a, b, col, dir) {
  const get = SORTABLE[col];
  if (!get) return 0;
  const va = get(a);
  const vb = get(b);
  const aEmpty = va === null || va === undefined || va === "";
  const bEmpty = vb === null || vb === undefined || vb === "";
  if (aEmpty && bEmpty) return 0;
  if (aEmpty) return 1;  // empties always sort last
  if (bEmpty) return -1;
  return String(va).localeCompare(String(vb), undefined, { sensitivity: "base" }) * dir;
}

function applySort(list) {
  if (!SE.sort.col) return list;
  return list.slice().sort((a, b) => compareRows(a, b, SE.sort.col, SE.sort.dir));
}

function updateSortIndicators() {
  document.querySelectorAll("#se-table th[data-sort]").forEach((th) => {
    const arrow = th.querySelector(".sort-arrow");
    if (arrow) {
      arrow.textContent = th.dataset.sort === SE.sort.col ? (SE.sort.dir === 1 ? "▲" : "▼") : "";
    }
  });
}

function setSort(col) {
  if (SE.sort.col === col) SE.sort.dir = -SE.sort.dir;
  else SE.sort = { col: col, dir: 1 };
  SE.page = 1;
  saveSort();
  renderSeTable();
}

function bindSortHeaders() {
  document.querySelectorAll("#se-table th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => setSort(th.dataset.sort));
  });
}

// ---- rendering -------------------------------------------------------------

function renderSeTable() {
  const body = document.getElementById("se-body");
  const countEl = document.getElementById("se-count");
  const visible = applySort(seVisible());
  body.innerHTML = "";
  updateSortIndicators();

  if (!SE.fetched) {
    body.appendChild(el("tr", {}, [el("td", { class: "muted", colspan: "10", text: "Not fetched yet — click Fetch." })]));
    countEl.textContent = "";
    renderPager(0);
    return;
  }
  if (visible.length === 0) {
    body.appendChild(el("tr", {}, [el("td", { class: "muted", colspan: "10", text: "No items match the filters." })]));
    countEl.textContent = "";
    renderPager(0);
    return;
  }

  const pages = Math.max(1, Math.ceil(visible.length / PAGE_SIZE));
  if (SE.page > pages) SE.page = pages;
  if (SE.page < 1) SE.page = 1;
  const start = (SE.page - 1) * PAGE_SIZE;
  const pageRows = visible.slice(start, start + PAGE_SIZE);

  countEl.textContent = visible.length + " / " + SE.rows.length + " shown";

  pageRows.forEach((r) => {
    const tr = el("tr");

    tr.appendChild(el("td", { text: r.type }));
    const parent = r.path ? r.path.replace(/[\\/][^\\/]*$/, "") : "";
    tr.appendChild(el("td", { class: "muted", text: parent, title: parent }));
    tr.appendChild(el("td", { text: r.file, title: r.file }));
    const nameTd = el("td", { title: r.display || r.name });
    nameTd.appendChild(el("div", { text: r.display || r.name }));
    const episodeLine = episodeSubLine(r);
    if (episodeLine) {
      nameTd.appendChild(el("div", { class: "muted name-sub", text: episodeLine }));
    }
    tr.appendChild(nameTd);
    tr.appendChild(el("td", { class: "muted", text: r.added || "—", title: r.added || "" }));
    const fav = (r.favorited_by || []).join(", ");
    tr.appendChild(el("td", { class: "muted", text: fav, title: fav }));

    // Subtitle Ok — the same ○/✔ mark button as Movie Match's Mismatch Ok.
    const marked = seSubsOk(r);
    const okTd = el("td");
    const okBtn = el("button", { class: "mark-btn", text: marked ? "✔" : "○" });
    okBtn.title = marked ? "Remove subtitle ok mark" : "Mark as subtitle ok";
    okBtn.addEventListener("click", () => seToggleSubsOk(r.path, !marked));
    okTd.appendChild(okBtn);
    tr.appendChild(okTd);

    const subs = seSubsText(r);
    tr.appendChild(el("td", { class: "muted", text: subs.text, title: subs.title || subs.text }));

    const audio = seAudioText(r);
    tr.appendChild(el("td", { class: "muted", text: audio.text, title: audio.title || audio.text }));

    tr.appendChild(seBuildActions(r));
    body.appendChild(tr);
    if (r.id) registerPopup(nameTd, { key: r.id, itemId: r.id });
  });

  renderPager(visible.length);
}

function renderPager(total) {
  const box = document.getElementById("se-pager");
  if (!box) return;
  box.innerHTML = "";
  if (!total) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const mkBtn = (label, disabled, onClick) => {
    const b = el("button", { type: "button", class: "page-btn", text: label });
    b.disabled = disabled;
    b.addEventListener("click", onClick);
    return b;
  };

  box.appendChild(mkBtn("« First", SE.page === 1, () => { SE.page = 1; renderSeTable(); }));
  box.appendChild(mkBtn("‹ Prev", SE.page === 1, () => { SE.page -= 1; renderSeTable(); }));
  box.appendChild(el("span", {
    class: "muted page-info",
    text: "Page " + SE.page + " of " + pages,
  }));
  box.appendChild(mkBtn("Next ›", SE.page === pages, () => { SE.page += 1; renderSeTable(); }));
  box.appendChild(mkBtn("Last »", SE.page === pages, () => { SE.page = pages; renderSeTable(); }));
}

// ---- load / fetch ----------------------------------------------------------

function applyFilterUI() {
  SE.ms.lib.setValues(SE.filters.lib);
  SE.ms.type.setValues(SE.filters.type);
  SE.ms.fav.setValues(SE.filters.fav);
  SE.ms.subs.setValues(SE.filters.subs);
  SE.ms.audio.setValues(SE.filters.audio);
  SE.ms.subsok.setValues(SE.filters.subsok);
  document.getElementById("se-search-file").value = SE.filters.file;
  document.getElementById("se-search-jf").value = SE.filters.jf;
  document.getElementById("se-subs-re").value = SE.filters.subsRe;
  document.getElementById("se-audio-re").value = SE.filters.audioRe;
}

function populateOptions() {
  const libs = [...new Set(SE.rows.map((r) => r.library).filter(Boolean))].sort();
  const types = [...new Set(SE.rows.map((r) => r.type).filter(Boolean))].sort();
  const favs = [...new Set(SE.rows.flatMap((r) => r.favorited_by || []))]
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
  SE.ms.lib.setOptions(libs.map((v) => ({ value: v, label: v })));
  SE.ms.type.setOptions(types.map((v) => ({ value: v, label: v })));
  SE.ms.audio.setOptions(AUDIO_OPTIONS);
  SE.ms.fav.setOptions([
    { value: FAV_ANY, label: "Any" },
    { value: FAV_NONE, label: "None" },
    ...favs.map((v) => ({ value: v, label: v })),
  ]);
}

async function seLoad() {
  const errEl = document.getElementById("se-error");
  try {
    const data = await getJSON("/api/jellyfin-id/items");
    SE.rows = data.rows || [];
    SE.jellyfin = data.jellyfin_url || "";
    SE.radarr = data.radarr_url || "";
    SE.sonarr = data.sonarr_url || "";
    SE.scans = data.scans || {};
    SE.subtitlesOk = new Set(data.subtitles_ok || []);
    SE.fetched = !!data.fetched;
    document.getElementById("se-sync-age").textContent = fmtAgo(data.last_sync);
    populateOptions();
    applyFilterUI();
    renderSeTable();
  } catch (err) {
    errEl.textContent = String(err);
    errEl.hidden = false;
  }
}

async function seFetch() {
  const btn = document.getElementById("se-fetch");
  const prog = document.getElementById("se-progress");
  const fill = document.getElementById("se-progress-fill");
  const statusEl = document.getElementById("se-status");
  btn.disabled = true;
  prog.hidden = false;
  let res;
  try {
    res = await postJSON("/api/jellyfin-id/refresh", null);
  } catch (e) {
    prog.hidden = true;
    btn.disabled = false;
    statusEl.textContent = String(e);
    return;
  }
  if (res && res.started === false) {
    prog.hidden = true;
    btn.disabled = false;
    statusEl.textContent = res.message || "Busy.";
    return;
  }
  startStatusPoll(
    "/api/jellyfin-id/status",
    (s) => {
      if (s.status === "running") {
        const pct = s.total ? Math.round(100 * s.fetched / s.total) : 0;
        fill.style.width = pct + "%";
        statusEl.textContent = "Fetching… " + s.fetched + "/" + (s.total || "?") + " (" + pct + "%)";
      } else {
        statusEl.textContent = s.status === "error" ? "Fetch failed: " + (s.error || "") : "Fetched.";
      }
    },
    async (s) => {
      prog.hidden = true;
      btn.disabled = false;
      if (s.status !== "error") await seLoad();
    },
    600
  );
}

async function seScan() {
  const btn = document.getElementById("se-scan");
  const prog = document.getElementById("se-progress");
  const fill = document.getElementById("se-progress-fill");
  const statusEl = document.getElementById("se-status");
  btn.disabled = true;
  prog.hidden = false;
  statusEl.textContent = "Media scan starting…";
  let res;
  try {
    res = await postJSON("/api/subtitles/scan", "mode=scan");
  } catch (e) {
    prog.hidden = true;
    btn.disabled = false;
    statusEl.textContent = String(e);
    return;
  }
  if (res && res.started === false) {
    prog.hidden = true;
    btn.disabled = false;
    statusEl.textContent = res.message || "Busy.";
    return;
  }
  startStatusPoll(
    "/api/subtitles/status",
    (s) => {
      if (s.status === "running") {
        if (s.phase === "checking") {
          fill.style.width = "0%";
          statusEl.textContent = "Checking subtitle files…";
          return;
        }
        const pct = s.total ? Math.round(100 * s.scanned / s.total) : 0;
        fill.style.width = pct + "%";
        statusEl.textContent = "Media scan… " + s.scanned + "/" + (s.total || "?") + " (" + pct + "%)";
      } else {
        statusEl.textContent = s.status === "error" ? "Media scan failed: " + (s.error || "") : "Media scan complete.";
      }
    },
    async (s) => {
      prog.hidden = true;
      btn.disabled = false;
      if (s.status !== "error") await seLoad();
    },
    600
  );
}

// ---- init ------------------------------------------------------------------

function bindSeControls() {
  const onFilter = () => { SE.page = 1; saveFilters(); renderSeTable(); };

  SE.ms.lib = MultiSelect(document.getElementById("se-lib"), [], (vals) => {
    SE.filters.lib = vals; onFilter();
  });
  SE.ms.type = MultiSelect(document.getElementById("se-type"), [], (vals) => {
    SE.filters.type = vals; onFilter();
  });
  SE.ms.fav = MultiSelect(document.getElementById("se-fav"), [], (vals) => {
    SE.filters.fav = vals; onFilter();
  });
  SE.ms.subs = MultiSelect(document.getElementById("se-subs"), SUBS_OPTIONS, (vals) => {
    SE.filters.subs = vals; onFilter();
  });
  SE.ms.audio = MultiSelect(document.getElementById("se-audio"), AUDIO_OPTIONS, (vals) => {
    SE.filters.audio = vals; onFilter();
  });
  SE.ms.subsok = MultiSelect(document.getElementById("se-subsok"), SUBSOK_OPTIONS, (vals) => {
    SE.filters.subsok = vals; onFilter();
  });

  document.getElementById("se-search-file").addEventListener("input", () => {
    SE.filters.file = document.getElementById("se-search-file").value;
    onFilter();
  });
  document.getElementById("se-search-jf").addEventListener("input", () => {
    SE.filters.jf = document.getElementById("se-search-jf").value;
    onFilter();
  });

  const subsRe = document.getElementById("se-subs-re");
  subsRe.addEventListener("input", () => {
    SE.filters.subsRe = subsRe.value;
    onFilter();
  });

  const audioRe = document.getElementById("se-audio-re");
  audioRe.addEventListener("input", () => {
    SE.filters.audioRe = audioRe.value;
    onFilter();
  });

  document.getElementById("se-fetch").addEventListener("click", seFetch);
  document.getElementById("se-scan").addEventListener("click", seScan);
}

document.addEventListener("DOMContentLoaded", () => {
  loadFilters();
  loadSort();
  bindSeControls();
  bindSortHeaders();
  window.subdlRefresh = () => seLoad();
  // An internal track edit returns the freshly probed entry; fold it straight
  // into the table instead of refetching the whole (large) items payload.
  window.audioRefresh = (path, entry) => {
    if (path && entry) SE.scans[path] = entry;
    populateOptions();
    applyFilterUI();
    renderSeTable();
  };
  FilterProfiles(document.getElementById("se-profiles"),
    "search",
    () => ({ filters: SE.filters, sort: SE.sort }),
    (p) => {
      const f = (p && p.filters) ? p.filters : (p || {});
      SE.filters = {
        lib: Array.isArray(f.lib) ? f.lib : [],
        type: Array.isArray(f.type) ? f.type : [],
        file: typeof f.file === "string" ? f.file : "",
        jf: typeof f.jf === "string" ? f.jf : "",
        fav: Array.isArray(f.fav) ? f.fav : [],
        subs: migrateSubs(f.subs),
        subsRe: typeof f.subsRe === "string" ? f.subsRe : "",
        audio: Array.isArray(f.audio) ? f.audio : [],
        audioRe: typeof f.audioRe === "string" ? f.audioRe : "",
        subsok: Array.isArray(f.subsok) ? f.subsok : [],
      };
      const s = (p && p.sort) || {};
      if (SORTABLE[s.col]) SE.sort = { col: s.col, dir: s.dir === -1 ? -1 : 1 };
      SE.page = 1;
      saveFilters();
      saveSort();
      applyFilterUI();
      renderSeTable();
    });
  seLoad();
});
