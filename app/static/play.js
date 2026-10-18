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
const plOverlay = document.getElementById("pl-overlay");
const plCaption = document.getElementById("pl-caption");

const PL_AUTOPLAY_MSG = "Autoplay was blocked — press ▶ to start.";

let plTracks = [];      // track descriptors from /api/player/tracks
let plTrackEls = [];    // matching <track> elements
let plCurrent = -1;     // selected track index, -1 for off
let plPrepared = false; // streaming the remuxed copy?
let plPreparePoll = null;
let plDead = false;
let plAudioChecked = false;
let plPlayTries = 0;    // rejected play() attempts, for the retry ladder
let plCaptionText = null;

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

// ---- autoplay ----------------------------------------------------------------

function plSetOverlay(show) {
  if (plOverlay) plOverlay.hidden = !show;
}

// Browsers block unmuted autoplay until the origin has earned it, and the
// click that opened the window does not always count. Rather than leaving the
// viewer staring at a paused frame, retry a few times and then offer a button.
function plTryPlay() {
  if (plDead) return;
  if (!plVideo.paused) {
    plSetOverlay(false);
    return;
  }
  const attempt = plVideo.play();
  if (!attempt || typeof attempt.then !== "function") return;
  attempt.then(() => {
    plSetOverlay(false);
    if (plStatus.textContent === PL_AUTOPLAY_MSG) plSetStatus("");
  }).catch(() => {
    plPlayTries += 1;
    if (plPlayTries < 4) {
      setTimeout(plTryPlay, 500 * plPlayTries);
      return;
    }
    plSetOverlay(true);
    if (plStatus.textContent !== PL_AUTOPLAY_MSG && !plStatus.classList.contains("error")) {
      plSetStatus(PL_AUTOPLAY_MSG, "warn");
    }
  });
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

// Cues are drawn here rather than left to the browser. Browsers size, place and
// even refuse to draw the native caption layer on their own, and the whole point
// of this window is to see the text.
function plRenderCues() {
  if (!plCaption) return;
  const node = plTrackEls[plCurrent];
  const track = node && node.track;
  let text = "";
  if (track && track.mode !== "disabled" && track.activeCues && !plNativeShowing()) {
    text = Array.from(track.activeCues).map((c) => c.text || "").join("\n");
  }
  if (text === plCaptionText) return;
  plCaptionText = text;
  plCaption.textContent = text;
  plCaption.hidden = !text;
}

// If the viewer turns captions on from the browser's own menu, its renderer
// takes over and ours would double up.
function plNativeShowing() {
  return plTrackEls.some((n) => n.track && n.track.mode === "showing");
}

function plNoteCues(index) {
  if (index !== plCurrent) return;
  const node = plTrackEls[index];
  const track = node && node.track;
  if (!track || !track.cues) return;
  const label = (plTracks[index] || {}).label || "That track";
  if (!track.cues.length) {
    plSetStatus("“" + label + "” holds no cues, so there is nothing to show.", "warn");
  } else if (plStatus.textContent !== PL_AUTOPLAY_MSG && !plPrepared) {
    const n = track.cues.length;
    plSetStatus((n === 1 ? "1 cue" : n + " cues") + " · " + label);
  }
  // A track that finishes loading while the video sits still does not always
  // produce a cuechange, so draw whatever is current now.
  plRenderCues();
}

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
    track.addEventListener("cuechange", plRenderCues);
    track.addEventListener("load", () => plNoteCues(i));
    track.addEventListener("error", () => {
      if (plTrackEls.indexOf(track) !== plCurrent) return;
      plSetStatus("The browser could not load that subtitle track.", "error");
    });
    plVideo.appendChild(track);
    plTrackEls.push(track);
  });
}

function plApplyModes(index) {
  plTrackEls.forEach((node, i) => {
    const textTrack = node.track;
    if (!textTrack) return;
    // "hidden" still loads the cues and keeps activeCues current — the browser
    // just doesn't draw them, and plRenderCues does.
    textTrack.mode = (i === index && plTracks[i] && plTracks[i].playable) ? "hidden" : "disabled";
  });
  plCurrent = index;
  plSubs.value = index >= 0 ? String(index) : "";
  plRenderCues();
}

// The browser's own caption menu can turn a track on behind our back; follow it.
function plOnTracksChange() {
  const showing = plTrackEls.findIndex((n) => n.track && n.track.mode === "showing");
  if (showing >= 0 && showing !== plCurrent) {
    plCurrent = showing;
    plSubs.value = String(showing);
  }
  plRenderCues();
}

// A failed conversion is easy to miss, so ask the server for the WebVTT first
// and show its reason when a track can't be shown.
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
  if (plPrepared) plSetStatus("Playing the browser copy.");
  else if (plStatus.textContent !== PL_AUTOPLAY_MSG) plSetStatus("");
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
    plTryPlay();
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
  plSetOverlay(false);
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
  plTryPlay();

  plVideo.addEventListener("timeupdate", plUpdateTime);
  plVideo.addEventListener("loadedmetadata", plUpdateTime);
  plVideo.addEventListener("durationchange", plUpdateTime);
  plVideo.addEventListener("timeupdate", plRenderCues);
  plVideo.addEventListener("seeked", plRenderCues);
  plVideo.addEventListener("pause", plRenderCues);
  plVideo.textTracks.addEventListener("change", plOnTracksChange);
  plVideo.addEventListener("canplay", plTryPlay);
  plVideo.addEventListener("play", () => plSetOverlay(false));
  plVideo.addEventListener("error", plOnVideoError);

  // The click that opened the window is often the only user gesture we get, so
  // the first one anywhere in here is a chance to start playback for real.
  document.addEventListener("pointerdown", () => plTryPlay(), { once: true });
  if (plOverlay) plOverlay.addEventListener("click", plTryPlay);

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
    // A track named in the link wins; otherwise show the best one so subtitles
    // are visible without hunting through the selector.
    let index = PL.track ? plTracks.findIndex((t) => t.spec === PL.track) : -1;
    if (index < 0) index = plTracks.findIndex((t) => t.default && t.playable);
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
