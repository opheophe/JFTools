"use strict";

let USERS = [];
let selectedUserId = null;
let LAST_SYNC = null;
let ageTimer = null;
let LOADING = false;

const IMG_CACHE = new Map();

// Reuse already-loaded poster nodes so live re-renders never refetch an image.
function posterEl(image) {
  if (!image || !image.item_id) return null;
  let url = "/api/media/image/" + encodeURIComponent(image.item_id);
  if (image.tag) url += "?tag=" + encodeURIComponent(image.tag);

  const cached = IMG_CACHE.get(url);
  if (cached) return cached.isConnected ? cached.cloneNode(true) : cached;

  const img = el("img", { class: "now-poster", src: url, alt: "", loading: "lazy" });
  IMG_CACHE.set(url, img);
  img.addEventListener("error", () => { IMG_CACHE.delete(url); img.remove(); });
  return img;
}

function renderUpdatedLabel() {
  renderSyncLabel("users-updated", { lastSync: LAST_SYNC, live: isLiveEnabled() });
}

function setUpdated(lastSync, fetching, error) {
  LAST_SYNC = lastSync;
  renderSyncLabel("users-updated", { lastSync, fetching, error, live: isLiveEnabled() });
}

function pollUsersUntilDone() {
  startStatusPoll(
    "/api/users/status",
    (s) => setUpdated(s.last_sync, s.status === "running", s.error),
    () => loadUsers()
  );
}

async function loadUsers() {
  if (LOADING) return;
  LOADING = true;
  const body = document.getElementById("users-body");
  try {
    const data = await getJSON("/api/users");
    if (!data.ok) {
      body.innerHTML = "";
      body.appendChild(el("tr", {}, [el("td", { colspan: "4", class: "error", text: data.error })]));
      setUpdated(data.last_sync, false, data.error || "Failed to load users.");
      return;
    }
    USERS = data.users || [];
    renderUsers();
    setUpdated(data.last_sync, data.fetching, data.fetch_error);
    if (data.fetching) pollUsersUntilDone();
  } catch (err) {
    body.innerHTML = "";
    body.appendChild(el("tr", {}, [el("td", { colspan: "4", class: "error", text: String(err) })]));
  } finally {
    LOADING = false;
  }
}

function renderUsers() {
  POPUP_ANCHORS.clear();
  POPUP_DESCS.clear();
  const body = document.getElementById("users-body");
  body.innerHTML = "";
  if (USERS.length === 0) {
    body.appendChild(el("tr", {}, [el("td", { colspan: "4", class: "muted", text: "No users." })]));
    return;
  }
  USERS.forEach((u) => {
    const tr = el("tr", { class: "user-row" });
    tr.appendChild(el("td", { text: u.name }));

    tr.appendChild(el("td", { class: "muted", text: fmtDate(u.last_login) }));

    tr.appendChild(renderLastWatched(u));

    if (u.active) {
      const td = el("td");
      const wrap = el("div", { class: "now-watching" });
      const poster = posterEl(u.active.image);
      if (poster) wrap.appendChild(poster);
      const info = el("div", { class: "now-info" });
      info.appendChild(el("div", { class: "muted", text: u.active.name + (u.active.paused ? " (paused)" : "") }));
      const bar = el("div", { class: "progress" });
      bar.appendChild(el("div", { class: "progress-fill", style: "width:" + u.active.pct + "%" }));
      info.appendChild(bar);
      info.appendChild(el("div", {
        class: "now-time",
        text: fmtHM(u.active.pos_s) + " of " + fmtHM(u.active.total_s),
      }));
      wrap.appendChild(info);
      td.appendChild(wrap);
      tr.appendChild(td);
      if (u.active.item_id) {
        registerPopup(wrap, {
          key: u.id + ":" + u.active.item_id,
          itemId: u.active.item_id,
          userId: u.id,
          live: {
            pct: u.active.pct,
            pos_s: u.active.pos_s,
            total_s: u.active.total_s,
            paused: u.active.paused,
          },
        });
      }
    } else {
      tr.appendChild(el("td", { class: "muted", text: "—" }));
    }

    tr.addEventListener("click", () => toggleHistory(u));
    body.appendChild(tr);
  });

  // Keep an open popup alive across live re-renders by re-anchoring it.
  reattachOpenPopup();
}

function renderLastWatched(u) {
  const td = el("td");
  const lw = u.last_watched;
  if (!lw || !lw.name) {
    td.appendChild(el("span", { class: "muted", text: "—" }));
    return td;
  }
  const wrap = el("div", { class: "now-watching" });
  const poster = posterEl(lw.image);
  if (poster) wrap.appendChild(poster);
  const info = el("div", { class: "now-info" });
  info.appendChild(el("div", { text: lw.name }));
  info.appendChild(el("div", { class: "now-time", text: fmtDate(lw.date) }));
  wrap.appendChild(info);
  td.appendChild(wrap);
  if (lw.id) registerPopup(wrap, { key: u.id + ":" + lw.id, itemId: lw.id, userId: u.id });
  return td;
}

async function toggleHistory(u) {
  const card = document.getElementById("user-history-card");
  if (selectedUserId === u.id) {
    card.hidden = true;
    selectedUserId = null;
    return;
  }
  selectedUserId = u.id;
  document.getElementById("user-history-title").textContent = "View statistics — " + u.name;
  card.hidden = false;
  const body = document.getElementById("history-body");
  body.innerHTML = "";
  body.appendChild(el("tr", {}, [el("td", { colspan: "3", class: "muted", text: "Loading…" })]));

  try {
    const data = await getJSON("/api/users/" + encodeURIComponent(u.id) + "/history");
    body.innerHTML = "";
    if (!data.ok) {
      body.appendChild(el("tr", {}, [el("td", { colspan: "3", class: "error", text: data.error })]));
      return;
    }
    const items = data.items || [];
    if (items.length === 0) {
      body.appendChild(el("tr", {}, [el("td", { colspan: "3", class: "muted", text: "No watch history." })]));
      return;
    }
    items.forEach((it) => {
      const tr = el("tr");
      tr.appendChild(el("td", { class: "muted", text: fmtDate(it.date) }));
      const nameTd = el("td", { text: it.name });
      tr.appendChild(nameTd);
      tr.appendChild(el("td", {
        class: it.completed ? "ok" : "muted",
        text: it.status,
      }));
      body.appendChild(tr);
      if (it.id) registerPopup(nameTd, { key: u.id + ":hist:" + it.id, itemId: it.id, userId: u.id });
    });
  } catch (err) {
    body.innerHTML = "";
    body.appendChild(el("tr", {}, [el("td", { colspan: "3", class: "error", text: String(err) })]));
  }
}

// ---- live polling ----------------------------------------------------------

const usersLive = LiveToggle("users-live", "medserv.users.live", 5000, () => {
  if (statusTimer || LOADING) return;  // don't fight the refresh-status poll
  loadUsers();
});

function isLiveEnabled() {
  return usersLive.enabled();
}

document.addEventListener("DOMContentLoaded", () => {
  loadUsers();
  const live = document.getElementById("users-live");
  if (live) live.addEventListener("change", renderUpdatedLabel);
  usersLive.apply(usersLive.stored(), false);
  ageTimer = setInterval(() => {
    if (!statusTimer && LAST_SYNC && !document.hidden) renderUpdatedLabel();
  }, 30000);
});
