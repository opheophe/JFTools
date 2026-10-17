"use strict";

const SE = {
  rows: [], jellyfin: "", radarr: "", sonarr: "",
  fetched: false, page: 1, sort: { col: null, dir: 1 },
  filters: { lib: [], type: [], file: "", jf: "", fav: [], favMode: "any" },
  ms: {},
};

const LS_KEY = "jftools.search.filters";
const SORT_KEY = "jftools.search.sort";
const PAGE_SIZE = 50;

// ---- helpers ---------------------------------------------------------------

function seEnc(s) { return encodeURIComponent(s || ""); }

function seSafeRe(text) {
  if (!text || !text.trim()) return null;
  try { return new RegExp(text, "i"); } catch (e) { return null; }
}

function seFallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try { document.execCommand("copy"); } catch (e) {}
  document.body.removeChild(ta);
  done();
}

function seCopy(text, btn) {
  const done = () => {
    const o = btn.textContent;
    btn.textContent = "✓";
    setTimeout(() => { btn.textContent = o; }, 800);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done).catch(() => seFallbackCopy(text, done));
  } else {
    seFallbackCopy(text, done);
  }
}

function seLink(href, text) {
  return el("a", { href: href, target: "_blank", rel: "noopener", text: text, class: "id-link" });
}

function seBuildActions(r) {
  const td = el("td", { class: "id-actions" });
  if (r.id && SE.jellyfin) {
    td.appendChild(seLink(SE.jellyfin.replace(/\/+$/, "") + "/web/#/details?id=" + r.id, "Jellyfin"));
  }
  if ((r.type === "Movie" || r.type === "Video") && SE.radarr) {
    td.appendChild(seLink(SE.radarr + seEnc(r.name), "Radarr"));
  } else if ((r.type === "Series" || r.type === "Episode") && SE.sonarr) {
    td.appendChild(seLink(SE.sonarr + seEnc(r.series || r.name), "Sonarr"));
  }
  const cp = el("button", { class: "mini", text: "path" });
  cp.addEventListener("click", () => seCopy(r.path, cp));
  td.appendChild(cp);
  const ci = el("button", { class: "mini", text: "id" });
  ci.addEventListener("click", () => seCopy(r.id, ci));
  td.appendChild(ci);
  return td;
}

// ---- filtering -------------------------------------------------------------

function seVisible() {
  const f = SE.filters;
  const fileRe = seSafeRe(f.file);
  const jfRe = seSafeRe(f.jf);
  return SE.rows.filter((r) => {
    if (f.lib.length && !f.lib.includes(r.library)) return false;
    if (f.type.length && !f.type.includes(r.type)) return false;
    if (f.fav.length) {
      const favSet = new Set(r.favorited_by || []);
      const ok = f.favMode === "all"
        ? f.fav.every((n) => favSet.has(n))
        : f.fav.some((n) => favSet.has(n));
      if (!ok) return false;
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
      SE.filters.favMode = f.favMode === "all" ? "all" : "any";
    }
  } catch (e) {}
}

// ---- sorting ---------------------------------------------------------------

const SORTABLE = {
  type: (r) => r.type || "",
  path: (r) => r.path || "",
  file: (r) => r.file || "",
  display: (r) => r.display || r.name || "",
  fav: (r) => (r.favorited_by || []).join(", "),
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
    body.appendChild(el("tr", {}, [el("td", { class: "muted", colspan: "6", text: "Not fetched yet — click Fetch." })]));
    countEl.textContent = "";
    renderPager(0);
    return;
  }
  if (visible.length === 0) {
    body.appendChild(el("tr", {}, [el("td", { class: "muted", colspan: "6", text: "No items match the filters." })]));
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
    tr.appendChild(el("td", { text: r.display || r.name, title: r.display || r.name }));
    const fav = (r.favorited_by || []).join(", ");
    tr.appendChild(el("td", { class: "muted", text: fav, title: fav }));
    tr.appendChild(seBuildActions(r));
    body.appendChild(tr);
    if (r.id) registerPopup(tr, { key: r.id, itemId: r.id });
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
  document.getElementById("se-search-file").value = SE.filters.file;
  document.getElementById("se-search-jf").value = SE.filters.jf;
  document.getElementById("se-fav-mode").value = SE.filters.favMode;
}

function populateOptions() {
  const libs = [...new Set(SE.rows.map((r) => r.library).filter(Boolean))].sort();
  const types = [...new Set(SE.rows.map((r) => r.type).filter(Boolean))].sort();
  const favs = [...new Set(SE.rows.flatMap((r) => r.favorited_by || []))]
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
  SE.ms.lib.setOptions(libs.map((v) => ({ value: v, label: v })));
  SE.ms.type.setOptions(types.map((v) => ({ value: v, label: v })));
  SE.ms.fav.setOptions(favs.map((v) => ({ value: v, label: v })));
}

async function seLoad() {
  const errEl = document.getElementById("se-error");
  try {
    const data = await getJSON("/api/jellyfin-id/items");
    SE.rows = data.rows || [];
    SE.jellyfin = data.jellyfin_url || "";
    SE.radarr = data.radarr_url || "";
    SE.sonarr = data.sonarr_url || "";
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

  document.getElementById("se-search-file").addEventListener("input", () => {
    SE.filters.file = document.getElementById("se-search-file").value;
    onFilter();
  });
  document.getElementById("se-search-jf").addEventListener("input", () => {
    SE.filters.jf = document.getElementById("se-search-jf").value;
    onFilter();
  });
  document.getElementById("se-fav-mode").addEventListener("change", () => {
    SE.filters.favMode = document.getElementById("se-fav-mode").value;
    onFilter();
  });

  document.getElementById("se-fetch").addEventListener("click", seFetch);
}

document.addEventListener("DOMContentLoaded", () => {
  loadFilters();
  loadSort();
  bindSeControls();
  bindSortHeaders();
  seLoad();
});
