"use strict";

// Audio tracks dialog (Search, Movie Match and Libraries).
//
// Opened from the row's "Audio" pill. It lists one row per embedded audio
// stream and can delete one of them. There is no search: audio tracks are
// either in the file or not. Deleting rewrites the video in place with ffmpeg
// (every other stream copied as-is), so it is gated on File management and
// asks for confirmation.
//
// The dialog reuses the subtitle dialog's shell classes (``subdl-*``) so the
// two look and behave the same. Pages that show an Audio column assign
// ``window.audioRefresh``; it receives the freshly probed scan entry.

window.audioRefresh = null;

let audioPath = null;
let audioTracks = [];
let audioFileMgmt = false;
let audioBack = null;

const AUDIO_CODECS = {
  aac: "AAC", ac3: "AC3", eac3: "E-AC-3", dts: "DTS", truehd: "TrueHD",
  flac: "FLAC", opus: "Opus", mp3: "MP3", vorbis: "Vorbis", alac: "ALAC",
};

function audioTech(t) {
  const codec = AUDIO_CODECS[(t.codec || "").toLowerCase()] || (t.codec || "?").toUpperCase();
  const layout = t.layout && t.layout !== "unknown" ? t.layout : "";
  return [codec, layout].filter(Boolean).join(" ");
}

// The write routes check X-JFTools: 1 (the same header the subtitle dialog
// sends); the generic postJSON helper does not.
async function audioPost(url, body) {
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

function audioNotice(msg, kind) {
  const statusEl = document.getElementById("audio-status");
  if (!statusEl) return;
  const cls = kind === "ok" ? "ok" : (kind === "error" ? "error" : "muted");
  statusEl.innerHTML = "";
  statusEl.appendChild(el("p", { class: cls, text: msg }));
}

function audioRender() {
  const box = document.getElementById("audio-tracks");
  if (!box) return;
  box.innerHTML = "";
  box.appendChild(el("h4", { text: "Audio tracks" }));
  if (!audioTracks.length) {
    box.appendChild(el("p", { class: "muted", text: "No audio tracks on this file." }));
    return;
  }
  audioTracks.forEach((t) => {
    const row = el("div", { class: "subdl-subrow" });
    row.appendChild(el("span", { class: "subdl-tag", text: t.default ? "default" : "audio" }));
    const label = t.lang || "Unknown";
    if (t.title && !t.commentary) {
      row.appendChild(el("span", { class: "subdl-name", text: label + " — " + t.title, title: label + " — " + t.title }));
    } else {
      row.appendChild(el("span", { class: "subdl-name", text: label, title: label }));
    }
    if (t.commentary) row.appendChild(el("span", { class: "subdl-badge", text: "commentary" }));
    row.appendChild(el("span", { class: "muted", text: audioTech(t) }));

    const del = el("button", { type: "button", class: "subdl-mini subdl-del", text: "Delete" });
    // Refuse to leave the video silent: the server enforces this too.
    if (!audioFileMgmt || audioTracks.length <= 1) {
      del.disabled = true;
      del.title = audioFileMgmt
        ? "This is the only audio track"
        : "File management is off";
    } else {
      del.addEventListener("click", () => audioDelete(t, del));
    }
    row.appendChild(del);
    box.appendChild(row);
  });
}

async function audioLoad() {
  audioTracks = [];
  try {
    const data = await audioPost("/api/audio/existing", "path=" + encodeURIComponent(audioPath));
    if (!data || !data.ok) {
      audioNotice((data && data.error) || "Could not read the audio tracks.", "error");
      audioRender();
      return;
    }
    audioFileMgmt = !!data.file_mgmt;
    audioTracks = (data.audio || []).slice();
    if (data.error) audioNotice("Probe warning: " + data.error, "error");
    const warn = document.getElementById("audio-fm");
    if (warn) warn.hidden = audioFileMgmt;
    audioRender();
  } catch (err) {
    audioNotice(String(err), "error");
    audioRender();
  }
}

async function audioDelete(track, btn) {
  if (!audioFileMgmt) return;
  const name = track.name || track.lang || "this audio track";
  if (!window.confirm(
    "Delete " + name + " from " + audioPath.replace(/^.*[\\/]/, "") + "?\n" +
    "The video is rewritten in place and needs free space equal to its size."
  )) return;
  btn.disabled = true;
  btn.textContent = "Deleting…";
  audioNotice("Rewriting the file — this can take a while…", "muted");
  try {
    const data = await audioPost(
      "/api/audio/delete",
      "path=" + encodeURIComponent(audioPath) + "&index=" + encodeURIComponent(track.index)
    );
    if (!data || !data.ok) {
      audioNotice((data && data.error) || "Delete failed.", "error");
      btn.disabled = false;
      btn.textContent = "Delete";
      return;
    }
    audioTracks = ((data.entry && data.entry.audio) || []).slice();
    let msg = data.message || "Deleted.";
    if (data.jellyfin) msg += " (Jellyfin warning: " + data.jellyfin + ")";
    audioNotice(msg, "ok");
    audioRender();
    if (audioPath && window.audioRefresh) window.audioRefresh(audioPath, data.entry);
  } catch (err) {
    audioNotice(String(err), "error");
    btn.disabled = false;
    btn.textContent = "Delete";
  }
}

function audioClose() {
  if (audioBack) {
    document.body.removeChild(audioBack);
    audioBack = null;
  }
  document.removeEventListener("keydown", audioOnKey);
  audioPath = null;
  audioTracks = [];
}

function audioOnKey(e) {
  if (e.key === "Escape") audioClose();
}

function audioOpen(path) {
  if (!path) return;
  audioClose();
  audioPath = path;
  audioTracks = [];

  const back = el("div", { class: "subdl-back" });
  const box = el("div", { class: "subdl" });
  box.appendChild(el("h3", { text: "Audio" }));
  box.appendChild(el("p", { class: "muted", text: path.replace(/^.*[\\/]/, "") }));
  const warn = el("p", {
    class: "muted subdl-fm",
    hidden: true,
    text: "File management is off — deleting an audio track is disabled. Enable it on the Settings page.",
  });
  warn.id = "audio-fm";
  box.appendChild(warn);
  box.appendChild(el("div", { id: "audio-status", class: "subdl-status" }));
  box.appendChild(el("div", { id: "audio-tracks", class: "subdl-existing" }));

  back.appendChild(box);
  document.body.appendChild(back);
  back.addEventListener("mousedown", (e) => { if (e.target === back) audioClose(); });
  document.addEventListener("keydown", audioOnKey);
  audioBack = back;

  audioLoad();
}
