"use strict";

// ---- file browser -----------------------------------------------------------
// Read-only browsing by default; rename / move / delete / new-folder appear
// when file management is enabled on the Settings page. Navigation happens
// through plain links (/files?path=...), so the browser's back/forward work.

const qp = new URLSearchParams(location.search);
const INITIAL = qp.get("path") || "";

const fbBody = document.getElementById("fb-body");
const fbCrumb = document.getElementById("fb-crumb");
const fbUsage = document.getElementById("fb-usage");
const fbError = document.getElementById("fb-error");
const fbUp = document.getElementById("fb-up");
const fbRescan = document.getElementById("fb-rescan");

let fbSort = { col: "name", dir: 1 };
let fbCurrent = { entries: [], parent: null, highlight: null, writable: true, usage: null, mgmt: false, path: "" };

function fbErr(msg) {
  fbError.textContent = msg;
  fbError.hidden = !msg;
}

function fmtSize(n) {
  if (n === null || n === undefined) return "—";
  if (n < 1024) return n + " B";
  const units = ["KB", "MB", "GB", "TB"];
  let v = n;
  let i = -1;
  do { v /= 1024; i++; } while (v >= 1024 && i < units.length - 1);
  return v.toFixed(1) + " " + units[i];
}

// POST with the CSRF header the mutation endpoints require.
async function fbPost(url, body) {
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

// ---- modal ------------------------------------------------------------------

function fbModal(opts) {
  return new Promise((resolve) => {
    const back = el("div", { class: "fb-modal-back" });
    const box = el("div", { class: "fb-modal" });
    box.appendChild(el("h3", { text: opts.title }));
    if (opts.text) box.appendChild(el("p", { class: "muted", text: opts.text }));
    let input = null;
    if (opts.input) {
      input = el("input", { type: "text", class: "fb-modal-input", value: opts.value || "" });
      box.appendChild(input);
    }
    const row = el("div", { class: "row" });
    const ok = el("button", { class: (opts.danger ? "danger" : "primary"), text: opts.okText || "OK" });
    const cancel = el("button", { text: "Cancel" });
    row.appendChild(cancel);
    row.appendChild(ok);
    box.appendChild(row);
    back.appendChild(box);
    document.body.appendChild(back);

    const done = (okClicked) => {
      const value = input ? input.value.trim() : "";
      document.body.removeChild(back);
      document.removeEventListener("keydown", onKey);
      resolve(okClicked ? { ok: true, value } : { ok: false, value });
    };
    const onKey = (e) => {
      if (e.key === "Escape") done(false);
      if (e.key === "Enter" && input) done(true);
    };
    cancel.addEventListener("click", () => done(false));
    ok.addEventListener("click", () => done(true));
    back.addEventListener("mousedown", (e) => { if (e.target === back) done(false); });
    document.addEventListener("keydown", onKey);
    if (input) input.focus();
  });
}

// ---- rescan banner ----------------------------------------------------------

function fbShowRescan(dir) {
  fbRescan.innerHTML = "";
  fbRescan.hidden = false;
  const text = "Media changed in " + dir;
  const btn = el("button", { class: "primary", text: "Rescan library folder" });
  const dismiss = el("button", { class: "mini", text: "Dismiss" });
  btn.addEventListener("click", () => rescanFile(btn, dir));
  dismiss.addEventListener("click", () => { fbRescan.hidden = true; });
  fbRescan.appendChild(el("span", { class: "muted", text: text + " — " }));
  fbRescan.appendChild(btn);
  fbRescan.appendChild(dismiss);
}

// ---- rendering --------------------------------------------------------------

function fbSortEntries(entries) {
  const dir = fbSort.dir;
  const val = (e) => {
    switch (fbSort.col) {
      case "size": return e.size === null ? -1 : e.size;
      case "mtime": return e.mtime || "";
      case "ext": return (e.ext || "").toLowerCase();
      default: return (e.name || "").toLowerCase();
    }
  };
  const cmp = (a, b) => {
    const va = val(a);
    const vb = val(b);
    if (va < vb) return -dir;
    if (va > vb) return dir;
    return 0;
  };
  const dirs = entries.filter((e) => e.dir).sort(cmp);
  const files = entries.filter((e) => !e.dir).sort(cmp);
  return dirs.concat(files);
}

function updateSortIndicators() {
  document.querySelectorAll("#fb-table th[data-sort]").forEach((th) => {
    const arrow = th.querySelector(".sort-arrow");
    if (arrow) {
      arrow.textContent = th.dataset.sort === fbSort.col ? (fbSort.dir === 1 ? "▲" : "▼") : "";
    }
  });
}

function fbCrumbHtml(data) {
  fbCrumb.innerHTML = "";
  (data.crumb || []).forEach(([p, label], i) => {
    if (i) fbCrumb.appendChild(el("span", { class: "muted", text: "/" }));
    fbCrumb.appendChild(el("a", { href: "/files?path=" + encodeURIComponent(p), text: label }));
  });
}

function fbActionBtn(text, title, cls, onClick) {
  const b = el("button", { class: "fb-mini-btn " + (cls || ""), text: text, title: title });
  b.addEventListener("click", () => onClick(b));
  return b;
}

function fbRender(data) {
  fbBody.innerHTML = "";
  fbCrumbHtml(data);
  fbUp.hidden = !data.parent;
  fbUp.onclick = () => { window.location.href = "/files?path=" + encodeURIComponent(data.parent); };

  if (!data.entries.length) {
    fbBody.appendChild(el("tr", {}, [el("td", { colspan: "5", class: "muted", text: "Empty folder." })]));
  }
  fbSortEntries(data.entries).forEach((e) => {
    const tr = el("tr");
    if (data.highlight && e.name === data.highlight) tr.classList.add("fb-hl");
    const nameTd = el("td");
    if (e.dir) {
      nameTd.appendChild(el("a", { class: "fb-entry-link", href: "/files?path=" + encodeURIComponent(e.path), text: e.name + "/" }));
    } else {
      nameTd.appendChild(el("span", { text: e.name, title: e.path }));
    }
    tr.appendChild(nameTd);
    tr.appendChild(el("td", { class: "muted", text: e.dir ? "—" : fmtSize(e.size) }));
    tr.appendChild(el("td", { class: "muted", text: e.mtime || "" }));
    tr.appendChild(el("td", { class: "muted", text: e.ext }));

    const actTd = el("td", { class: "fb-actions" });
    if (data.mgmt) {
      actTd.appendChild(fbActionBtn("✎", "Rename", "fb-edit", () => fbRename(e)));
      actTd.appendChild(fbActionBtn("🗑", "Delete", "fb-del", () => fbDelete(e)));
    } else {
      actTd.appendChild(el("span", { class: "muted", text: "—" }));
    }
    tr.appendChild(actTd);
    fbBody.appendChild(tr);
  });

  if (data.truncated) {
    fbBody.appendChild(el("tr", {}, [el("td", { colspan: "5", class: "muted", text: "Too many entries — only the first 2000 are shown." })]));
  }

  if (data.usage) {
    fbUsage.textContent = fmtSize(data.usage.free) + " free of " + fmtSize(data.usage.total) + (data.writable ? "" : " · read-only");
  } else if (!data.writable) {
    fbUsage.textContent = "read-only";
  } else {
    fbUsage.textContent = "";
  }
  updateSortIndicators();

  const hl = fbBody.querySelector(".fb-hl");
  if (hl && hl.scrollIntoView) hl.scrollIntoView({ block: "center" });
}

// ---- mutations --------------------------------------------------------------

async function fbMutate(url, body, rescanDir) {
  let data = null;
  try {
    data = await fbPost(url, body);
  } catch (err) {
    fbErr("Could not reach the server.");
    return false;
  }
  if (!data.ok) {
    fbErr(data.error || "That operation failed.");
    return false;
  }
  fbErr("");
  await fbLoad(fbCurrent.path);
  if (rescanDir) fbShowRescan(rescanDir);
  return true;
}

async function fbRename(e) {
  const r = await fbModal({
    title: "Rename",
    text: "New name for \"" + e.name + "\" (same folder):",
    input: true,
    value: e.name,
    okText: "Rename",
  });
  if (!r.ok || !r.value || r.value === e.name) return;
  await fbMutate("/api/files/rename", "path=" + encodeURIComponent(e.path) + "&name=" + encodeURIComponent(r.value), fbCurrent.path);
}

async function fbDelete(e) {
  const what = e.dir ? "folder \"" + e.name + "\" and all its contents" : "file \"" + e.name + "\"";
  const r = await fbModal({
    title: "Delete permanently?",
    text: "The " + what + " will be deleted. This cannot be undone.",
    danger: true,
    okText: "Delete permanently",
  });
  if (!r.ok) return;
  await fbMutate("/api/files/delete", "path=" + encodeURIComponent(e.path), fbCurrent.path);
}

// ---- loading ----------------------------------------------------------------

async function fbLoad(path) {
  fbErr("");
  let data = null;
  try {
    data = await getJSON("/api/files/list?path=" + encodeURIComponent(path || ""));
  } catch (err) {
    fbErr("Could not reach the server.");
    fbBody.innerHTML = "";
    fbCrumb.innerHTML = "";
    fbUp.hidden = true;
    fbUsage.textContent = "";
    return;
  }
  if (!data) return;
  if (!data.ok) {
    fbErr(data.error || "Could not load that folder.");
    fbBody.innerHTML = "";
    fbCrumb.innerHTML = "";
    fbUp.hidden = true;
    fbUsage.textContent = "";
    return;
  }
  if (data.pick) {
    fbErr(data.error || "");
    renderRoots(data.roots || []);
    return;
  }
  fbCurrent = data;
  fbRender(data);
}

function renderRoots(roots) {
  fbBody.innerHTML = "";
  fbCrumb.innerHTML = "";
  fbUp.hidden = true;
  fbUsage.textContent = "";
  if (!roots.length) return;
  fbBody.appendChild(el("tr", {}, [el("td", { colspan: "5", class: "muted", text: "Pick a media folder:" })]));
  roots.forEach((r) => {
    fbBody.appendChild(el("tr", {}, [el("td", { colspan: "5" }, [
      el("a", { class: "fb-entry-link", href: "/files?path=" + encodeURIComponent(r.path), text: r.path }),
    ])]));
  });
}

(async () => {
  if (INITIAL) {
    await fbLoad(INITIAL);
    return;
  }
  let data = null;
  try {
    data = await getJSON("/api/files/roots");
  } catch (err) {
    fbErr("Could not reach the server.");
    return;
  }
  if (!data || !data.ok) {
    fbErr((data && data.error) || "Could not load the media folders.");
    return;
  }
  fbErr(data.roots.length ? "" : "No media folders found yet. Add mappings under Settings → Media paths, or refresh Libraries once.");
  renderRoots(data.roots);
})();

document.querySelectorAll("#fb-table th[data-sort]").forEach((th) => {
  th.addEventListener("click", () => {
    const col = th.dataset.sort;
    if (fbSort.col === col) fbSort.dir = -fbSort.dir;
    else fbSort = { col: col, dir: 1 };
    fbRender(fbCurrent);
  });
});
