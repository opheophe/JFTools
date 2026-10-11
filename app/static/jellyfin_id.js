"use strict";

const ID = {
  rows: [], marks: new Set(), jellyfin: "", radarr: "", sonarr: "",
  fetched: false, timer: null,
  filters: { lib: [], type: [], ok: [], match: [], ymatch: [], file: "", jf: "" },
  ms: {},
};

const LS_KEY = "medserv.jellyfinId.filters";

// ---- helpers ---------------------------------------------------------------

function idEnc(s) { return encodeURIComponent(s || ""); }

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

function idFmtAgo(ts) {
  if (!ts) return "Not fetched";
  const d = Math.max(0, Math.round((Date.now() / 1000) - ts));
  if (d < 60) return "Fetched " + d + "s ago";
  const m = Math.floor(d / 60);
  if (m < 60) return "Fetched " + m + "m ago";
  const h = Math.floor(m / 60);
  if (h < 24) return "Fetched " + h + "h " + (m % 60) + "m ago";
  return "Fetched " + Math.floor(h / 24) + "d ago";
}

function idFallbackCopy(text, done) {
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

function idCopy(text, btn) {
  const done = () => {
    const o = btn.textContent;
    btn.textContent = "✓";
    setTimeout(() => { btn.textContent = o; }, 800);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done).catch(() => idFallbackCopy(text, done));
  } else {
    idFallbackCopy(text, done);
  }
}

function idLink(href, text) {
  return el("a", { href: href, target: "_blank", rel: "noopener", text: text, class: "id-link" });
}

function idBuildActions(r) {
  const td = el("td", { class: "id-actions" });
  if (r.id && ID.jellyfin) {
    td.appendChild(idLink(ID.jellyfin.replace(/\/+$/, "") + "/web/#/details?id=" + r.id, "Jellyfin"));
  }
  if (r.type === "Movie" && ID.radarr) {
    td.appendChild(idLink(ID.radarr + idEnc(r.name), "Radarr"));
  } else if (r.type === "Episode" && ID.sonarr) {
    td.appendChild(idLink(ID.sonarr + idEnc(r.series || r.name), "Sonarr"));
  }
  td.appendChild(idLink("https://www.google.com/search?q=" + idEnc(r.name), "Google"));

  const cp = el("button", { class: "mini", text: "path" });
  cp.addEventListener("click", () => idCopy(r.path, cp));
  td.appendChild(cp);
  const ci = el("button", { class: "mini", text: "id" });
  ci.addEventListener("click", () => idCopy(r.id, ci));
  td.appendChild(ci);
  return td;
}

// ---- multi-select ----------------------------------------------------------

function MultiSelect(container, options, onChange) {
  let opts = options || [];
  const selected = new Set();
  const btn = el("button", { type: "button", class: "ms-btn", text: "All" });
  const menu = el("div", { class: "ms-menu" });
  menu.hidden = true;
  container.appendChild(btn);
  container.appendChild(menu);

  const labelFor = (v) => {
    const o = opts.find((x) => x.value === v);
    return o ? o.label : v;
  };

  function renderButton() {
    if (selected.size === 0 || selected.size === opts.length) {
      btn.textContent = "All";
    } else if (selected.size <= 2) {
      btn.textContent = [...selected].map(labelFor).join(", ");
    } else {
      btn.textContent = selected.size + " selected";
    }
  }

  function renderMenu() {
    menu.innerHTML = "";
    opts.forEach((o) => {
      const row = el("label", { class: "ms-item" });
      const cb = el("input", { type: "checkbox" });
      cb.checked = selected.has(o.value);
      cb.addEventListener("change", () => {
        if (cb.checked) selected.add(o.value); else selected.delete(o.value);
        renderButton();
        onChange([...selected]);
      });
      row.appendChild(cb);
      row.appendChild(el("span", { text: o.label }));
      menu.appendChild(row);
    });
  }

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    menu.hidden = !menu.hidden;
  });

  document.addEventListener("click", (e) => {
    if (!container.contains(e.target)) menu.hidden = true;
  });

  function setOptions(newOptions) {
    opts = newOptions || [];
    const valid = new Set(opts.map((o) => o.value));
    [...selected].forEach((v) => { if (!valid.has(v)) selected.delete(v); });
    renderButton();
    renderMenu();
  }

  function setValues(vals) {
    selected.clear();
    (vals || []).forEach((v) => { if (opts.some((o) => o.value === v)) selected.add(v); });
    renderButton();
    renderMenu();
  }

  renderButton();
  renderMenu();
  return { setOptions, setValues, getValue: () => [...selected] };
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
    const marked = ID.marks.has(r.id);
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

// ---- rendering -------------------------------------------------------------

function renderIdTable() {
  const body = document.getElementById("id-body");
  const countEl = document.getElementById("id-count");
  const visible = idVisible();
  body.innerHTML = "";

  if (!ID.fetched) {
    body.appendChild(el("tr", {}, [el("td", { class: "muted", colspan: "8", text: "Not fetched yet — click Fetch." })]));
    countEl.textContent = "";
    return;
  }
  if (visible.length === 0) {
    body.appendChild(el("tr", {}, [el("td", { class: "muted", colspan: "8", text: "No items match the filters." })]));
    countEl.textContent = "";
    return;
  }
  countEl.textContent = visible.length + " / " + ID.rows.length + " shown";

  visible.forEach((r) => {
    const tr = el("tr");
    const marked = ID.marks.has(r.id);
    const okTd = el("td");
    const markBtn = el("button", { class: "mark-btn", text: marked ? "✔" : "○" });
    markBtn.title = marked ? "Remove ok mark" : "Mark as ok";
    markBtn.addEventListener("click", () => idToggleMark(r.id, !marked));
    okTd.appendChild(markBtn);
    tr.appendChild(okTd);
    tr.appendChild(el("td", { text: r.type }));
    const parent = r.path ? r.path.replace(/[\\/][^\\/]*$/, "") : "";
    tr.appendChild(el("td", { class: "muted", text: parent, title: parent }));
    tr.appendChild(el("td", { text: r.file, title: r.file }));
    tr.appendChild(el("td", { text: r.name ? r.match + "%" : "" }));
    tr.appendChild(el("td", { text: r.year_match || "—" }));
    tr.appendChild(el("td", { text: r.display || r.name, title: r.display || r.name }));
    tr.appendChild(idBuildActions(r));
    body.appendChild(tr);
  });
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
    document.getElementById("id-sync-age").textContent = idFmtAgo(data.last_sync);
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
  btn.disabled = true;
  try {
    await postJSON("/api/jellyfin-id/refresh", null);
  } catch (e) {
    document.getElementById("id-status").textContent = String(e);
    btn.disabled = false;
    return;
  }
  pollIdStatus(btn);
}

function pollIdStatus(btn) {
  const prog = document.getElementById("id-progress");
  const fill = document.getElementById("id-progress-fill");
  const statusEl = document.getElementById("id-status");
  prog.hidden = false;
  const tick = async () => {
    try {
      const s = await getJSON("/api/jellyfin-id/status");
      if (s.status === "running") {
        const pct = s.total ? Math.round(100 * s.fetched / s.total) : 0;
        fill.style.width = pct + "%";
        statusEl.textContent = "Fetching… " + s.fetched + "/" + (s.total || "?") + " (" + pct + "%)";
        ID.timer = setTimeout(tick, 600);
      } else {
        prog.hidden = true;
        btn.disabled = false;
        if (s.status === "error") {
          statusEl.textContent = "Fetch failed: " + (s.error || "");
        } else {
          statusEl.textContent = "Fetched.";
          await idLoad();
        }
      }
    } catch (err) {
      prog.hidden = true;
      btn.disabled = false;
      statusEl.textContent = String(err);
    }
  };
  tick();
}

// ---- init ------------------------------------------------------------------

function bindIdControls() {
  ID.ms.lib = MultiSelect(document.getElementById("id-lib"), [], (vals) => {
    ID.filters.lib = vals; saveFilters(); renderIdTable();
  });
  ID.ms.type = MultiSelect(document.getElementById("id-type"), [], (vals) => {
    ID.filters.type = vals; saveFilters(); renderIdTable();
  });
  ID.ms.ok = MultiSelect(document.getElementById("id-ok"), OK_OPTIONS, (vals) => {
    ID.filters.ok = vals; saveFilters(); renderIdTable();
  });
  ID.ms.match = MultiSelect(document.getElementById("id-match"), MATCH_OPTIONS, (vals) => {
    ID.filters.match = vals; saveFilters(); renderIdTable();
  });
  ID.ms.ymatch = MultiSelect(document.getElementById("id-ymatch"), YMATCH_OPTIONS, (vals) => {
    ID.filters.ymatch = vals; saveFilters(); renderIdTable();
  });

  document.getElementById("id-search-file").addEventListener("input", () => {
    ID.filters.file = document.getElementById("id-search-file").value;
    saveFilters(); renderIdTable();
  });
  document.getElementById("id-search-jf").addEventListener("input", () => {
    ID.filters.jf = document.getElementById("id-search-jf").value;
    saveFilters(); renderIdTable();
  });

  document.getElementById("id-fetch").addEventListener("click", idFetch);
}

document.addEventListener("DOMContentLoaded", () => {
  loadFilters();
  bindIdControls();
  idLoad();
});
