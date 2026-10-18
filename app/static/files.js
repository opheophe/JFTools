"use strict";

// ---- file browser -----------------------------------------------------------
// Read-only browsing of the media folders. Navigation happens through plain
// links (/files?path=...), so the browser's back/forward buttons work.

const qp = new URLSearchParams(location.search);
const INITIAL = qp.get("path") || "";

const fbBody = document.getElementById("fb-body");
const fbCrumb = document.getElementById("fb-crumb");
const fbUsage = document.getElementById("fb-usage");
const fbError = document.getElementById("fb-error");
const fbUp = document.getElementById("fb-up");

let fbSort = { col: "name", dir: 1 };
let fbCurrent = { entries: [], parent: null, highlight: null, writable: true, usage: null };

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

function fbRender(data) {
  fbBody.innerHTML = "";
  fbCrumbHtml(data);
  fbUp.hidden = !data.parent;
  fbUp.onclick = () => { window.location.href = "/files?path=" + encodeURIComponent(data.parent); };

  if (!data.entries.length) {
    fbBody.appendChild(el("tr", {}, [el("td", { colspan: "4", class: "muted", text: "Empty folder." })]));
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
    fbBody.appendChild(tr);
  });

  if (data.truncated) {
    fbBody.appendChild(el("tr", {}, [el("td", { colspan: "4", class: "muted", text: "Too many entries — only the first 2000 are shown." })]));
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
  fbBody.appendChild(el("tr", {}, [el("td", { colspan: "4", class: "muted", text: "Pick a media folder:" })]));
  roots.forEach((r) => {
    fbBody.appendChild(el("tr", {}, [el("td", { colspan: "4" }, [
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
