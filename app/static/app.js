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

  if (data.type === "kv") {
    const grid = el("div", { class: "kv" });
    data.items.forEach(([k, v]) => {
      grid.appendChild(el("span", { class: "k", text: k }));
      grid.appendChild(el("span", { text: v === null || v === undefined ? "" : String(v) }));
    });
    container.appendChild(grid);
  } else {
    container.appendChild(el("p", { class: "muted", text: data.message || "Done." }));
  }
}

async function requestJSON(url, opts) {
  const resp = await fetch(url, opts);
  if (resp.status === 401) {
    window.location.href = "/login";
    throw new Error("Not authenticated");
  }
  return resp.json();
}

async function postJSON(url, body) {
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body }
    : { method: "POST" };
  return requestJSON(url, opts);
}

async function getJSON(url) {
  return requestJSON(url);
}

// ---- shared utilities -------------------------------------------------------

function fmtAgo(ts) {
  if (!ts) return "never";
  const d = Math.max(0, Math.round((Date.now() / 1000) - ts));
  if (d < 60) return d + "s ago";
  const m = Math.floor(d / 60);
  if (m < 60) return m + "m ago";
  const h = Math.floor(m / 60);
  if (h < 24) return h + "h ago";
  return Math.floor(h / 24) + "d ago";
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  const p = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
         " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

function fmtHM(sec) {
  const s = Math.max(0, Math.round(sec || 0));
  const p = (n) => String(n).padStart(2, "0");
  return p(Math.floor(s / 3600)) + ":" + p(Math.floor((s % 3600) / 60));
}

// Shared "Updated … / Refreshing… / Update failed" label (Users + Libraries).
function renderSyncLabel(elementId, opts) {
  const elU = document.getElementById(elementId);
  if (!elU) return;
  if (opts.fetching) {
    elU.textContent = "Refreshing…";
    elU.className = "muted";
    return;
  }
  if (opts.error) {
    elU.textContent = "Update failed: " + opts.error;
    elU.className = "error";
    return;
  }
  const prefix = opts.live ? "Live · " : "";
  elU.textContent = prefix + (opts.lastSync ? "Updated " + fmtAgo(opts.lastSync) : "Not updated yet");
  elU.className = "muted";
}

// Shared "Live" auto-refresh toggle: persists its state and polls onTick.
function LiveToggle(checkboxId, storageKey, interval, onTick) {
  const cb = document.getElementById(checkboxId);
  let timer = null;

  function enabled() { return !!(cb && cb.checked); }
  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
  }
  function start() {
    stop();
    if (!enabled()) return;
    timer = setInterval(() => { if (!document.hidden) onTick(); }, interval);
  }
  function apply(on, persist) {
    if (cb) cb.checked = on;
    if (persist) {
      try { localStorage.setItem(storageKey, on ? "1" : "0"); } catch (e) {}
    }
    if (on) start(); else stop();
  }
  function stored() {
    try { return localStorage.getItem(storageKey) === "1"; } catch (e) { return false; }
  }

  if (cb) cb.addEventListener("change", () => apply(cb.checked, true));
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && enabled()) onTick();
  });
  return { apply, stored, enabled, stop };
}

// Highlight the nav link for the current page.
function highlightNav() {
  const path = window.location.pathname;
  document.querySelectorAll(".topbar nav a").forEach((a) => {
    if (a.getAttribute("href") === path) a.classList.add("active");
  });
}

// ---- multi-select dropdown (shared by Movie Match + Search filters) ---------

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

let statusTimer = null;

// Poll a "/status" endpoint until it reports no longer "running".
// onUpdate(status) runs each tick (including the final one);
// onDone(status) runs once polling stops.
function startStatusPoll(url, onUpdate, onDone, interval) {
  if (statusTimer) return;
  const tick = async () => {
    let s = null;
    try {
      s = await getJSON(url);
      onUpdate(s);
    } catch (err) {
      /* keep polling */
    }
    if (s && s.status !== "running") {
      stopStatusPoll();
      if (onDone) onDone(s);
      return;
    }
    statusTimer = setTimeout(tick, interval || 2000);
  };
  statusTimer = setTimeout(tick, 1000);
}

function stopStatusPoll() {
  if (statusTimer) {
    clearTimeout(statusTimer);
    statusTimer = null;
  }
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
  bindFormSubmit("integrations-form", "/settings/integrations", "integrations-result");
  bindFormSubmit("timezone-form", "/settings/timezone", "timezone-result");
  bindSettingsBackup();
  bindPathMaps();

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

function bindSettingsBackup() {
  const exportBtn = document.getElementById("export-btn");
  const importBtn = document.getElementById("import-btn");
  const fileInput = document.getElementById("import-file");
  const resultEl = document.getElementById("backup-result");
  if (!exportBtn || !importBtn || !fileInput || !resultEl) return;

  exportBtn.addEventListener("click", () => {
    window.location.href = "/settings/export";
  });

  importBtn.addEventListener("click", () => fileInput.click());

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    showSpinner(resultEl);
    try {
      const resp = await fetch("/settings/import", { method: "POST", body });
      if (resp.status === 401) {
        window.location.href = "/login";
        return;
      }
      const data = await resp.json();
      showMessage(resultEl, data);
      if (data.ok) setTimeout(() => window.location.reload(), 1200);
    } catch (err) {
      resultEl.innerHTML = "";
      resultEl.appendChild(el("p", { class: "error", text: String(err) }));
    } finally {
      fileInput.value = "";
    }
  });
}

function bindPathMaps() {
  const rowsEl = document.getElementById("paths-rows");
  const addBtn = document.getElementById("paths-add");
  const prefillBtn = document.getElementById("paths-prefill");
  const saveBtn = document.getElementById("paths-save");
  const resultEl = document.getElementById("paths-result");
  if (!rowsEl || !addBtn || !saveBtn || !resultEl) return;

  let maps = [];
  const dataEl = document.getElementById("paths-data");
  if (dataEl) {
    try { maps = JSON.parse(dataEl.textContent) || []; } catch (e) { maps = []; }
  }

  function rowEl(m) {
    const div = el("div", { class: "paths-row" });
    const fromInput = el("input", { type: "text", placeholder: "/Filmer", value: (m && m.from) || "" });
    const toInput = el("input", { type: "text", placeholder: "\\\\NAS\\Filmer", value: (m && m.to) || "" });
    const testBtn = el("button", { type: "button", class: "btn-edit", text: "Test" });
    const testOut = el("span", { class: "path-test" });
    const rmBtn = el("button", { type: "button", class: "btn-edit", text: "Remove" });

    testBtn.addEventListener("click", async () => {
      const path = fromInput.value.trim();
      const to = toInput.value.trim();
      if (!path && !to) { testOut.textContent = "Fill in a path first."; return; }
      testOut.textContent = "…";
      try {
        const resp = await fetch("/api/paths/test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: path, to: to }),
        });
        if (resp.status === 401) { window.location.href = "/login"; return; }
        const d = await resp.json();
        if (d.error) { testOut.textContent = d.error; return; }
        const bits = [d.exists ? "exists" : "missing"];
        if (d.exists) {
          bits.push(d.is_dir ? "directory" : "file");
          bits.push(d.readable ? "readable" : "not readable");
          bits.push(d.writable ? "writable" : "not writable");
        }
        testOut.textContent = "→ " + (d.resolved || "") + "  (" + bits.join(", ") + ")";
      } catch (err) {
        testOut.textContent = String(err);
      }
    });

    rmBtn.addEventListener("click", () => div.remove());

    div.appendChild(fromInput);
    div.appendChild(el("span", { class: "path-arrow", text: "→" }));
    div.appendChild(toInput);
    div.appendChild(testBtn);
    div.appendChild(testOut);
    div.appendChild(rmBtn);
    return div;
  }

  function render() {
    rowsEl.innerHTML = "";
    (maps.length ? maps : [null]).forEach((m) => rowsEl.appendChild(rowEl(m)));
  }

  addBtn.addEventListener("click", () => {
    maps.push({ from: "", to: "" });
    render();
  });

  if (prefillBtn) {
    prefillBtn.addEventListener("click", async () => {
      prefillBtn.disabled = true;
      showSpinner(resultEl);
      try {
        let data = await getJSON("/api/libraries");
        let libs = (data && data.libraries) || [];
        if (!libs.length) {
          // No cached data yet: kick a refresh and wait for it to finish.
          try { await postJSON("/api/libraries/refresh", null); } catch (e) {}
          await new Promise((resolve) => {
            startStatusPoll("/api/libraries/status", () => {}, () => resolve(), 800);
          });
          data = await getJSON("/api/libraries");
          libs = (data && data.libraries) || [];
        }
        const locations = [];
        libs.forEach((lib) => (lib.locations || []).forEach((loc) => {
          if (loc && !locations.includes(loc)) locations.push(loc);
        }));
        if (!locations.length) {
          showMessage(resultEl, { ok: false, error: "No library folders found." });
          return;
        }
        const existing = new Set(maps.map((m) => m.from));
        let added = 0;
        locations.forEach((loc) => {
          if (!existing.has(loc)) { maps.push({ from: loc, to: "" }); existing.add(loc); added++; }
        });
        render();
        showMessage(resultEl, {
          ok: true,
          message: added
            ? "Added " + added + " library folder(s). Fill in the local path for each and save."
            : "All library folders are already listed.",
        });
      } catch (err) {
        resultEl.innerHTML = "";
        resultEl.appendChild(el("p", { class: "error", text: String(err) }));
      } finally {
        prefillBtn.disabled = false;
      }
    });
  }

  saveBtn.addEventListener("click", async () => {
    const collected = [];
    rowsEl.querySelectorAll(".paths-row").forEach((row) => {
      const inputs = row.querySelectorAll("input");
      const from = inputs[0].value.trim();
      const to = inputs[1].value.trim();
      if (from && to) collected.push({ from: from, to: to });
    });
    showSpinner(resultEl);
    try {
      const resp = await fetch("/settings/paths", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ maps: collected }),
      });
      if (resp.status === 401) { window.location.href = "/login"; return; }
      const d = await resp.json();
      showMessage(resultEl, d);
      if (d.ok) { maps = collected; render(); }
    } catch (err) {
      resultEl.innerHTML = "";
      resultEl.appendChild(el("p", { class: "error", text: String(err) }));
    }
  });

  render();
}

function renderRecentScans(body, scans) {
  scans.forEach((s) => {
    const age = s.age < 60 ? s.age + "s ago" : Math.round(s.age / 60) + " min ago";
    const row = el("div", { class: "task" });
    row.appendChild(el("div", { class: "task-label", text: "Scan " + s.name }));
    row.appendChild(el("div", { class: "task-progress muted", text: "Started " + age }));
    body.appendChild(row);
  });
}

function renderRunningTasks(body, running) {
  running.forEach((task) => {
    const pct = (task.progress === null || task.progress === undefined)
      ? null
      : Math.max(0, Math.min(100, Math.round(task.progress)));
    const row = el("div", { class: "task" });
    row.appendChild(el("div", {
      class: "task-label",
      text: pct === null ? task.name : task.name + " — " + pct + "%",
    }));
    if (pct === null) {
      row.appendChild(el("div", { class: "task-progress muted", text: "Running…" }));
    } else {
      const bar = el("div", { class: "progress" });
      bar.appendChild(el("div", { class: "progress-fill", style: "width:" + pct + "%" }));
      row.appendChild(bar);
    }
    body.appendChild(row);
  });
}

function renderActivityLog(body, act) {
  if (!act.ok) {
    body.appendChild(el("p", { class: "error", text: act.error }));
    return;
  }
  if (!act.entries || act.entries.length === 0) {
    body.appendChild(el("p", { class: "muted", text: "No recent activity." }));
    return;
  }
  const table = el("table");
  const thead = el("thead");
  const hr = el("tr");
  hr.appendChild(el("th", { text: "When" }));
  hr.appendChild(el("th", { text: "Activity" }));
  thead.appendChild(hr);
  table.appendChild(thead);
  const tbody = el("tbody");
  act.entries.forEach((e) => {
    const tr = el("tr");
    tr.appendChild(el("td", { class: "muted nowrap", text: e.date || "" }));
    const detail = e.short_overview ? e.name + " — " + e.short_overview : e.name;
    tr.appendChild(el("td", { text: detail }));
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  body.appendChild(table);
}

function renderActivity(body, tasks, act) {
  body.innerHTML = "";
  const running = tasks.ok && tasks.running ? tasks.running : [];
  const scans = tasks.ok && tasks.recent_scans ? tasks.recent_scans : [];

  if (running.length === 0 && scans.length === 0) {
    body.appendChild(el("p", { class: "muted", text: "No active tasks." }));
  } else {
    renderRecentScans(body, scans);
    renderRunningTasks(body, running);
  }

  body.appendChild(el("h3", { class: "activity-heading", text: "Recent activity" }));
  renderActivityLog(body, act);
}

async function refreshActivity() {
  const body = document.getElementById("activity-body");
  try {
    const [tasks, act] = await Promise.all([
      getJSON("/api/jellyfin/tasks"),
      getJSON("/api/jellyfin/activity"),
    ]);
    renderActivity(body, tasks, act);
  } catch (err) {
    body.innerHTML = "";
    body.appendChild(el("p", { class: "error", text: String(err) }));
  }
}

const activityLive = LiveToggle("activity-live", "jftools.activity.live", 3000, refreshActivity);

// Show a red dot on the About nav link when a newer release is available.
async function checkUpdateDot() {
  const dot = document.getElementById("update-dot");
  if (!dot) return;
  try {
    const data = await getJSON("/api/about");
    if (data && data.update_available) dot.hidden = false;
  } catch (e) {
    /* ignore — leave the dot hidden */
  }
}

// ---- collapsible cards (shared by every page) -------------------------------

const CARDS_KEY = "jftools.cards";

function loadCardState() {
  try {
    const s = JSON.parse(localStorage.getItem(CARDS_KEY));
    return s && typeof s === "object" ? s : {};
  } catch (e) {
    return {};
  }
}

function saveCardState(state) {
  try { localStorage.setItem(CARDS_KEY, JSON.stringify(state)); } catch (e) {}
}

// Clicking a card's title collapses/expands it. The collapsed state is saved
// per page + card (card id when present, otherwise its document order).
function initCollapsibleCards() {
  const page = window.location.pathname;
  const state = loadCardState();

  document.querySelectorAll("section.card").forEach((card, i) => {
    const header = card.querySelector(":scope > h2");
    if (!header) return;

    const storeKey = page + "|" + (card.id || "idx" + i);
    const collapsed = !!state[storeKey];

    const apply = (on) => {
      card.classList.toggle("collapsed", on);
      header.setAttribute("aria-expanded", on ? "false" : "true");
    };
    apply(collapsed);

    header.setAttribute("role", "button");
    header.setAttribute("tabindex", "0");

    const toggle = () => {
      const now = !card.classList.contains("collapsed");
      apply(now);
      if (now) state[storeKey] = 1; else delete state[storeKey];
      saveCardState(state);
    };

    header.addEventListener("click", toggle);
    header.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggle();
      }
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  highlightNav();
  bindSettings();
  initCollapsibleCards();
  window.addEventListener("resize", hidePopup);
  window.addEventListener("scroll", hidePopup, true);
  checkUpdateDot();
  if (document.getElementById("activity-body")) {
    refreshActivity();  // always load once when the page opens
    activityLive.apply(activityLive.stored(), false);
  }
});

// ---- hover popup (shared by Users / Libraries / Movie Match) ----------------

const ITEM_CACHE = new Map();
const POPUP_ANCHORS = new Map();  // popup key -> element (current render)
const POPUP_DESCS = new Map();    // popup key -> descriptor (current render)
let popupNode = null;
let popupTimer = null;
let popupHide = null;
let popupKey = null;

function popupKeyOf(desc) {
  return desc.key || desc.itemId;
}

function fmtRuntime(sec) {
  const s = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? h + "h " + m + "m" : m + "m";
}

function chip(text, cls) {
  return el("span", { class: "chip" + (cls ? " " + cls : ""), text });
}

function popImg(ref, kind, w, cls) {
  if (!ref || !ref.item_id) return null;
  let url = "/api/media/image/" + encodeURIComponent(ref.item_id) + "?kind=" + kind + "&w=" + w;
  if (ref.tag) url += "&tag=" + encodeURIComponent(ref.tag);
  return el("img", { class: cls, src: url, alt: "" });
}

function loadItem(itemId, userId) {
  if (!ITEM_CACHE.has(itemId)) {
    let url = "/api/media/item/" + encodeURIComponent(itemId);
    if (userId) url += "?user_id=" + encodeURIComponent(userId);
    ITEM_CACHE.set(itemId,
      getJSON(url)
        .then((d) => (d && d.ok ? d : null))
        .catch(() => null));
  }
  return ITEM_CACHE.get(itemId);
}

function hidePopup() {
  clearTimeout(popupTimer);
  clearTimeout(popupHide);
  if (popupNode) popupNode.hidden = true;
  popupKey = null;
}

function scheduleHide() {
  clearTimeout(popupHide);
  popupHide = setTimeout(hidePopup, 300);
}

function ensurePopup() {
  if (popupNode) return popupNode;
  popupNode = el("div", { class: "pop" });
  popupNode.hidden = true;
  popupNode.addEventListener("mouseenter", () => clearTimeout(popupHide));
  popupNode.addEventListener("mouseleave", scheduleHide);
  document.body.appendChild(popupNode);
  return popupNode;
}

function positionPopup(anchor) {
  if (!popupNode) return;
  const r = anchor.getBoundingClientRect();
  const w = popupNode.offsetWidth;
  const h = popupNode.offsetHeight;
  let left = Math.round(r.left + r.width / 2 - w / 2);
  left = Math.max(10, Math.min(left, window.innerWidth - w - 10));
  let top = Math.round(r.bottom + 8);
  if (top + h > window.innerHeight - 10) top = Math.max(10, r.top - h - 8);
  popupNode.style.left = left + "px";
  popupNode.style.top = top + "px";
}

function renderPopup(resp, desc) {
  if (!popupNode) return;
  popupNode.innerHTML = "";
  const inner = el("div", { class: "pop-inner" });

  if (!resp || !resp.item) {
    inner.appendChild(el("div", { class: "muted", text: "No details available." }));
    popupNode.appendChild(inner);
    return;
  }

  const item = resp.item;
  const imgs = item.images || {};

  // Wide banner: episode still for episodes, otherwise the backdrop.
  const wideRef = (item.type === "Episode" && imgs.thumb) || imgs.backdrop || imgs.primary;
  const wideKind = (item.type === "Episode" && imgs.thumb)
    ? "Thumb" : (imgs.backdrop ? "Backdrop" : "Primary");
  const wide = popImg(wideRef, wideKind, 800, "pop-wide");
  if (wide) inner.appendChild(wide);

  inner.appendChild(el("div", { class: "pop-title", text: item.name }));

  const sub = [];
  if (item.series) sub.push(item.series);
  if (item.season && item.episode != null) {
    sub.push("S" + String(item.season).padStart(2, "0") + "E" + String(item.episode).padStart(2, "0"));
  }
  const yr = item.year || (item.premiere ? fmtDate(item.premiere).slice(0, 10) : "");
  if (yr) sub.push(yr);
  const subline = sub.join(" · ");
  if (subline) inner.appendChild(el("div", { class: "muted pop-sub", text: subline }));

  const chips = el("div", { class: "pop-chips" });
  if (item.community_rating) chips.appendChild(chip("★ " + Number(item.community_rating).toFixed(1), "chip-rating"));
  if (item.critic_rating) chips.appendChild(chip("Critics " + item.critic_rating + "%", "chip-critics"));
  if (item.official_rating) chips.appendChild(chip(item.official_rating));
  if (item.runtime_s) chips.appendChild(chip(fmtRuntime(item.runtime_s)));
  if (item.studios && item.studios.length) chips.appendChild(chip(item.studios[0]));
  if (chips.children.length) inner.appendChild(chips);

  if (item.genres && item.genres.length) {
    inner.appendChild(el("div", { class: "muted pop-genres", text: item.genres.join(" · ") }));
  }

  if (item.overview) {
    inner.appendChild(el("div", { class: "pop-overview", text: item.overview }));
  }

  const credits = [];
  if (item.directors && item.directors.length) credits.push("Dir. " + item.directors.join(", "));
  if (item.cast && item.cast.length) credits.push(item.cast.join(", "));
  if (credits.length) inner.appendChild(el("div", { class: "muted pop-credits", text: credits.join(" — ") }));

  // Live playback position (only when the item is being watched right now).
  const a = desc.live;
  if (a) {
    const live = el("div", { class: "pop-live" });
    live.appendChild(el("div", { class: "progress" }, [
      el("div", { class: "progress-fill", style: "width:" + a.pct + "%" }),
    ]));
    live.appendChild(el("div", {
      class: "muted",
      text: (a.paused ? "Paused" : "Playing") + " · " + fmtHM(a.pos_s) + " of " + fmtHM(a.total_s),
    }));
    inner.appendChild(live);
  }

  const links = el("div", { class: "pop-links" });
  if (resp.jellyfin_url && item.name) {
    const href = resp.jellyfin_url.replace(/\/+$/, "") + "/web/#/details?id=" + encodeURIComponent(desc.itemId);
    links.appendChild(el("a", { class: "pop-link", href: href, target: "_blank", rel: "noopener", text: "Open in Jellyfin" }));
  }
  if ((item.type === "Movie" || item.type === "Video") && resp.radarr_url) {
    links.appendChild(el("a", {
      class: "pop-link",
      href: resp.radarr_url + encodeURIComponent(item.name),
      target: "_blank",
      rel: "noopener",
      text: "Radarr",
    }));
  } else if ((item.type === "Series" || item.type === "Episode") && resp.sonarr_url) {
    links.appendChild(el("a", {
      class: "pop-link",
      href: resp.sonarr_url + encodeURIComponent(item.series || item.name),
      target: "_blank",
      rel: "noopener",
      text: "Sonarr",
    }));
  }
  if (links.children.length) inner.appendChild(links);

  if (item.path) {
    const rescan = el("a", { class: "pop-rescan", href: "#", text: "Rescan library file" });
    rescan.addEventListener("click", (ev) => {
      ev.preventDefault();
      rescanFile(rescan, item.path);
    });
    inner.appendChild(el("div", { class: "pop-links" }, [rescan]));
  }

  popupNode.appendChild(inner);
}

async function rescanFile(node, path) {
  if (node.dataset.busy) return;
  node.dataset.busy = "1";
  const prev = node.textContent;
  node.textContent = "Rescanning…";
  try {
    const data = await postJSON("/api/media/rescan", "path=" + encodeURIComponent(path));
    node.textContent = data.ok ? "✓ Rescan started" : "Rescan failed";
  } catch (err) {
    node.textContent = "Rescan failed";
  }
  setTimeout(() => { node.textContent = prev; delete node.dataset.busy; }, 2500);
}

function showPopup(desc, anchor) {
  const node = ensurePopup();
  const key = popupKeyOf(desc);
  popupKey = key;
  node.hidden = false;
  node.innerHTML = "";
  node.appendChild(el("div", { class: "pop-inner muted", text: "Loading…" }));
  positionPopup(anchor);
  loadItem(desc.itemId, desc.userId).then((resp) => {
    if (popupKey !== key) return;
    renderPopup(resp, desc);
    positionPopup(anchor);
  });
}

function registerPopup(anchor, desc) {
  const key = popupKeyOf(desc);
  POPUP_ANCHORS.set(key, anchor);
  POPUP_DESCS.set(key, desc);
  anchor.addEventListener("mouseenter", () => {
    clearTimeout(popupHide);
    clearTimeout(popupTimer);
    popupTimer = setTimeout(() => showPopup(desc, anchor), 220);
  });
  anchor.addEventListener("mouseleave", () => {
    clearTimeout(popupTimer);
    scheduleHide();
  });
}

// Re-anchor an open popup after a table re-render (call from render code, after
// the new rows are built, so the popup survives live refreshes).
function reattachOpenPopup() {
  if (!popupKey) return;
  const anchor = POPUP_ANCHORS.get(popupKey);
  const desc = POPUP_DESCS.get(popupKey);
  if (anchor && desc) showPopup(desc, anchor);
  else hidePopup();
}
