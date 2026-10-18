"use strict";

const ID = {
  rows: [], marks: new Set(), jellyfin: "", radarr: "", sonarr: "",
  fetched: false, page: 1, sort: { col: null, dir: 1 },
  filters: { lib: [], type: [], ok: [], match: [], ymatch: [], file: "", jf: "" },
  ms: {},
};

const LS_KEY = "jftools.moviematch.filters";
const SORT_KEY = "jftools.moviematch.sort";
const PAGE_SIZE = 50;

// ---- helpers ---------------------------------------------------------------

function idEnc(s) { return encodeURIComponent(s || ""); }

// A row's "ok" identity is the file + Jellyfin id, so a replaced file (or a
// re-matched item) is no longer considered ok.
function idMarkKey(r) {
  return (r.id || "") + "|" + (r.file || "");
}

function idSafeRe(text) {
  if (!text || !text.trim()) return null;
  try { return new RegExp(text, "i"); } catch (e) { return null; }
}

function idInRanges(score, ranges) {
  for (const r of ranges) {
    if (r === "100") { if (score >= 100) return true; }
    else {
      const p = r.split("-");
      if (score >= Number(p[0]) && score <= Number(p[1])) return true;
    }
  }
  return false;
}

// Title used for external lookups: the show for series/episodes, else the name.
function idSearchTitle(r) {
  if (r.type === "Series" || r.type === "Episode") return r.series || r.name || "";
  return r.name || "";
}

// Candidate years: the on-disk year(s) from the filename/folder, then Jellyfin's.
function idYears(r) {
  const yrs = [];
  const grab = (s) => {
    if (!s) return;
    const re = /\b(?:19|20)\d{2}\b/g;
    let m;
    while ((m = re.exec(s)) !== null) {
      const y = parseInt(m[0], 10);
      if (y >= 1900 && y <= 2100 && yrs.indexOf(y) === -1) yrs.push(y);
    }
  };
  grab(r.file);
  if (r.path) {
    grab(r.path.replace(/[\\/][^\\/]*$/, "").split(/[\\/]/).pop());
  }
  if (r.year != null && yrs.indexOf(r.year) === -1) yrs.push(r.year);
  return yrs;
}

// A cleaned title from the on-disk filename (extension/release tags stripped).
function idDiskTitle(r) {
  let s = (r.file || "").replace(/\.[A-Za-z0-9]{2,5}$/, "");
  s = s.replace(/[._]+/g, " ");
  s = s.replace(/[\[\](){}]/g, " ");
  s = s.replace(/\b(2160p|1080p|720p|480p|4k|uhd|hdr10?|hdr|dv|sdr|bluray|blu-?ray|brrip|bdrip|bd-?remux|remux|webrip|web-?dl|webdl|web|hdrip|dvdrip|dvd|hdtv|cam|x264|x265|h264|h265|hevc|avc|xvid|divx|10bit|8bit|aac|ac3|eac3|ddp?|dts|truehd|atmos|flac|mp3|5\.1|7\.1|2\.0|yts|yify|rarbg|proper|repack|extended|unrated|remastered|multi|dual\s?audio|amzn|nf|hulu|itunes)\b/gi, " ");
  s = s.replace(/[^\p{L}\p{N}]+/gu, " ");
  return s.replace(/\s+/g, " ").trim();
}

// Google search queries for the hover menu on the "Google" button.
function idGoogleQueries(r) {
  const title = idSearchTitle(r);
  if (!title) return [];
  const year = r.year ? String(r.year) : "";
  const years = idYears(r);

  let yq = "Which year was " + title + " released";
  if (years.length >= 2) yq += "; was it " + years[0] + " or " + years[1];
  else if (years.length === 1) yq += "; was it " + years[0];

  const items = [
    { label: "Ask about year", q: yq },
    { label: "IMDB", q: title + (year ? " " + year : "") + " imdb" },
    { label: "Language", q: "What is the original language of " + title + (year ? " " + year : "") },
  ];
  const disk = idDiskTitle(r);
  if (disk) {
    items.unshift({ label: "Same movie", q: 'Is "' + disk + '" the same movie as "' + title + '"' });
  }
  return items;
}

function idGoogleMenu(r) {
  const wrap = el("span", { class: "id-google" });
  const btn = el("button", { type: "button", class: "id-google-btn", text: "Google" });
  btn.appendChild(el("span", { class: "id-google-caret", text: "▾" }));
  wrap.appendChild(btn);

  const items = idGoogleQueries(r);
  if (!items.length) {
    btn.disabled = true;
    return wrap;
  }

  const menu = el("div", { class: "id-google-menu" });
  menu.hidden = true;
  items.forEach((it) => {
    menu.appendChild(el("a", {
      class: "id-google-item",
      href: "https://www.google.com/search?q=" + idEnc(it.q),
      target: "_blank",
      rel: "noopener",
      text: it.label,
      title: it.q,
    }));
  });
  wrap.appendChild(menu);

  let hideTimer = null;
  const show = () => {
    clearTimeout(hideTimer);
    menu.hidden = false;
    const rct = wrap.getBoundingClientRect();
    const mw = menu.offsetWidth;
    const mh = menu.offsetHeight;
    let left = Math.round(rct.left);
    left = Math.max(8, Math.min(left, window.innerWidth - mw - 8));
    let top = Math.round(rct.bottom + 4);
    if (top + mh > window.innerHeight - 8) top = Math.max(8, rct.top - mh - 4);
    menu.style.left = left + "px";
    menu.style.top = top + "px";
  };
  const scheduleHide = () => {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => { menu.hidden = true; }, 180);
  };

  wrap.addEventListener("mouseenter", show);
  wrap.addEventListener("mouseleave", scheduleHide);
  menu.addEventListener("mouseenter", () => clearTimeout(hideTimer));
  menu.addEventListener("mouseleave", scheduleHide);

  return wrap;
}

function idBuildActions(r) {
  const td = el("td", { class: "id-actions" });

  const openItems = [];
  if (r.id && ID.jellyfin) {
    openItems.push(hoverMenuLink(ID.jellyfin.replace(/\/+$/, "") + "/web/#/details?id=" + r.id, "Jellyfin"));
  }
  if ((r.type === "Movie" || r.type === "Video") && ID.radarr) {
    openItems.push(hoverMenuLink(ID.radarr + idEnc(r.name), "Radarr"));
  } else if ((r.type === "Series" || r.type === "Episode") && ID.sonarr) {
    openItems.push(hoverMenuLink(ID.sonarr + idEnc(r.series || r.name), "Sonarr"));
  }
  td.appendChild(hoverMenuButton("Open in", openItems));

  const actItems = [];
  if (r.path) {
    actItems.push(hoverMenuAction("Rescan library file", (node) => rescanFile(node, r.path)));
    actItems.push(hoverMenuAction("Rescan sub", (node) => rescanSubtitle(node, r.path)));
  }
  td.appendChild(hoverMenuButton("Actions", actItems));

  td.appendChild(idGoogleMenu(r));
  return td;
}

const MATCH_OPTIONS = ["0-20", "20-40", "40-60", "60-80", "80-99", "100"].map((v) => ({ value: v, label: v }));
const OK_OPTIONS = [{ value: "yes", label: "Yes" }, { value: "no", label: "No" }];
const YMATCH_OPTIONS = ["No year", "Correct", "+/-1 year", "Mismatch"].map((v) => ({ value: v, label: v }));

// ---- filtering -------------------------------------------------------------

function idVisible() {
  const f = ID.filters;
  const fileRe = idSafeRe(f.file);
  const jfRe = idSafeRe(f.jf);
  return ID.rows.filter((r) => {
    if (f.lib.length && !f.lib.includes(r.library)) return false;
    if (f.type.length && !f.type.includes(r.type)) return false;
    if (f.match.length && !f.match.some((range) => idInRanges(r.match, [range]))) return false;
    if (f.ymatch.length && !f.ymatch.includes(r.year_match || "")) return false;
    if (fileRe && !fileRe.test(r.file)) return false;
    if (jfRe && !jfRe.test(r.display || r.name)) return false;
    const marked = ID.marks.has(idMarkKey(r));
    const rowOk = marked ? "yes" : "no";
    if (f.ok.length && !f.ok.includes(rowOk)) return false;
    return true;
  });
}

// ---- persistence -----------------------------------------------------------

function saveFilters() {
  try { localStorage.setItem(LS_KEY, JSON.stringify(ID.filters)); } catch (e) {}
}

function loadFilters() {
  try {
    const s = localStorage.getItem(LS_KEY);
    if (!s) return;
    const f = JSON.parse(s);
    if (f && typeof f === "object") {
      ID.filters.lib = Array.isArray(f.lib) ? f.lib : [];
      ID.filters.type = Array.isArray(f.type) ? f.type : [];
      ID.filters.ok = Array.isArray(f.ok) ? f.ok : [];
      ID.filters.match = Array.isArray(f.match) ? f.match : [];
      ID.filters.ymatch = Array.isArray(f.ymatch) ? f.ymatch : [];
      ID.filters.file = typeof f.file === "string" ? f.file : "";
      ID.filters.jf = typeof f.jf === "string" ? f.jf : "";
    }
  } catch (e) {}
}

// ---- sorting ---------------------------------------------------------------

// Year-match categories in severity order (worst first), not alphabetical.
const YEAR_ORDER = ["No year", "Mismatch", "+/-1 year", "Correct"];

// Sortable column registry: value getter for each header's data-sort key.
const SORTABLE = {
  ok: (r) => (ID.marks.has(idMarkKey(r)) ? 1 : 0),
  type: (r) => r.type || "",
  path: (r) => r.path || "",
  file: (r) => r.file || "",
  match: (r) => r.match,
  year_match: (r) => YEAR_ORDER.indexOf(r.year_match || "No year"),
  display: (r) => r.display || r.name || "",
};

function saveSort() {
  try { localStorage.setItem(SORT_KEY, JSON.stringify(ID.sort)); } catch (e) {}
}

function loadSort() {
  try {
    const s = localStorage.getItem(SORT_KEY);
    if (!s) return;
    const o = JSON.parse(s);
    if (o && typeof o === "object" && SORTABLE[o.col]) {
      ID.sort = { col: o.col, dir: o.dir === -1 ? -1 : 1 };
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
  let cmp;
  if (typeof va === "number" && typeof vb === "number") cmp = va - vb;
  else cmp = String(va).localeCompare(String(vb), undefined, { sensitivity: "base" });
  return cmp * dir;
}

function applySort(list) {
  if (!ID.sort.col) return list;
  return list.slice().sort((a, b) => compareRows(a, b, ID.sort.col, ID.sort.dir));
}

function updateSortIndicators() {
  document.querySelectorAll("#id-table th[data-sort]").forEach((th) => {
    const arrow = th.querySelector(".sort-arrow");
    if (arrow) {
      arrow.textContent = th.dataset.sort === ID.sort.col ? (ID.sort.dir === 1 ? "▲" : "▼") : "";
    }
  });
}

function setSort(col) {
  if (ID.sort.col === col) ID.sort.dir = -ID.sort.dir;
  else ID.sort = { col: col, dir: 1 };
  ID.page = 1;
  saveSort();
  renderIdTable();
}

function bindSortHeaders() {
  document.querySelectorAll("#id-table th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => setSort(th.dataset.sort));
  });
}

// ---- rendering -------------------------------------------------------------

function renderIdTable() {
  const body = document.getElementById("id-body");
  const countEl = document.getElementById("id-count");
  const visible = applySort(idVisible());
  body.innerHTML = "";
  updateSortIndicators();

  if (!ID.fetched) {
    body.appendChild(el("tr", {}, [el("td", { class: "muted", colspan: "8", text: "Not fetched yet — click Fetch." })]));
    countEl.textContent = "";
    renderPager(0);
    return;
  }
  if (visible.length === 0) {
    body.appendChild(el("tr", {}, [el("td", { class: "muted", colspan: "8", text: "No items match the filters." })]));
    countEl.textContent = "";
    renderPager(0);
    return;
  }

  const pages = Math.max(1, Math.ceil(visible.length / PAGE_SIZE));
  if (ID.page > pages) ID.page = pages;
  if (ID.page < 1) ID.page = 1;
  const start = (ID.page - 1) * PAGE_SIZE;
  const pageRows = visible.slice(start, start + PAGE_SIZE);

  countEl.textContent = visible.length + " / " + ID.rows.length + " shown";

  pageRows.forEach((r) => {
    const tr = el("tr");
    tr.appendChild(el("td", { text: r.type }));
    const parent = r.path ? r.path.replace(/[\\/][^\\/]*$/, "") : "";
    tr.appendChild(el("td", { class: "muted", text: parent, title: parent }));
    tr.appendChild(el("td", { text: r.file, title: r.file }));
    tr.appendChild(el("td", { text: r.name ? r.match + "%" : "" }));
    tr.appendChild(el("td", { text: r.year_match || "—" }));
    const marked = ID.marks.has(idMarkKey(r));
    const okTd = el("td");
    const markBtn = el("button", { class: "mark-btn", text: marked ? "✔" : "○" });
    markBtn.title = marked ? "Remove mismatch ok mark" : "Mark as mismatch ok";
    markBtn.addEventListener("click", () => idToggleMark(idMarkKey(r), !marked));
    okTd.appendChild(markBtn);
    tr.appendChild(okTd);
    tr.appendChild(el("td", { text: r.display || r.name, title: r.display || r.name }));
    tr.appendChild(idBuildActions(r));
    body.appendChild(tr);
    if (r.id) registerPopup(tr, { key: r.id, itemId: r.id });
  });

  renderPager(visible.length);
}

function renderPager(total) {
  const box = document.getElementById("id-pager");
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

  box.appendChild(mkBtn("« First", ID.page === 1, () => { ID.page = 1; renderIdTable(); }));
  box.appendChild(mkBtn("‹ Prev", ID.page === 1, () => { ID.page -= 1; renderIdTable(); }));
  box.appendChild(el("span", {
    class: "muted page-info",
    text: "Page " + ID.page + " of " + pages,
  }));
  box.appendChild(mkBtn("Next ›", ID.page === pages, () => { ID.page += 1; renderIdTable(); }));
  box.appendChild(mkBtn("Last »", ID.page === pages, () => { ID.page = pages; renderIdTable(); }));
}

async function idToggleMark(id, marked) {
  try {
    await postJSON("/api/jellyfin-id/mark",
      "item_id=" + encodeURIComponent(id) + "&marked=" + (marked ? "true" : "false"));
    if (marked) ID.marks.add(id); else ID.marks.delete(id);
    renderIdTable();
  } catch (e) {}
}

// ---- load / fetch ----------------------------------------------------------

function applyFilterUI() {
  ID.ms.lib.setValues(ID.filters.lib);
  ID.ms.type.setValues(ID.filters.type);
  ID.ms.ok.setValues(ID.filters.ok);
  ID.ms.match.setValues(ID.filters.match);
  ID.ms.ymatch.setValues(ID.filters.ymatch);
  document.getElementById("id-search-file").value = ID.filters.file;
  document.getElementById("id-search-jf").value = ID.filters.jf;
}

function populateOptions() {
  const libs = [...new Set(ID.rows.map((r) => r.library).filter(Boolean))].sort();
  const types = [...new Set(ID.rows.map((r) => r.type).filter(Boolean))].sort();
  ID.ms.lib.setOptions(libs.map((v) => ({ value: v, label: v })));
  ID.ms.type.setOptions(types.map((v) => ({ value: v, label: v })));
}

async function idLoad() {
  const errEl = document.getElementById("id-error");
  try {
    const data = await getJSON("/api/jellyfin-id/items");
    ID.rows = data.rows || [];
    ID.marks = new Set(data.marks || []);
    ID.jellyfin = data.jellyfin_url || "";
    ID.radarr = data.radarr_url || "";
    ID.sonarr = data.sonarr_url || "";
    ID.fetched = !!data.fetched;
    document.getElementById("id-sync-age").textContent = fmtAgo(data.last_sync);
    populateOptions();
    applyFilterUI();
    renderIdTable();
  } catch (err) {
    errEl.textContent = String(err);
    errEl.hidden = false;
  }
}

async function idFetch() {
  const btn = document.getElementById("id-fetch");
  const prog = document.getElementById("id-progress");
  const fill = document.getElementById("id-progress-fill");
  const statusEl = document.getElementById("id-status");
  btn.disabled = true;
  prog.hidden = false;
  try {
    await postJSON("/api/jellyfin-id/refresh", null);
  } catch (e) {
    prog.hidden = true;
    btn.disabled = false;
    statusEl.textContent = String(e);
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
      if (s.status !== "error") await idLoad();
    },
    600
  );
}

// ---- init ------------------------------------------------------------------

function bindIdControls() {
  const onFilter = () => { ID.page = 1; saveFilters(); renderIdTable(); };

  ID.ms.lib = MultiSelect(document.getElementById("id-lib"), [], (vals) => {
    ID.filters.lib = vals; onFilter();
  });
  ID.ms.type = MultiSelect(document.getElementById("id-type"), [], (vals) => {
    ID.filters.type = vals; onFilter();
  });
  ID.ms.ok = MultiSelect(document.getElementById("id-ok"), OK_OPTIONS, (vals) => {
    ID.filters.ok = vals; onFilter();
  });
  ID.ms.match = MultiSelect(document.getElementById("id-match"), MATCH_OPTIONS, (vals) => {
    ID.filters.match = vals; onFilter();
  });
  ID.ms.ymatch = MultiSelect(document.getElementById("id-ymatch"), YMATCH_OPTIONS, (vals) => {
    ID.filters.ymatch = vals; onFilter();
  });

  document.getElementById("id-search-file").addEventListener("input", () => {
    ID.filters.file = document.getElementById("id-search-file").value;
    onFilter();
  });
  document.getElementById("id-search-jf").addEventListener("input", () => {
    ID.filters.jf = document.getElementById("id-search-jf").value;
    onFilter();
  });

  document.getElementById("id-fetch").addEventListener("click", idFetch);
}

document.addEventListener("DOMContentLoaded", () => {
  loadFilters();
  loadSort();
  bindIdControls();
  bindSortHeaders();
  FilterProfiles(document.getElementById("id-profiles"),
    "jftools.moviematch.profiles",
    () => ({ filters: ID.filters, sort: ID.sort }),
    (p) => {
      const f = (p && p.filters) ? p.filters : (p || {});
      ID.filters = {
        lib: Array.isArray(f.lib) ? f.lib : [],
        type: Array.isArray(f.type) ? f.type : [],
        ok: Array.isArray(f.ok) ? f.ok : [],
        match: Array.isArray(f.match) ? f.match : [],
        ymatch: Array.isArray(f.ymatch) ? f.ymatch : [],
        file: typeof f.file === "string" ? f.file : "",
        jf: typeof f.jf === "string" ? f.jf : "",
      };
      const s = (p && p.sort) || {};
      if (SORTABLE[s.col]) ID.sort = { col: s.col, dir: s.dir === -1 ? -1 : 1 };
      ID.page = 1;
      saveFilters();
      saveSort();
      applyFilterUI();
      renderIdTable();
    });
  idLoad();
});
