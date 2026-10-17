"use strict";

const SCH = { jobs: [], operations: [], editing: null };

function pad(n) { return String(n).padStart(2, "0"); }

function populateTimeSelects() {
  const hSel = document.getElementById("sch-hour");
  const mSel = document.getElementById("sch-minute");
  hSel.innerHTML = "";
  mSel.innerHTML = "";
  for (let h = 0; h < 24; h++) hSel.appendChild(el("option", { value: String(h), text: pad(h) }));
  for (let m = 0; m < 60; m++) mSel.appendChild(el("option", { value: String(m), text: pad(m) }));
}

function setTime(h, m) {
  document.getElementById("sch-hour").value = String(h);
  document.getElementById("sch-minute").value = String(m);
}

function getTime() {
  return { h: Number(document.getElementById("sch-hour").value), m: Number(document.getElementById("sch-minute").value) };
}

async function schLoad(refreshOps = true) {
  const body = document.getElementById("sch-body");
  try {
    const data = await getJSON("/api/scheduler/jobs");
    SCH.jobs = data.jobs || [];
    SCH.operations = data.operations || [];
    if (refreshOps) populateOpSelect();
    renderJobs();
  } catch (err) {
    body.innerHTML = "";
    body.appendChild(el("tr", {}, [el("td", { colspan: "8", class: "error", text: String(err) })]));
  }
}

function populateOpSelect() {
  const sel = document.getElementById("sch-op");
  sel.innerHTML = "";
  SCH.operations.forEach((op) => sel.appendChild(el("option", { value: op.id, text: op.label })));
}

function schBtn(label, fn, extraClass) {
  const b = el("button", { class: "mini" + (extraClass ? " " + extraClass : ""), text: label });
  b.addEventListener("click", fn);
  return b;
}

function renderJobs() {
  const body = document.getElementById("sch-body");
  body.innerHTML = "";
  if (SCH.jobs.length === 0) {
    body.appendChild(el("tr", {}, [el("td", { colspan: "8", class: "muted", text: "No schedules yet — click '+ New schedule'." })]));
    return;
  }
  SCH.jobs.forEach((j) => {
    const tr = el("tr");
    tr.appendChild(el("td", { text: j.operation_label }));
    tr.appendChild(el("td", { class: "muted sch-desc", text: j.operation_description || "" }));
    tr.appendChild(el("td", { text: j.schedule }));

    const td = el("td");
    const tog = el("button", { class: "toggle " + (j.enabled ? "on" : "off"), text: j.enabled ? "on" : "off" });
    tog.addEventListener("click", () => schToggle(j.id));
    td.appendChild(tog);
    tr.appendChild(td);

    const std = el("td");
    if (j.running) {
      const wrap = el("span", { class: "sch-running" });
      wrap.appendChild(el("span", { class: "spinner" }));
      wrap.appendChild(el("span", { text: "Running" }));
      std.appendChild(wrap);
    } else {
      std.appendChild(el("span", { class: "muted", text: "Idle" }));
    }
    tr.appendChild(std);

    tr.appendChild(el("td", { class: "muted nowrap", text: j.next_run || "—" }));

    let last = "—";
    if (j.last_run) {
      last = fmtAgo(j.last_run) + (j.last_status === "error" ? " ✗" : (j.last_status === "ok" ? " ✓" : ""));
    }
    tr.appendChild(el("td", { class: "muted", text: last }));

    const atd = el("td", { class: "sch-actions" });
    atd.appendChild(schBtn("Run now", () => schRun(j.id), "btn-run"));
    atd.appendChild(schBtn("Edit", () => schEdit(j), "btn-edit"));
    atd.appendChild(schBtn("Delete", () => schDelete(j.id), "btn-danger"));
    tr.appendChild(atd);
    body.appendChild(tr);
  });
}

async function schRun(id) {
  await postJSON("/api/scheduler/jobs/" + id + "/run", null);
  schLoad();
}

async function schToggle(id) {
  await postJSON("/api/scheduler/jobs/" + id + "/toggle", null);
  schLoad();
}

async function schDelete(id) {
  if (!confirm("Delete this schedule?")) return;
  await requestJSON("/api/scheduler/jobs/" + id, { method: "DELETE" });
  schLoad();
}

function showForm(show) {
  document.getElementById("sch-form-card").hidden = !show;
}

function schNew() {
  SCH.editing = null;
  document.getElementById("sch-form-title").textContent = "New schedule";
  document.getElementById("sch-op").value = SCH.operations.length ? SCH.operations[0].id : "";
  document.getElementById("sch-freq").value = "daily";
  setTime(3, 0);
  document.getElementById("sch-day").value = "mon";
  document.getElementById("sch-cron").value = "0 3 * * *";
  document.getElementById("sch-enabled").checked = true;
  syncFreqFields();
  showForm(true);
}

function freqFromTrigger(t) {
  if (t.kind === "interval") {
    const map = { 15: "every15m", 30: "every30m", 60: "every1h", 180: "every3h", 360: "every6h", 720: "every12h" };
    return map[t.minutes] || "every1h";
  }
  if (t.kind === "cron") return t.day_of_week ? "weekly" : "daily";
  if (t.kind === "crontab") return "cron";
  return "daily";
}

function schEdit(job) {
  SCH.editing = job.id;
  document.getElementById("sch-form-title").textContent = "Edit schedule";
  document.getElementById("sch-op").value = job.operation;
  const t = job.trigger || {};
  document.getElementById("sch-freq").value = freqFromTrigger(t);
  setTime(t.hour || 0, t.minute || 0);
  if (t.day_of_week) document.getElementById("sch-day").value = t.day_of_week;
  if (t.expr) document.getElementById("sch-cron").value = t.expr;
  document.getElementById("sch-enabled").checked = !!job.enabled;
  syncFreqFields();
  showForm(true);
}

function syncFreqFields() {
  const freq = document.getElementById("sch-freq").value;
  document.getElementById("sch-time-label").hidden = !(freq === "daily" || freq === "weekly");
  document.getElementById("sch-day-label").hidden = freq !== "weekly";
  document.getElementById("sch-cron-label").hidden = freq !== "cron";
}

function buildTrigger() {
  const freq = document.getElementById("sch-freq").value;
  const { h, m } = getTime();
  const day = document.getElementById("sch-day").value;
  if (freq === "every15m") return { kind: "interval", minutes: 15 };
  if (freq === "every30m") return { kind: "interval", minutes: 30 };
  if (freq === "every1h") return { kind: "interval", minutes: 60 };
  if (freq === "every3h") return { kind: "interval", minutes: 180 };
  if (freq === "every6h") return { kind: "interval", minutes: 360 };
  if (freq === "every12h") return { kind: "interval", minutes: 720 };
  if (freq === "daily") return { kind: "cron", hour: h, minute: m };
  if (freq === "weekly") return { kind: "cron", day_of_week: day, hour: h, minute: m };
  if (freq === "cron") return { kind: "crontab", expr: document.getElementById("sch-cron").value.trim() || "0 3 * * *" };
  return { kind: "interval", minutes: 60 };
}

async function schSave(e) {
  e.preventDefault();
  const payload = {
    operation: document.getElementById("sch-op").value,
    trigger: buildTrigger(),
    enabled: document.getElementById("sch-enabled").checked,
  };
  const url = SCH.editing ? "/api/scheduler/jobs/" + SCH.editing : "/api/scheduler/jobs";
  const method = SCH.editing ? "PUT" : "POST";
  await requestJSON(url, {
    method: method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  showForm(false);
  schLoad();
}

let schTimer = null;
function startSchPolling() {
  if (schTimer) return;
  schTimer = setInterval(() => {
    if (!document.hidden) schLoad(false);
  }, 5000);
}

document.addEventListener("DOMContentLoaded", () => {
  populateTimeSelects();
  document.getElementById("sch-new").addEventListener("click", schNew);
  document.getElementById("sch-cancel").addEventListener("click", () => showForm(false));
  document.getElementById("sch-freq").addEventListener("change", syncFreqFields);
  document.getElementById("sch-form").addEventListener("submit", schSave);
  schLoad();
  startSchPolling();
});
