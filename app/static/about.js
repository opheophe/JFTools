"use strict";

function aboutFormatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

async function aboutLoad() {
  try {
    const d = await getJSON("/api/about/refresh");
    let installed = d.version ? "v" + d.version : "—";
    if (d.built) installed += " · built " + aboutFormatTime(d.built);
    document.getElementById("about-version").textContent = installed;

    const latest = document.getElementById("about-latest");
    latest.innerHTML = "";
    if (d.latest) {
      latest.appendChild(el("a", {
        class: "btn-link",
        href: d.latest_url,
        target: "_blank",
        rel: "noopener",
        text: "v" + d.latest,
      }));
      if (d.published) latest.appendChild(el("span", { class: "muted", text: " · released " + d.published }));
    } else {
      latest.textContent = "Unknown";
    }

    const status = document.getElementById("about-status");
    if (d.update_available) {
      status.textContent = "Update available";
      status.className = "warn";
    } else if (d.latest) {
      status.textContent = "Up to date";
      status.className = "ok";
    } else {
      status.textContent = "Could not reach GitHub";
      status.className = "muted";
    }
  } catch (err) {
    const status = document.getElementById("about-status");
    status.textContent = "Could not load version info";
    status.className = "error";
  }
}

document.addEventListener("DOMContentLoaded", aboutLoad);
