"use strict";

let USERS = [];
let selectedUserId = null;

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  const p = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
         " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

async function loadUsers() {
  const body = document.getElementById("users-body");
  try {
    const data = await getJSON("/api/users");
    if (!data.ok) {
      body.innerHTML = "";
      body.appendChild(el("tr", {}, [el("td", { colspan: "4", class: "error", text: data.error })]));
      return;
    }
    USERS = data.users || [];
    renderUsers();
  } catch (err) {
    body.innerHTML = "";
    body.appendChild(el("tr", {}, [el("td", { colspan: "4", class: "error", text: String(err) })]));
  }
}

function renderUsers() {
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

    const lw = u.last_watched && u.last_watched.name
      ? fmtDate(u.last_watched.date) + " · " + u.last_watched.name
      : "—";
    tr.appendChild(el("td", { text: lw }));

    if (u.active) {
      const td = el("td");
      td.appendChild(el("div", { class: "muted", text: u.active.name + (u.active.paused ? " (paused)" : "") }));
      const bar = el("div", { class: "progress" });
      bar.appendChild(el("div", { class: "progress-fill", style: "width:" + u.active.pct + "%" }));
      td.appendChild(bar);
      tr.appendChild(td);
    } else {
      tr.appendChild(el("td", { class: "muted", text: "—" }));
    }

    tr.addEventListener("click", () => toggleHistory(u));
    body.appendChild(tr);
  });
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
      tr.appendChild(el("td", { text: it.name }));
      tr.appendChild(el("td", {
        class: it.completed ? "ok" : "muted",
        text: it.status,
      }));
      body.appendChild(tr);
    });
  } catch (err) {
    body.innerHTML = "";
    body.appendChild(el("tr", {}, [el("td", { colspan: "3", class: "error", text: String(err) })]));
  }
}

document.addEventListener("DOMContentLoaded", loadUsers);
