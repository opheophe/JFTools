"use strict";

// Silences "Uncaught (in promise) TypeError: NetworkError …" console noise the
// browser emits when it aborts an in-flight fetch on refresh/navigation. Real
// errors are already caught and surfaced by their callers, so only abort /
// network-error rejections (which have no meaningful handler in that case) are
// swallowed here.
window.addEventListener("unhandledrejection", (e) => {
  const r = e.reason;
  const msg = String((r && r.message) || r || "");
  if (r && (r.name === "AbortError" || /networkerror/i.test(msg))) {
    e.preventDefault();
  }
});

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

// "Series S01E02" line shown under an item's name in lists. Prefers Jellyfin's
// season/episode numbers when the row carries them, otherwise parses the file
// name the way the media scan and OpenSubtitles search do (S01E02 / 1x02).
function episodeSubLine(r) {
  if (!r || r.type !== "Episode") return "";
  let season = r.season;
  let number = r.episode;
  if (season == null || number == null) {
    const text = r.file || r.path || "";
    const m = /[sS](\d{1,2})[eE](\d{1,2})/.exec(text)
      || /(?:^|[^0-9])(\d{1,2})[xX](\d{2})(?:[^0-9]|$)/.exec(text);
    if (!m) return "";
    season = m[1];
    number = m[2];
  }
  const code = "S" + String(season).padStart(2, "0") + "E" + String(number).padStart(2, "0");
  return r.series ? r.series + " " + code : code;
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

  // A section marker is {section: "Label"}; it divides the menu but is not a
  // selectable value. The same value may appear in more than one section (for
  // example "No subtitles" under Language and Quality); those checkboxes share
  // one selection.
  const isSection = (o) => o && o.section !== undefined && o.value === undefined;
  const values = () => [...new Set(opts.filter((o) => !isSection(o)).map((o) => o.value))];

  const labelFor = (v) => {
    const o = opts.find((x) => !isSection(x) && x.value === v);
    return o ? o.label : v;
  };

  function renderButton() {
    const total = values().length;
    if (selected.size === 0 || selected.size === total) {
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
      if (isSection(o)) {
        menu.appendChild(el("div", { class: "ms-section", text: o.section }));
        return;
      }
      const row = el("label", { class: "ms-item" });
      const cb = el("input", { type: "checkbox" });
      cb.checked = selected.has(o.value);
      cb.dataset.value = String(o.value);
      cb.addEventListener("change", () => {
        if (cb.checked) selected.add(o.value); else selected.delete(o.value);
        // Duplicate values live in separate sections; keep every checkbox for
        // this value in step with the shared selection.
        menu.querySelectorAll('input[type="checkbox"]').forEach((box) => {
          if (box.dataset.value === String(o.value)) box.checked = selected.has(o.value);
        });
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
    const valid = new Set(values());
    [...selected].forEach((v) => { if (!valid.has(v)) selected.delete(v); });
    renderButton();
    renderMenu();
  }

  function setValues(vals) {
    selected.clear();
    const valid = new Set(values());
    (vals || []).forEach((v) => { if (valid.has(v)) selected.add(v); });
    renderButton();
    renderMenu();
  }

  renderButton();
  renderMenu();
  return { setOptions, setValues, getValue: () => [...selected] };
}

// Filter profiles (Search + Movie Match): a "Save profile / Delete profile"
// row with one button per saved profile. Profiles are part of the server config
// (data/config.json) so they survive browser clears and travel with settings
// backups. getFilters() snapshots the current filter state; applyFilters()
// restores a profile into the page.
function FilterProfiles(host, page, getFilters, applyFilters) {
  let profiles = {};
  let epoch = 0;

  const row = el("div", { class: "prof-row" });
  const chips = el("div", { class: "prof-chips" });
  const status = el("span", { class: "muted prof-status", text: "" });

  const delWrap = el("div", { class: "prof-del-wrap" });
  const menu = el("div", { class: "prof-menu" });
  menu.hidden = true;

  function setStatus(text, isError) {
    status.textContent = text;
    status.classList.toggle("error", !!isError);
  }

  // Save this page's whole map; the chips change only after the server accepts
  // it, so a failed write never leaves a profile that looks saved.
  function persist(next) {
    const mine = ++epoch;
    const body = "page=" + encodeURIComponent(page) +
      "&profiles=" + encodeURIComponent(JSON.stringify(next));
    postJSON("/api/profiles", body)
      .then((data) => {
        if (mine !== epoch) return;  // a newer save superseded this one
        if (!data.ok) throw new Error(data.error || "Save failed");
        profiles = (data.profiles && data.profiles[page]) || {};
        setStatus("");
        renderChips();
      })
      .catch((err) => {
        if (mine === epoch) setStatus("Not saved: " + err.message, true);
      });
  }

  const names = () => Object.keys(profiles)
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));

  function renderChips() {
    chips.innerHTML = "";
    names().forEach((name) => {
      const b = el("button", { type: "button", class: "prof-btn prof-chip", text: name });
      b.title = "Load filters: " + name;
      b.addEventListener("click", () => applyFilters(profiles[name]));
      chips.appendChild(b);
    });
  }

  const saveBtn = el("button", { type: "button", class: "prof-btn prof-save", text: "Save profile" });
  saveBtn.title = "Save the current filter selections";
  saveBtn.addEventListener("click", () => {
    const name = (window.prompt("Profile name:") || "").trim();
    if (!name) return;
    setStatus("Saving…");
    const next = Object.assign({}, profiles, {
      [name]: JSON.parse(JSON.stringify(getFilters())),
    });
    persist(next);
  });

  const delBtn = el("button", { type: "button", class: "prof-btn prof-del", text: "Delete profile" });
  delBtn.title = "Delete a saved profile";
  delBtn.addEventListener("click", () => {
    menu.innerHTML = "";
    const saved = names();
    if (!saved.length) {
      menu.appendChild(el("span", { class: "muted prof-menu-empty", text: "No profiles saved." }));
    } else {
      saved.forEach((name) => {
        const item = el("button", { type: "button", class: "prof-menu-item", text: "✕ " + name });
        item.title = "Delete " + name;
        item.addEventListener("click", () => {
          const next = Object.assign({}, profiles);
          delete next[name];
          setStatus("Saving…");
          persist(next);
          menu.hidden = true;
        });
        menu.appendChild(item);
      });
    }
    menu.hidden = !menu.hidden;
  });
  document.addEventListener("click", (e) => {
    if (!row.contains(e.target)) menu.hidden = true;
  });

  delWrap.appendChild(delBtn);
  delWrap.appendChild(menu);
  row.appendChild(saveBtn);
  row.appendChild(delWrap);
  row.appendChild(chips);
  row.appendChild(status);
  host.appendChild(row);
  renderChips();

  // Ignore a slow initial load if a save already superseded it.
  getJSON("/api/profiles")
    .then((data) => {
      if (epoch) return;
      profiles = (data.profiles && data.profiles[page]) || {};
      renderChips();
    })
    .catch((err) => setStatus("Could not load profiles: " + err.message, true));
}

// A hover dropdown button (like the Google menu): shows a fixed-position menu
// of items (links or buttons) while the pointer is over the button or menu.
// ``onClick`` makes the label itself do something (the Sync pill resyncs both
// halves while its menu still offers each one on its own).
function hoverMenuButton(label, items, variant, onClick) {
  const wrap = el("span", { class: "id-google" });
  const btn = el("button", {
    type: "button",
    class: "id-google-btn" + (variant ? " id-btn-" + variant : ""),
    text: label,
  });
  wrap.appendChild(btn);
  if (!items.length) {
    if (onClick) btn.addEventListener("click", () => onClick(btn));
    else btn.disabled = true;
    return wrap;
  }
  btn.appendChild(el("span", { class: "id-google-caret", text: "▾" }));
  const menu = el("div", { class: "id-google-menu" });
  menu.hidden = true;
  items.forEach((it) => menu.appendChild(it));
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
  if (onClick) btn.addEventListener("click", () => onClick(btn));
  return wrap;
}

// A pill that acts rather than listing: same frame as the menus, no caret.
// With no handler (a row with no file on disk) it renders greyed out.
function pillButton(label, variant, onClick) {
  return hoverMenuButton(label, [], variant, onClick);
}

// A link item and a clickable button item for a hover menu.
function hoverMenuLink(href, text) {
  return el("a", { class: "id-google-item", href: href, target: "_blank", rel: "noopener", text: text });
}

function hoverMenuAction(text, onClick) {
  const item = el("button", { type: "button", class: "id-google-item id-google-item-btn", text: text });
  item.addEventListener("click", () => onClick(item));
  return item;
}

// ---- row actions -----------------------------------------------------------
//
// The action pills a row carries: Open ▾ · Sync ▾ · Play · Subtitles ·
// Internal and, where asked for, Google ▾. One builder so the Search, Movie
// Match and Libraries tables cannot drift apart; ``cfg.urls`` supplies the base
// URLs (Jellyfin/Radarr/Sonarr, empty when unconfigured) and ``cfg.google``
// adds the trailing Google menu.

function rowActions(r, cfg) {
  cfg = cfg || {};
  const urls = cfg.urls || {};
  const enc = encodeURIComponent;
  const td = el("td", { class: "id-actions" });

  const openItems = [];
  if (r.id && urls.jellyfin) {
    openItems.push(hoverMenuLink(urls.jellyfin.replace(/\/+$/, "") + "/web/#/details?id=" + enc(r.id), "Jellyfin"));
  }
  if ((r.type === "Movie" || r.type === "Video") && urls.radarr) {
    openItems.push(hoverMenuLink(urls.radarr + enc(r.name), "Radarr"));
  } else if ((r.type === "Series" || r.type === "Episode") && urls.sonarr) {
    openItems.push(hoverMenuLink(urls.sonarr + enc(r.series || r.name), "Sonarr"));
  }
  if (r.path) {
    openItems.push(hoverMenuLink("/files?path=" + enc(r.path), "File browser"));
  }
  td.appendChild(hoverMenuButton("Open", openItems, "open"));

  const syncItems = [];
  if (r.path) {
    syncItems.push(hoverMenuAction(
      "Jellyfin library", (node) => resyncOne(node, r.path, resyncLibrary, "Resyncing…")
    ));
    syncItems.push(hoverMenuAction(
      "Subtitles & audio", (node) => resyncOne(node, r.path, resyncTracks, "Resyncing…")
    ));
  }
  // Hover lists the two targets; clicking the pill runs both.
  td.appendChild(hoverMenuButton(
    "Sync", syncItems, "sync", r.path ? (node) => resyncBoth(node, r.path) : null
  ));

  // A series row's path is a folder: there is no file to play or probe.
  const playable = r.path && r.type !== "Series";
  td.appendChild(pillButton("Play", "play", playable ? () => playVideo(r.path) : null));
  td.appendChild(pillButton("Subtitles", "subs", playable ? () => subdlOpen(r.path) : null));
  td.appendChild(pillButton("Internal", "audio", playable ? () => audioOpen(r.path) : null));

  if (cfg.google) td.appendChild(rowGoogleMenu(r));
  return td;
}

// Title used for external lookups: the show for series/episodes, else the name.
function rowLookupTitle(r) {
  if (r.type === "Series" || r.type === "Episode") return r.series || r.name || "";
  return r.name || "";
}

// Candidate years: the on-disk year(s) from the filename/folder, then Jellyfin's.
function rowYears(r) {
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
function rowDiskTitle(r) {
  let s = (r.file || "").replace(/\.[A-Za-z0-9]{2,5}$/, "");
  s = s.replace(/[._]+/g, " ");
  s = s.replace(/[\[\](){}]/g, " ");
  s = s.replace(/\b(2160p|1080p|720p|480p|4k|uhd|hdr10?|hdr|dv|sdr|bluray|blu-?ray|brrip|bdrip|bd-?remux|remux|webrip|web-?dl|webdl|web|hdrip|dvdrip|dvd|hdtv|cam|x264|x265|h264|h265|hevc|avc|xvid|divx|10bit|8bit|aac|ac3|eac3|ddp?|dts|truehd|atmos|flac|mp3|5\.1|7\.1|2\.0|yts|yify|rarbg|proper|repack|extended|unrated|remastered|multi|dual\s?audio|amzn|nf|hulu|itunes)\b/gi, " ");
  s = s.replace(/[^\p{L}\p{N}]+/gu, " ");
  return s.replace(/\s+/g, " ").trim();
}

// Google search queries for the hover menu on the "Google" button.
function rowGoogleQueries(r) {
  const title = rowLookupTitle(r);
  if (!title) return [];
  const year = r.year ? String(r.year) : "";
  const years = rowYears(r);

  let yq = "Which year was " + title + " released";
  if (years.length >= 2) yq += "; was it " + years[0] + " or " + years[1];
  else if (years.length === 1) yq += "; was it " + years[0];

  const items = [
    { label: "Ask about year", q: yq },
    { label: "IMDB", q: title + (year ? " " + year : "") + " imdb" },
    { label: "Language", q: "What is the original language of " + title + (year ? " " + year : "") },
  ];
  const disk = rowDiskTitle(r);
  if (disk) {
    items.unshift({ label: "Same movie", q: 'Is "' + disk + '" the same movie as "' + title + '"' });
  }
  return items;
}

function rowGoogleMenu(r) {
  const wrap = el("span", { class: "id-google" });
  const btn = el("button", { type: "button", class: "id-google-btn id-btn-google", text: "Google" });
  btn.appendChild(el("span", { class: "id-google-caret", text: "▾" }));
  wrap.appendChild(btn);

  const items = rowGoogleQueries(r);
  if (!items.length) {
    btn.disabled = true;
    return wrap;
  }

  const menu = el("div", { class: "id-google-menu" });
  menu.hidden = true;
  items.forEach((it) => {
    menu.appendChild(el("a", {
      class: "id-google-item",
      href: "https://www.google.com/search?q=" + encodeURIComponent(it.q),
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
  bindFormSubmit("files-form", "/settings/files", "files-result");
  bindFormSubmit("subtitle-prefs-form", "/settings/subtitles", "subtitle-prefs-result");
  bindFormSubmit("opensubtitles-form", "/settings/opensubtitles", "opensubtitles-result");
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

  const osTestBtn = document.getElementById("os-test-btn");
  const osResultEl = document.getElementById("opensubtitles-result");
  if (!osTestBtn || !osResultEl) return;

  osTestBtn.addEventListener("click", async () => {
    const form = document.getElementById("opensubtitles-form");
    showSpinner(osResultEl);
    osTestBtn.disabled = true;
    try {
      const data = await postJSON("/api/settings/opensubtitles/test", serializeForm(form));
      osResultEl.innerHTML = "";
      renderResult(osResultEl, data);
    } catch (err) {
      osResultEl.innerHTML = "";
      osResultEl.appendChild(el("p", { class: "error", text: String(err) }));
    } finally {
      osTestBtn.disabled = false;
    }
  });
}

function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
  return (n / 1024 / 1024 / 1024).toFixed(1) + " GB";
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

  const downloadBtn = document.getElementById("backup-download");
  if (downloadBtn) {
    downloadBtn.addEventListener("click", () => {
      window.location.href = "/settings/backup/download";
    });
  }

  const restoreBtn = document.getElementById("backup-restore-btn");
  const restoreFile = document.getElementById("backup-restore-file");
  if (restoreBtn && restoreFile) {
    restoreBtn.addEventListener("click", () => restoreFile.click());
    restoreFile.addEventListener("change", async () => {
      const file = restoreFile.files && restoreFile.files[0];
      if (!file) return;
      const ok = window.confirm(
        "Restore " + file.name + "?\n\n" +
        "This replaces settings, schedules, scan results and Movie Match data. " +
        "JFTools must be restarted afterwards."
      );
      if (!ok) {
        restoreFile.value = "";
        return;
      }
      const body = new FormData();
      body.append("file", file);
      showSpinner(resultEl);
      try {
        const resp = await fetch("/api/backup/restore", { method: "POST", body });
        if (resp.status === 401) {
          window.location.href = "/login";
          return;
        }
        const data = await resp.json();
        showMessage(resultEl, data);
      } catch (err) {
        resultEl.innerHTML = "";
        resultEl.appendChild(el("p", { class: "error", text: String(err) }));
      } finally {
        restoreFile.value = "";
      }
    });
  }

  const form = document.getElementById("backup-form");
  const settingsResult = document.getElementById("backup-settings-result");
  const listBody = document.getElementById("backup-list");
  const listWrap = document.getElementById("backup-list-wrap");
  const listCount = document.getElementById("backup-list-count");
  const nowBtn = document.getElementById("backup-now");

  function renderList(data) {
    if (!listBody || !listWrap) return;
    const backups = (data && data.backups) || [];
    listWrap.hidden = false;
    listBody.innerHTML = "";
    if (!backups.length) {
      listBody.appendChild(el("tr", {}, [
        el("td", { colspan: "4", class: "muted", text: "No backups yet." }),
      ]));
    } else {
      backups.forEach((b) => {
        const tr = el("tr");
        tr.appendChild(el("td", { text: b.name }));
        tr.appendChild(el("td", { class: "muted", text: fmtBytes(b.size) }));
        tr.appendChild(el("td", {
          class: "muted",
          text: fmtDate(new Date(b.created_at * 1000).toISOString()),
        }));
        const actions = el("td", { class: "nowrap" });
        const dl = el("a", {
          href: "/api/backup/file?name=" + encodeURIComponent(b.name),
          class: "btn",
          text: "Download",
        });
        dl.setAttribute("download", b.name);
        const del = el("button", { type: "button", class: "btn-danger", text: "Delete" });
        del.addEventListener("click", async () => {
          if (!window.confirm("Delete " + b.name + "?")) return;
          try {
            const d = await postJSON("/api/backup/delete", "name=" + encodeURIComponent(b.name));
            if (settingsResult) showMessage(settingsResult, d);
            if (d.ok) await refreshList();
          } catch (err) {
            if (settingsResult) {
              settingsResult.innerHTML = "";
              settingsResult.appendChild(el("p", { class: "error", text: String(err) }));
            }
          }
        });
        actions.appendChild(dl);
        actions.appendChild(del);
        tr.appendChild(actions);
        listBody.appendChild(tr);
      });
    }
    if (listCount) {
      const keep = data && data.keep ? data.keep : 0;
      listCount.textContent = backups.length + " saved" +
        (keep ? " — keeping the newest " + keep : " — no limit");
    }
  }

  async function refreshList() {
    if (!listBody) return;
    try {
      renderList(await getJSON("/api/backup/list"));
    } catch (err) {
      if (listCount) listCount.textContent = "Could not load backups.";
    }
  }

  if (form && settingsResult) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      showSpinner(settingsResult);
      try {
        const data = await postJSON("/settings/backup", serializeForm(form));
        showMessage(settingsResult, data);
        if (data.ok) await refreshList();
      } catch (err) {
        settingsResult.innerHTML = "";
        settingsResult.appendChild(el("p", { class: "error", text: String(err) }));
      }
    });
  }

  if (nowBtn && settingsResult) {
    nowBtn.addEventListener("click", async () => {
      showSpinner(settingsResult);
      nowBtn.disabled = true;
      try {
        const data = await postJSON("/api/backup/now");
        showMessage(settingsResult, data);
        if (data.ok) renderList({ backups: data.backups || [], keep: data.keep });
      } catch (err) {
        settingsResult.innerHTML = "";
        settingsResult.appendChild(el("p", { class: "error", text: String(err) }));
      } finally {
        nowBtn.disabled = false;
      }
    });
  }

  refreshList();
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
    const testBtn = el("button", { type: "button", text: "Test" });
    const testOut = el("span", { class: "path-test" });
    const rmBtn = el("button", { type: "button", class: "btn-danger", text: "Remove" });

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

  function readRows() {
    const out = [];
    rowsEl.querySelectorAll(".paths-row").forEach((row) => {
      const inputs = row.querySelectorAll("input");
      out.push({ from: inputs[0].value.trim(), to: inputs[1].value.trim() });
    });
    return out;
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
        // Keep whatever is typed already (drop fully empty rows), then fill both
        // sides from the library so the rows are complete and saveable.
        maps = readRows().filter((m) => m.from || m.to);
        const index = new Map(maps.map((m, i) => [m.from, i]));
        let added = 0;
        let filled = 0;
        locations.forEach((loc) => {
          if (index.has(loc)) {
            const row = maps[index.get(loc)];
            if (!row.to) { row.to = loc; filled += 1; }
          } else {
            maps.push({ from: loc, to: loc });
            index.set(loc, maps.length - 1);
            added += 1;
          }
        });
        render();
        const parts = [];
        if (added) parts.push("added " + added);
        if (filled) parts.push("filled in " + filled);
        showMessage(resultEl, {
          ok: true,
          message: parts.length
            ? "Library paths " + parts.join(", ") + ". Edit the right-hand path if it differs, then save."
            : "All library paths are already listed and set.",
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

// Users nav dot: green = watching, orange = paused, red = idle. Hidden entirely
// when the "User watching" job is disabled (the dot would be pointless).
function applyWatchDot(data) {
  const dot = document.getElementById("watch-dot");
  if (!dot) return;
  if (!data || !data.active) {
    dot.hidden = true;
    return;
  }
  dot.hidden = false;
  dot.className = "watch-dot " + (data.status === "watching" ? "watching"
    : data.status === "paused" ? "paused" : "idle");
}

async function pollWatchDot() {
  try {
    applyWatchDot(await getJSON("/api/users/watching"));
  } catch (e) {
    /* ignore — leave the dot as-is */
  }
}

function startWatchDot() {
  if (!document.getElementById("watch-dot")) return;
  pollWatchDot();
  setInterval(() => { if (!document.hidden) pollWatchDot(); }, 15000);
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
  startWatchDot();
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

  popupNode.appendChild(inner);
}

// Player popup: one reusable window per session, so clicking Play again on
// another file (or another subtitle track) navigates that window instead of
// stacking a new one per click. Must be called straight from a click handler,
// or the browser blocks the popup.
const PLAY_WINDOW = "jftools-play";

function playVideo(path, track) {
  if (!path) return;
  let url = "/play?path=" + encodeURIComponent(path);
  if (track) url += "&track=" + encodeURIComponent(track);
  window.open(
    url,
    PLAY_WINDOW,
    "width=1180,height=720,menubar=no,toolbar=no,location=no,status=no,resizable=yes,scrollbars=no"
  );
}

// Each resync reports on its own, so the Sync pill can show which target worked.
async function resyncLibrary(path) {
  try {
    const data = await postJSON("/api/media/rescan", "path=" + encodeURIComponent(path));
    return data.ok
      ? { ok: true, text: "Rescan started" }
      : { ok: false, text: data.error || "Rescan failed" };
  } catch (err) {
    return { ok: false, text: "Rescan failed" };
  }
}

// A media rescan updates the server-side store, so mirror the fresh entry into
// the page's data: the Subtitles and Audio columns then show it straight away
// instead of waiting for a reload (SE comes from search.js and is absent on the
// Movie Match page, which has no scan columns).
function applyScanEntry(path, entry) {
  if (!entry || typeof SE === "undefined" || !SE.scans) return;
  SE.scans[path] = entry;
  renderSeTable();
}

async function resyncTracks(path) {
  try {
    const data = await postJSON("/api/subtitles/rescan", "path=" + encodeURIComponent(path));
    if (!data.ok) return { ok: false, text: data.error || "Rescan failed" };
    const e = data.entry || {};
    applyScanEntry(path, data.entry);
    if (e.folder) return { ok: true, text: "series folder, nothing to probe" };
    if (e.error) return { ok: false, text: e.error };
    const subs = (e.subs || []).length;
    const audio = (e.audio || []).length;
    const subsLabel = subs + (subs === 1 ? " sub" : " subs");
    const audioLabel = audio + (audio === 1 ? " audio track" : " audio tracks");
    return { ok: true, text: subsLabel + ", " + audioLabel };
  } catch (err) {
    return { ok: false, text: "Rescan failed" };
  }
}

// One target at a time, from the Sync menu: the item reports its own result.
async function resyncOne(node, path, run, busyText) {
  if (node.dataset.busy) return;
  node.dataset.busy = "1";
  const prev = node.textContent;
  node.textContent = busyText;
  const result = await run(path);
  node.textContent = (result.ok ? "✓ " : "✗ ") + result.text;
  node.title = result.text;
  setTimeout(() => {
    node.textContent = prev;
    node.title = "";
    delete node.dataset.busy;
  }, 2500);
}

// The Sync pill: both targets, each marked so a failure is visible.
async function resyncBoth(node, path) {
  if (!path || node.dataset.busy) return;
  node.dataset.busy = "1";
  const prev = node.textContent;
  node.textContent = "Syncing…";
  const [lib, tracks] = await Promise.all([resyncLibrary(path), resyncTracks(path)]);
  const mark = (r) => (r.ok ? "✓" : "✗");
  node.textContent = mark(lib) + " Library · " + mark(tracks) + " Tracks";
  node.title = "Jellyfin library: " + lib.text + " · Subtitles & audio: " + tracks.text;
  setTimeout(() => {
    node.textContent = prev;
    node.title = "";
    delete node.dataset.busy;
  }, 4000);
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
