"use strict";

// The player window: streams one library video, offers every subtitle track the
// scanner knows about (embedded streams are extracted, external files are
// converted to WebVTT server-side), and can ask the server to remux a file the
// browser cannot decode into a cached MP4 it can.

const PL = window.JF_PLAY || { path: "", file: "", track: "" };

const plVideo = document.getElementById("pl-video");
const plFile = document.getElementById("pl-file");
const plStatus = document.getElementById("pl-status");
const plTime = document.getElementById("pl-time");
const plSubs = document.getElementById("pl-subs");
const plRate = document.getElementById("pl-rate");
const plGoto = document.getElementById("pl-goto");
const plPrepare = document.getElementById("pl-prepare");

let plTracks = [];      // track descriptors from /api/player/tracks
let plTrackEls = [];    // matching <track> elements
let plCurrent = -1;     // selected track index, -1 for off
let plPrepared = false; // streaming the remuxed copy?
let plPreparePoll = null;
let plDead = false;
let plAudioChecked = false;

function plEl(tag, props) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === "text") node.textContent = value;
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  return node;
}

function plEnc(s) { return encodeURIComponent(s || ""); }

function plSetStatus(text, kind) {
  plStatus.textContent = text || "";
  plStatus.className = "play-status" + (kind ? " " + kind : " muted");
}

function plStreamUrl(prepared) {
  return "/api/player/stream?path=" + plEnc(PL.path) + (prepared ? "&prepared=1" : "");
}

function plSubUrl(track) {
  return "/api/player/sub?path=" + plEnc(PL.path) + "&track=" + plEnc(track.spec);
}

// ---- timeline ----------------------------------------------------------------

function plFmt(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return (h ? h + ":" : "") + mm + ":" + String(s).padStart(2, "0");
}

function plUpdateTime() {
  plTime.textContent = plFmt(plVideo.currentTime) + " / " + plFmt(plVideo.duration);
}

function plParseTime(text) {
  const parts = String(text || "").trim().split(":").map((p) => p.trim().replace(",", "."));
  if (!parts.length || parts.some((p) => p === "")) return NaN;
  let total = 0;
  for (const part of parts) {
    const value = Number(part);
    if (!isFinite(value)) return NaN;
    total = total * 60 + value;
  }
  return total;
}

// ---- subtitle tracks ---------------------------------------------------------

function plBuildTrackSelect(list) {
  plTracks = list.slice();
  plTrackEls = [];
  plSubs.innerHTML = "";
  plSubs.appendChild(plEl("option", { value: "", text: list.length ? "Off" : "No subtitles" }));
  plSubs.disabled = !list.length;

  list.forEach((t, i) => {
    const opt = plEl("option", {
      value: String(i),
      text: t.playable ? t.label : t.label + " — unavailable",
    });
    if (!t.playable) opt.disabled = true;
    plSubs.appendChild(opt);

    const track = plEl("track", {
      kind: "subtitles",
      label: t.label,
      srclang: (t.lang && t.lang.length <= 3) ? t.lang.toLowerCase() : "und",
    });
    plVideo.appendChild(track);
    plTrackEls.push(track);
  });
}

function plApplyModes(index) {
  plTrackEls.forEach((node, i) => {
    const textTrack = node.track;
    if (!textTrack) return;
    textTrack.mode = (i === index && plTracks[i] && plTracks[i].playable) ? "showing" : "disabled";
  });
  plCurrent = index;
  plSubs.value = index >= 0 ? String(index) : "";
}

// The browser only fetches a <track> once its mode turns to showing, and a
// failed fetch is easy to miss, so check the conversion respond first and show
// the server's reason when a track can't be shown.
async function plChooseTrack(value) {
  if (value === "" || value === null) {
    plApplyModes(-1);
    plSetStatus("");
    return;
  }
  const index = Number(value);
  const track = plTracks[index];
  if (!track) return;
  if (!track.playable) {
    plApplyModes(-1);
    plSetStatus("That track holds image-based subtitles, which a browser can't show.", "error");
    return;
  }
  const url = plSubUrl(track);
  try {
    const res = await fetch(url);
    if (!res.ok) {
      plApplyModes(-1);
      plSetStatus((await res.text()) || "Could not load that subtitle.", "error");
      return;
    }
  } catch (err) {
    plApplyModes(-1);
    plSetStatus("Could not load that subtitle: " + err, "error");
    return;
  }
  if (!plTrackEls[index].getAttribute("src")) {
    plTrackEls[index].setAttribute("src", url);
  }
  plApplyModes(index);
  plSetStatus(plPrepared ? "Playing the browser copy." : "");
}

async function plLoadTracks() {
  try {
    const res = await fetch("/api/player/tracks?path=" + plEnc(PL.path));
    const data = await res.json();
    if (!data || !data.ok) {
      plBuildTrackSelect([]);
      plSetStatus((data && data.error) || "Could not list the subtitle tracks.", "error");
      return;
    }
    plBuildTrackSelect(data.tracks || []);
  } catch (err) {
    plBuildTrackSelect([]);
    plSetStatus("Could not list the subtitle tracks: " + err, "error");
  }
}

// ---- browser copy (remux) ----------------------------------------------------

function plSwitchToPrepared() {
  const at = plVideo.currentTime || 0;
  plPrepared = true;
  plDead = false;
  plPrepare.hidden = true;
  plSetStatus("Playing the browser copy.");
  const restore = () => {
    plVideo.removeEventListener("loadedmetadata", restore);
    if (at > 0) {
      try { plVideo.currentTime = at; } catch (err) { /* ignore */ }
    }
    plApplyModes(plCurrent);
    plVideo.play().catch(() => {});
  };
  plVideo.addEventListener("loadedmetadata", restore);
  plVideo.src = plStreamUrl(true);
  plVideo.load();
}

async function plPollPrepare() {
  clearInterval(plPreparePoll);
  plPreparePoll = setInterval(async () => {
    let data = {};
    try {
      const res = await fetch("/api/player/prepare/status?path=" + plEnc(PL.path));
      data = await res.json();
    } catch (err) {
      return; // keep polling through a blip
    }
    if (data.status === "running") {
      plSetStatus(data.message || "Making a browser copy…");
      return;
    }
    clearInterval(plPreparePoll);
    plPrepare.disabled = false;
    if (data.status === "done") plSwitchToPrepared();
    else if (data.status === "error") plSetStatus(data.error || "The browser copy failed.", "error");
    else plSetStatus("");
  }, 1500);
}

async function plPrepareCopy() {
  if (plPrepare.disabled) return;
  plPrepare.disabled = true;
  plSetStatus("Starting…");
  try {
    const res = await fetch("/api/player/prepare?path=" + plEnc(PL.path));
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      plSetStatus(data.error || "Could not start the browser copy.", "error");
      plPrepare.disabled = false;
      return;
    }
    if (data.status === "done") {
      plSwitchToPrepared();
      return;
    }
    plPollPrepare();
  } catch (err) {
    plSetStatus("Could not start the browser copy: " + err, "error");
    plPrepare.disabled = false;
  }
}

function plSuggestCopy(message) {
  plSetStatus(message, "warn");
  plPrepare.hidden = false;
  plPrepare.disabled = false;
}

// Ask the server why the video element gave up. A file that is not reachable
// (bad Media paths, or gone from disk) is not something a remux can fix, so
// report the server's own message instead of suggesting a browser copy.
async function plStreamError() {
  const controller = new AbortController();
  try {
    const res = await fetch(plStreamUrl(false), {
      signal: controller.signal,
      headers: { Range: "bytes=0-0" },
    });
    if (res.ok) return "";
    const data = await res.json().catch(() => null);
    return (data && data.error) || "";
  } catch (err) {
    return "";
  } finally {
    controller.abort();
  }
}

async function plExplainFailure(message) {
  const serverError = await plStreamError();
  if (serverError) {
    plPrepare.hidden = true;
    plSetStatus(serverError, "error");
    return;
  }
  plSuggestCopy(message);
}

async function plOnVideoError() {
  if (plDead || plPrepared) return;
  plDead = true;
  await plExplainFailure(
    "This browser can't play the file as it is (usually the container, video " +
      "or audio codec). Make a browser copy to remux it with ffmpeg."
  );
}

// ---- wiring ------------------------------------------------------------------

function plInit() {
  plFile.textContent = PL.file || "Player";
  plFile.title = PL.path || "";
  if (!PL.path) {
    plSetStatus("No file to play.", "error");
    return;
  }

  plVideo.src = plStreamUrl(false);
  plVideo.load();
  plVideo.play().catch(() => {}); // popups from a click are allowed to autoplay

  plVideo.addEventListener("timeupdate", plUpdateTime);
  plVideo.addEventListener("loadedmetadata", plUpdateTime);
  plVideo.addEventListener("durationchange", plUpdateTime);
  plVideo.addEventListener("error", plOnVideoError);

  // A container the browser can open but whose audio it cannot decode (AC3,
  // DTS, …) plays silently, with no error at all. Chrome counts decoded audio
  // bytes, so a file with audio tracks and nothing decoded is a silent one.
  plVideo.addEventListener("timeupdate", () => {
    if (plDead || plPrepared || plAudioChecked) return;
    if (plVideo.currentTime < 10) return;
    if (typeof plVideo.webkitAudioDecodedByteCount !== "number") return;
    if (plVideo.webkitAudioDecodedByteCount > 0) return;
    // Firefox can say whether the file has audio at all; Chrome cannot, and a
    // muted-by-missing-codec file is the case worth flagging.
    if (typeof plVideo.mozHasAudio === "boolean" && !plVideo.mozHasAudio) return;
    if (plVideo.audioTracks && plVideo.audioTracks.length === 0) return;
    plAudioChecked = true;
    plSuggestCopy(
      "This file's audio isn't playing (the browser can't decode its audio " +
        "codec). Make a browser copy to remux it with AAC audio."
    );
  });

  plVideo.addEventListener("stalled", () => {
    if (!plVideo.duration) plSuggestCopy("The browser stopped loading this file. Make a browser copy if it won't play.");
  });

  // A file the browser silently refuses to decode never fires "error" on every
  // platform, so offer the copy after a while too.
  setTimeout(() => {
    if (!plDead && !plVideo.duration) {
      plExplainFailure("Still loading… if this file won't play, make a browser copy.");
    }
  }, 12000);

  document.querySelectorAll("[data-skip]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const delta = Number(btn.dataset.skip) || 0;
      const duration = plVideo.duration || 0;
      let next = plVideo.currentTime + delta;
      if (duration) next = Math.min(next, duration);
      plVideo.currentTime = Math.max(0, next);
    });
  });

  plGoto.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      plJump();
    }
  });
  document.getElementById("pl-goto-go").addEventListener("click", plJump);

  plRate.addEventListener("change", () => {
    plVideo.playbackRate = Number(plRate.value) || 1;
  });
  plVideo.addEventListener("ratechange", () => {
    plRate.value = String(plVideo.playbackRate);
  });

  plSubs.addEventListener("change", () => plChooseTrack(plSubs.value));
  plPrepare.addEventListener("click", plPrepareCopy);

  plUpdateTime();
  plLoadTracks().then(() => {
    if (!PL.track) return;
    const index = plTracks.findIndex((t) => t.spec === PL.track);
    if (index >= 0) plChooseTrack(String(index));
  });
}

function plJump() {
  const seconds = plParseTime(plGoto.value);
  if (!isFinite(seconds)) {
    plSetStatus("Enter a time like 1:23 or 0:12:45.", "error");
    return;
  }
  const duration = plVideo.duration || 0;
  plVideo.currentTime = duration ? Math.min(seconds, duration) : seconds;
  plSetStatus("");
}

plInit();
