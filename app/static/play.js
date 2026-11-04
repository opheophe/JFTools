"use strict";

// The player window: streams one library video, offers every subtitle track the
// scanner knows about (embedded streams are extracted, external files are
// converted to WebVTT server-side), and can ask the server to remux a file the
// browser cannot decode into a cached MP4 it can.

const PL = window.JF_PLAY || { path: "", file: "", track: "" };

// The Subfind dialog embeds this page in an iframe; the popup window does not.
const PL_EMBEDDED = window.self !== window.top;

const plVideo = document.getElementById("pl-video");
const plAudio = document.getElementById("pl-audio");
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
let plCompanion = false; // playing the video muted + the converted audio track?
let plPreparePoll = null;
let plPrepareKind = "audio"; // what the Fix audio button will ask the server for
let plDead = false;
let plAudioChecked = false;
let plHasAudio = null;  // server's answer: null unknown, false = no audio stream
let plCopyWhy = "";     // e.g. "DTS audio": the server knows it won't play
let plStatusHold = false; // the status line is explaining something, not noting
let plPlayTries = 0;    // rejected play() attempts, for the retry ladder
let plCaptionText = null;
let plSyncTimer = null; // drift correction while the companion audio plays
let plHls = null;       // hls.js instance when playing Jellyfin's HLS stream
let plHlsToken = "";    // server-side HLS session token, "" when not using HLS
let plHlsAvailable = false; // Jellyfin can stream this item
let plHlsRetries = 0;
let plHlsProgress = null;
let plKnownDuration = 0; // runtime from the scan store, for HLS playlists
let plSourceStarting = false;
let plHlsStarting = false;

function plEl(tag, props) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === "text") node.textContent = value;
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  return node;
}

function plEnc(s) { return encodeURIComponent(s || ""); }

// Tell the embedding page (the Subfind dialog) which subtitle track is showing
// so its Sync and Delete buttons act on the right file. Does nothing when this
// page is the popup window.
function plPostParent(msg) {
  if (!PL_EMBEDDED) return;
  try {
    window.parent.postMessage({ jftools: true, ...msg }, window.location.origin);
  } catch (err) {
    /* ignore */
  }
}

function plSetStatus(text, kind) {
  plStatus.textContent = text || "";
  plStatus.className = "play-status" + (kind ? " " + kind : " muted");
  // A warning or an error explains something the viewer needs to know, so the
  // passing "N cues · track" notes that loading a subtitle track produces must
  // not talk over it.
  plStatusHold = kind === "warn" || kind === "error";
}

function plStatusHeld() {
  return plStatusHold;
}

// The status to show when nothing else is going on.
function plIdleStatus() {
  if (plStatusHeld()) return;
  if (plPrepared) plSetStatus(plPlayingCopy(), plCopyWhy ? "warn" : "");
  else if (plCompanion) plSetStatus(plPlayingCompanion(), plCopyWhy ? "warn" : "");
  else if (plHlsToken) plSetStatus("Playing the Jellyfin stream.");
  else plSetStatus("");
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
    if (!plStatus.classList.contains("error")) plSetStatus(PL_AUTOPLAY_MSG, "warn");
  });
}

function plStreamUrl(prepared) {
  return "/api/player/stream?path=" + plEnc(PL.path) + (prepared ? "&prepared=1" : "");
}

function plSubUrl(track) {
  return "/api/player/sub?path=" + plEnc(PL.path) + "&track=" + plEnc(track.spec);
}

// ---- Jellyfin HLS ------------------------------------------------------------

// Ask the server to stop the Jellyfin transcode behind this page. Called when
// switching sources and when the window goes away.
function plStopHls() {
  clearInterval(plHlsProgress);
  plHlsProgress = null;
  if (plHls) {
    try { plHls.destroy(); } catch (err) { /* ignore */ }
    plHls = null;
  }
  const token = plHlsToken;
  plHlsToken = "";
  if (!token) return;
  const position = Math.max(0, plVideo.currentTime || 0);
  const url = "/api/player/hls/" + token + "/stop" +
    "?position=" + encodeURIComponent(position);
  try {
    if (navigator.sendBeacon) navigator.sendBeacon(url);
    else fetch(url, { method: "POST", keepalive: true }).catch(() => {});
  } catch (err) {
    fetch(url, { method: "POST", keepalive: true }).catch(() => {});
  }
}

function plHlsProgressPing() {
  if (!plHlsToken || plDead) return;
  const url = "/api/player/hls/" + plHlsToken + "/progress" +
    "?position=" + encodeURIComponent(plVideo.currentTime || 0) +
    "&paused=" + (plVideo.paused ? "true" : "false");
  fetch(url, { method: "POST", keepalive: true }).catch(() => { /* best effort */ });
}

function plHlsFatal(message) {
  plStopHls();
  plDead = true;
  plSetOverlay(false);
  plSuggestCopy(message + " Make a browser copy to remux it with ffmpeg.");
}

function plOnHlsError(data) {
  if (!data || !data.fatal) return;
  if (data.type === Hls.ErrorTypes.NETWORK_ERROR && plHlsRetries < 2) {
    plHlsRetries += 1;
    plSetStatus("The Jellyfin stream stalled — retrying…", "warn");
    try { plHls.startLoad(); } catch (err) { /* fall through below */ }
    return;
  }
  if (data.type === Hls.ErrorTypes.MEDIA_ERROR && plHlsRetries < 2) {
    plHlsRetries += 1;
    plSetStatus("The Jellyfin stream hit a media error — recovering…", "warn");
    try { plHls.recoverMediaError(); } catch (err) { /* fall through below */ }
    return;
  }
  plHlsFatal("This browser could not play the Jellyfin stream.");
}

async function plStartHls(startSeconds) {
  if (plDead || plHlsStarting) return false;
  plHlsStarting = true;
  plStopHls();
  plStopCompanion();
  clearInterval(plPreparePoll);
  plPreparePoll = null;
  plPrepare.hidden = true;
  plPrepare.disabled = false;
  plDead = false;
  plSetStatus("Starting the Jellyfin stream…", "warn");
  try {
    const res = await fetch(
      "/api/player/hls/start?path=" + plEnc(PL.path) +
      "&start=" + encodeURIComponent(startSeconds || 0)
    );
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      plSetStatus(data.error || "Jellyfin could not start this stream.", "warn");
      return false;
    }
    plHlsToken = data.token;
    plHlsRetries = 0;
    const url = data.playlist;

    if (window.Hls && Hls.isSupported()) {
      plHls = new Hls({
        enableWorker: true,
        lowLatencyMode: false,
        backBufferLength: 120,
        maxBufferLength: 60,
        manifestLoadingTimeOut: 20000,
        levelLoadingTimeOut: 20000,
        fragLoadingTimeOut: 60000,
      });
      plHls.attachMedia(plVideo);
      plHls.on(Hls.Events.MEDIA_ATTACHED, () => plHls.loadSource(url));
      plHls.on(Hls.Events.MANIFEST_PARSED, () => {
        plPlayTries = 0;
        plSetOverlay(false);
        plApplyModes(plCurrent);
        plTryPlay();
      });
      plHls.on(Hls.Events.FRAG_LOADED, () => {
        // Clear a "retrying…" note once data is flowing again, without
        // talking over a subtitle warning.
        if (plStatus.textContent.indexOf("The Jellyfin stream") === 0) {
          plSetStatus("Playing the Jellyfin stream.");
        }
      });
      plHls.on(Hls.Events.ERROR, (evt, detail) => plOnHlsError(detail));
    } else if (plVideo.canPlayType("application/vnd.apple.mpegurl")) {
      // Safari and iOS play HLS natively.
      const restore = () => {
        plVideo.removeEventListener("loadedmetadata", restore);
        plApplyModes(plCurrent);
        plTryPlay();
      };
      plVideo.addEventListener("loadedmetadata", restore);
      plVideo.src = url;
      plVideo.load();
    } else {
      plSetStatus("This browser cannot play HLS streams.", "warn");
      plStopHls();
      return false;
    }

    clearInterval(plHlsProgress);
    plHlsProgress = setInterval(plHlsProgressPing, 10000);
    plSetStatus("Playing the Jellyfin stream.");
    return true;
  } catch (err) {
    plSetStatus("Could not start the Jellyfin stream: " + err, "warn");
    plStopHls();
    return false;
  } finally {
    plHlsStarting = false;
  }
}

// Can this browser open the original file without Jellyfin? Containers are
// checked by extension first: Chrome and Firefox cannot demux Matroska, and
// HEVC/AC-3 support is platform-dependent even inside MP4.
function plDirectPlayable(data) {
  const name = (PL.file || "").toLowerCase();
  const ext = name.includes(".") ? name.split(".").pop() : "";
  const codec = ((data && data.video) || {}).codec || "";
  const probe = document.createElement("video");

  if (["mp4", "m4v", "mov"].includes(ext)) {
    if (codec === "hevc") {
      return !!probe.canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"') ||
        !!probe.canPlayType('video/mp4; codecs="hev1.1.6.L93.B0"');
    }
    if (codec === "av1") return !!probe.canPlayType('video/mp4; codecs="av01.0.05M.08"');
    if (codec === "vp9") return !!probe.canPlayType('video/mp4; codecs="vp09.00.10.08"');
    return true; // h264 and friends
  }
  if (ext === "webm") return true;
  if (ext === "ogv" || ext === "ogg") return true;
  // Unknown extension: trust a non-Matroska container the probe found.
  const container = ((data && data.container) || "").toLowerCase();
  return !!container && !container.includes("matroska");
}

async function plStartSource(data) {
  if (plSourceStarting || plDead || plPrepared || plCompanion) return;
  plSourceStarting = true;
  try {
    if (data.copy_ready) {
      plSwitchToPrepared();
      return;
    }
    const wantsHls = data.hls_available && (!plDirectPlayable(data) || !!data.copy_reason);
    if (wantsHls) {
      const ok = await plStartHls(0);
      if (ok) return;
    }
    plPlayTries = 0;
    plVideo.src = plStreamUrl(false);
    plVideo.load();
    plTryPlay();
    plHandleAudio(data);
  } finally {
    plSourceStarting = false;
  }
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

function plDuration() {
  if (isFinite(plVideo.duration) && plVideo.duration > 0) return plVideo.duration;
  return plKnownDuration || 0;
}

function plUpdateTime() {
  plTime.textContent = plFmt(plVideo.currentTime) + " / " + plFmt(plDuration());
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
  } else if (!plStatusHeld() && !plPrepared) {
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
      srclang: t.code || "und",
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
  const t = (index >= 0 && plTracks[index]) ? plTracks[index] : null;
  plPostParent({
    action: "track",
    spec: t ? t.spec : "",
    label: t ? t.label : "",
    kind: t ? t.kind : "",
    file: t ? (t.file || "") : "",
  });
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
    plIdleStatus();
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
  plIdleStatus();
}

// Pick a subtitle track by its spec (used by the Subfind dialog, which sends
// ``external:<file>`` for a freshly downloaded subtitle). The video keeps
// playing; only the subtitle track changes.
async function plSelectBySpec(spec) {
  await plLoadTracks();
  const index = plTracks.findIndex((t) => t.spec === spec);
  if (index >= 0) await plChooseTrack(String(index));
  plTryPlay();
}

// Re-list the subtitle tracks (after a sync or delete changed the files on
// disk) and keep the current track when it still exists. The video keeps
// playing; only the track list and the captions refresh.
async function plReloadTracks() {
  const prev = plCurrent >= 0 ? plTracks[plCurrent] : null;
  const prevSpec = prev ? prev.spec : "";
  await plLoadTracks();
  if (prevSpec) {
    const index = plTracks.findIndex((t) => t.spec === prevSpec);
    if (index >= 0) {
      await plChooseTrack(String(index));
      return;
    }
  }
  const index = plTracks.findIndex((t) => t.default && t.playable);
  if (index >= 0) await plChooseTrack(String(index));
  else plApplyModes(-1);
}

async function plLoadTracks() {
  try {
    const res = await fetch("/api/player/tracks?path=" + plEnc(PL.path));
    const data = await res.json();
    if (!data || !data.ok) {
      plBuildTrackSelect([]);
      plSetStatus((data && data.error) || "Could not list the subtitle tracks.", "error");
      return null;
    }
    plBuildTrackSelect(data.tracks || []);
    plHlsAvailable = !!data.hls_available;
    plKnownDuration = Number(data.duration) || 0;
    return data;
  } catch (err) {
    plBuildTrackSelect([]);
    plSetStatus("Could not list the subtitle tracks: " + err, "error");
    return null;
  }
}

// ---- audio fallback (converted track / browser copy) -------------------------

// A file whose audio the browser cannot decode (a DTS or TrueHD rip) plays with
// picture and no sound, and fires no error at all. The server probes the first
// audio stream, so act on that here rather than leaving the viewer to wonder
// where the sound went.
function plIsAc3(audio) {
  const codec = ((audio || {}).codec || "").toLowerCase();
  return codec === "ac3" || codec === "eac3";
}

// AC-3 and E-AC-3 are decoded by the operating system on some browsers and not
// at all on others. When the browser says it cannot take either in MP4, make the
// copy up front instead of waiting for the four-second silence check, which some
// browsers cannot perform at all (they have no decoded-byte counter).
function plUnsupportedAc3(audio) {
  if (!plIsAc3(audio)) return false;
  const probe = document.createElement("video");
  return !probe.canPlayType('video/mp4; codecs="ac-3"')
    && !probe.canPlayType('video/mp4; codecs="ec-3"');
}

function plHandleAudio(data) {
  // HLS carries its own AAC audio, and a prepared copy is already complete.
  if (plDead || plPrepared || plCompanion || plHlsToken) return;
  if (data.audio) plHasAudio = Boolean(data.audio.codec);
  if (data.audio && !data.audio.codec) {
    plSetStatus("This file has no audio track.", "warn");
    return;
  }
  plCopyWhy = data.copy_reason || "";
  // A browser copy only exists because one was needed for this exact file, so
  // prefer it even when the codec looks decodable: whoever made it found that
  // it wasn't.
  if (data.copy_ready) {
    plSwitchToPrepared();
    return;
  }
  // A converted audio track exists: play the original (muted) video alongside
  // it right away.
  if (data.audio_ready) {
    plStartCompanion();
    return;
  }
  // Keep the button reachable for surround audio even when the browser claims
  // it can decode it: a silent chrome/firefox is not obvious until it happens,
  // and converting the audio is the way out.
  if (plIsAc3(data.audio)) plPrepare.hidden = false;
  if (plUnsupportedAc3(data.audio)) {
    plCopyWhy = "AC-3 audio";
    plPrepareAudio();
    return;
  }
  if (!data.needs_copy) return;
  plPrepare.hidden = false;
  plPrepareAudio();
}

// Why the audio was converted, as a lead-in for the status line; empty when
// the viewer asked for it themselves.
function plCopyNote() {
  return plCopyWhy ? plCopyWhy + " won't play in this browser. " : "";
}

function plPlayingCopy() {
  return plCopyNote() + "Playing the browser copy.";
}

function plPlayingCompanion() {
  return plCopyNote() + "Playing with converted audio.";
}

function plCopyProgress(message) {
  const tail = message || "Converting audio…";
  return plCopyNote() + tail;
}

// ---- companion audio --------------------------------------------------------
//
// The file's own audio cannot be decoded, so the server converted it to a
// small AAC track. The original video plays muted, and the converted track
// plays next to it; play/pause, seek, speed and drift correction keep the two
// aligned. This avoids the slow full remux (and its movie-sized copy).

function plStopCompanion() {
  if (!plCompanion) return;
  plCompanion = false;
  clearInterval(plSyncTimer);
  plSyncTimer = null;
  plVideo.muted = false;
  try { plAudio.pause(); } catch (err) { /* ignore */ }
  plAudio.removeAttribute("src");
}

function plAudioUrl() {
  return "/api/player/audio?path=" + plEnc(PL.path) + "&prepared=1";
}

function plStartCompanion() {
  if (plCompanion || plHlsToken || !plAudio) return;
  plCompanion = true;
  plPrepare.hidden = true;
  plPrepare.disabled = false;
  plVideo.muted = true;
  plAudio.src = plAudioUrl();
  plAudio.load();

  const sync = () => {
    if (plVideo.paused || plVideo.ended) {
      if (!plAudio.paused) plAudio.pause();
      return;
    }
    const attempt = plAudio.play();
    if (attempt && typeof attempt.catch === "function") attempt.catch(() => { /* ignore */ });
  };
  plVideo.addEventListener("play", sync);
  plVideo.addEventListener("pause", sync);
  plVideo.addEventListener("seeked", () => {
    try { plAudio.currentTime = plVideo.currentTime; } catch (err) { /* ignore */ }
    sync();
  });
  plVideo.addEventListener("ratechange", () => {
    try { plAudio.playbackRate = plVideo.playbackRate; } catch (err) { /* ignore */ }
  });
  plAudio.addEventListener("loadedmetadata", () => {
    try {
      if (plVideo.currentTime > 0.1) plAudio.currentTime = plVideo.currentTime;
      plAudio.playbackRate = plVideo.playbackRate;
    } catch (err) { /* ignore */ }
    sync();
  });
  // Two elements never stay sample-perfect; nudge the audio back onto the
  // video whenever they drift apart.
  clearInterval(plSyncTimer);
  plSyncTimer = setInterval(() => {
    if (!plCompanion || plVideo.paused || plAudio.paused) return;
    if (Math.abs(plVideo.currentTime - plAudio.currentTime) > 0.4) {
      try { plAudio.currentTime = plVideo.currentTime; } catch (err) { /* ignore */ }
    }
  }, 4000);

  plSetStatus(plPlayingCompanion(), plCopyWhy ? "warn" : "");
  sync();
  plTryPlay();
}

function plSwitchToPrepared() {
  const at = plVideo.currentTime || 0;
  plStopHls();
  plPrepared = true;
  plDead = false;
  plPrepare.hidden = true;
  // The full copy carries its own audio; drop the companion track.
  plStopCompanion();
  plSetStatus(plPlayingCopy(), plCopyWhy ? "warn" : "");
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
      plSetStatus(plCopyProgress(data.message), "warn");
      return;
    }
    clearInterval(plPreparePoll);
    plPrepare.disabled = false;
    if (data.status === "done") {
      if (data.kind === "copy" || plPrepareKind === "copy") plSwitchToPrepared();
      else plStartCompanion();
    } else if (data.status === "error") {
      plSetStatus(data.error || "The audio conversion failed.", "error");
    } else plIdleStatus();
  }, 1500);
}

async function plPrepareAudio() {
  if (plPrepare.disabled) return;
  plPrepare.disabled = true;
  plPrepareKind = "audio";
  plPrepare.textContent = "Fix audio";
  plSetStatus(plCopyProgress("Starting…"), "warn");
  try {
    const res = await fetch("/api/player/prepare?path=" + plEnc(PL.path) + "&kind=audio");
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      plSetStatus(data.error || "Could not start the audio conversion.", "error");
      plPrepare.disabled = false;
      return;
    }
    if (data.status === "done") {
      plStartCompanion();
      return;
    }
    plPollPrepare();
  } catch (err) {
    plSetStatus("Could not start the audio conversion: " + err, "error");
    plPrepare.disabled = false;
  }
}

async function plPrepareCopy() {
  if (plPrepare.disabled) return;
  plPrepare.disabled = true;
  plPrepareKind = "copy";
  plPrepare.textContent = "Make browser copy";
  plSetStatus(plCopyProgress("Starting…"), "warn");
  try {
    const res = await fetch("/api/player/prepare?path=" + plEnc(PL.path) + "&kind=copy");
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
  // The video itself will not play: only a full remux can fix that.
  plPrepareKind = "copy";
  plPrepare.textContent = "Make browser copy";
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
  if (plDead) return;
  if (plPrepared) {
    // The cached copy is unplayable too (a video codec this browser cannot
    // decode, or a damaged file): say so instead of freezing silently.
    plSetStatus(
      "This browser can't play the browser copy either — its video codec may " +
        "not be supported.",
      "error"
    );
    return;
  }
  if (plHlsToken) {
    // hls.js reports its own fatal errors and can recover from some of them;
    // only a native HLS element (Safari) leaves the error to us.
    if (!plHls) plHlsFatal("The Jellyfin stream could not be played.");
    return;
  }
  // A stream is being started; an error from the previous source is expected.
  if (plHlsStarting) return;
  const at = plVideo.currentTime || 0;
  if (plHlsAvailable) {
    plSetStatus(
      "This browser can't play the file directly — switching to the Jellyfin stream…",
      "warn"
    );
    if (await plStartHls(at)) return;
  }
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

  plSetStatus("Preparing playback…");

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

  // A container the browser can open but whose audio it cannot decode plays
  // silently, with no error at all. Chrome counts decoded audio bytes, so a
  // file with an audio track and nothing decoded is a silent one: convert the
  // audio without being asked. The server has already done this for the codecs
  // no browser can handle (DTS, TrueHD); this catches the rest — AC-3 and
  // E-AC-3 play on some platforms and are silent on others.
  plVideo.addEventListener("timeupdate", () => {
    if (plDead || plPrepared || plCompanion || plHlsToken || plAudioChecked || plCopyWhy) return;
    if (plHasAudio === false) return;
    if (plVideo.currentTime < 4) return;
    if (typeof plVideo.webkitAudioDecodedByteCount !== "number") return;
    if (plVideo.webkitAudioDecodedByteCount > 0) return;
    // Firefox can say whether the file has audio at all; Chrome cannot, and a
    // muted-by-missing-codec file is the case worth flagging.
    if (typeof plVideo.mozHasAudio === "boolean" && !plVideo.mozHasAudio) return;
    if (plVideo.audioTracks && plVideo.audioTracks.length === 0) return;
    plAudioChecked = true;
    plCopyWhy = "This file's audio";
    plPrepare.hidden = false;
    plPrepareAudio();
  });

  plVideo.addEventListener("stalled", () => {
    if (!plVideo.duration && !plHlsToken && !plSourceStarting) {
      plSuggestCopy("The browser stopped loading this file. Make a browser copy if it won't play.");
    }
  });

  // A file the browser silently refuses to decode never fires "error" on every
  // platform, so offer the copy after a while too.
  setTimeout(() => {
    if (!plDead && !plVideo.duration && !plHlsToken && !plSourceStarting) {
      plExplainFailure("Still loading… if this file won't play, make a browser copy.");
    }
  }, 12000);

  // Stop the Jellyfin transcode when the window closes or navigates elsewhere.
  window.addEventListener("pagehide", plStopHls);
  window.addEventListener("beforeunload", plStopHls);

  document.querySelectorAll("[data-skip]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const delta = Number(btn.dataset.skip) || 0;
      const duration = plDuration();
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
  plPrepare.addEventListener("click", () => {
    if (plPrepareKind === "copy") plPrepareCopy();
    else plPrepareAudio();
  });

  // The Subfind dialog drives this page from its iframe: select a track (a
  // freshly downloaded subtitle) or re-list the tracks (a sync/delete changed
  // the files on disk). "hello" lets the dialog re-sync its Now playing state.
  window.addEventListener("message", (ev) => {
    if (ev.source !== window.parent) return;
    const msg = (ev.data && typeof ev.data === "object") ? ev.data : {};
    if (!msg.jftools) return;
    if (msg.action === "hello") {
      const t = plCurrent >= 0 ? plTracks[plCurrent] : null;
      plPostParent({
        action: "ready",
        spec: t ? t.spec : "",
        label: t ? t.label : "",
        kind: t ? t.kind : "",
        file: t ? (t.file || "") : "",
      });
    } else if (msg.action === "select" && msg.spec) {
      plSelectBySpec(String(msg.spec));
    } else if (msg.action === "reload") {
      plReloadTracks();
    }
  });

  plUpdateTime();
  plLoadTracks().then((data) => {
    if (!data) {
      // The track list failed, but the file may still play: fall back to the
      // direct stream rather than leaving the player empty.
      plVideo.src = plStreamUrl(false);
      plVideo.load();
      plTryPlay();
      return;
    }
    // Pick direct play or Jellyfin HLS before touching the video element, so an
    // MKV never reaches a browser that would only download it.
    plStartSource(data);
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
  const duration = plDuration();
  plVideo.currentTime = duration ? Math.min(seconds, duration) : seconds;
  plIdleStatus();
}

plInit();
