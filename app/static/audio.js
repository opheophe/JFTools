"use strict";

// Tracks dialog (Search, Movie Match and Libraries).
//
// Opened from the row's "Tracks" pill. It lists the file's embedded audio and
// embedded subtitle streams plus the external subtitle files, lets several be
// marked for deletion, and lets one default of each type be chosen. Nothing
// changes until Execute: the server rewrites the video once with ffmpeg (every
// other stream copied as-is) and deletes the marked external files, so the
// dialog is gated on File management and asks for confirmation.
//
// The dialog reuses the subtitle dialog's shell classes (``subdl-*``) so the
// dialogs look and behave the same. Pages that show an Audio column assign
// ``window.audioRefresh``; it receives the freshly probed scan entry.

window.audioRefresh = null;

let audioPath = null;
let audioTracks = [];
let audioSubtitles = [];
let audioExtSubtitles = [];
let audioFileMgmt = false;
let audioBack = null;
let audioBusy = false;
let audioDeleteSet = new Set(); // stream indices (numbers) and "ext:<file>"
let audioDefault = null;
let audioDefaultSub = null;
let audioPreferred = [];
let audioSmartMark = [];
let audioSmartMarkFiles = [];
let audioDoneSound = null; // the Execute success chime, unlocked on click

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

function audioCurrentDefault(tracks) {
  const track = tracks.find((t) => t.default);
  return track ? track.index : null;
}

function audioFind(tracks, index) {
  return tracks.find((t) => t.index === index) || null;
}

function audioLabel(t, kind) {
  if (kind === "audio") {
    let label = t.lang || "Unknown";
    if (t.title && !t.commentary) label += " — " + t.title;
    return label;
  }
  let label = t.name || t.lang || "Unknown";
  if (t.forced && !/forced/i.test(label)) label += " (Forced)";
  return label;
}

function audioWouldLeaveNoAudio(track) {
  if (audioDeleteSet.has(track.index)) return false;
  return audioTracks.filter((t) => !audioDeleteSet.has(t.index)).length <= 1;
}

function audioFmtSize(n) {
  if (n === null || n === undefined) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(1) + " MB";
}

// The success.wav chime after Execute finishes. The element is unlocked with a
// muted play() inside the click gesture, so the real play after the (possibly
// long) rewrite is not blocked by the autoplay policy.
function audioUnlockSound() {
  try {
    if (!audioDoneSound) audioDoneSound = new Audio("/resources/success.wav");
    audioDoneSound.volume = 0.9;
    audioDoneSound.muted = true;
    audioDoneSound.play().catch(() => { /* ignore */ });
    audioDoneSound.pause();
    audioDoneSound.currentTime = 0;
    audioDoneSound.muted = false;
  } catch (err) {
    /* ignore */
  }
}

function audioPlayDone() {
  try {
    if (!audioDoneSound) audioDoneSound = new Audio("/resources/success.wav");
    audioDoneSound.muted = false;
    audioDoneSound.currentTime = 0;
    audioDoneSound.play().catch(() => { /* a blocked autoplay is not worth an error */ });
  } catch (err) {
    /* ignore */
  }
}

// An external subtitle file row: only Mark delete/Unmark. External files have
// no default bit, and Execute deletes them from disk without a rewrite.
function audioExtRow(e) {
  const key = "ext:" + e.file;
  const marked = audioDeleteSet.has(key);
  const row = el("div", { class: "subdl-subrow" });
  row.appendChild(el("span", { class: "subdl-tag", text: "external" }));
  let label = e.lang || "Unknown";
  if (e.forced) label += " (Forced)";
  if (e.ai) label += " (AI)";
  row.appendChild(el("span", { class: "subdl-c-lang", text: label }));
  row.appendChild(el("span", { class: "subdl-name", text: e.file, title: e.file }));
  row.appendChild(el("span", { class: "muted", text: audioFmtSize(e.size) }));
  const mark = el("button", {
    type: "button",
    class: "subdl-mini subdl-mark" + (marked ? " subdl-mark-on" : ""),
    text: marked ? "Unmark" : "Mark delete",
  });
  if (!audioFileMgmt || audioBusy) {
    mark.disabled = true;
    mark.title = !audioFileMgmt ? "File management is off" : "A rewrite is running";
  } else {
    mark.addEventListener("click", () => {
      if (marked) audioDeleteSet.delete(key);
      else audioDeleteSet.add(key);
      audioRender();
    });
  }
  row.appendChild(mark);
  return row;
}

function audioTrackRow(track, kind) {
  if (kind === "ext") return audioExtRow(track);
  const index = track.index;
  const marked = audioDeleteSet.has(index);
  const pending = kind === "audio" ? audioDefault === index : audioDefaultSub === index;
  const isDefault = !!track.default;
  const row = el("div", { class: "subdl-subrow" });

  row.appendChild(el("span", {
    class: "subdl-tag",
    text: isDefault ? "default" : (kind === "audio" ? "audio" : "embedded"),
  }));

  const label = audioLabel(track, kind);
  row.appendChild(el("span", { class: "subdl-name", text: label, title: label }));

  if (kind === "audio" && track.commentary) {
    row.appendChild(el("span", { class: "subdl-badge", text: "commentary" }));
  }
  if (kind === "subtitle" && track.forced) {
    row.appendChild(el("span", { class: "subdl-badge", text: "forced" }));
  }
  row.appendChild(el("span", {
    class: "muted",
    text: kind === "audio" ? audioTech(track) : (track.codec || ""),
  }));

  const def = el("button", {
    type: "button",
    class: "subdl-mini subdl-default" + (pending ? " subdl-default-on" : ""),
    text: pending ? "Default ✓" : "Set default",
  });
  if (!audioFileMgmt || audioBusy || marked || isDefault) {
    def.disabled = true;
    def.title = !audioFileMgmt
      ? "File management is off"
      : (audioBusy ? "A rewrite is running"
        : (isDefault ? "Already the default" : "Marked for deletion"));
  } else {
    def.addEventListener("click", () => {
      if (kind === "audio") audioDefault = index;
      else audioDefaultSub = index;
      // The two controls are mutually exclusive for one row.
      audioDeleteSet.delete(index);
      audioRender();
    });
  }
  row.appendChild(def);

  const mark = el("button", {
    type: "button",
    class: "subdl-mini subdl-mark" + (marked ? " subdl-mark-on" : ""),
    text: marked ? "Unmark" : "Mark delete",
  });
  if (!audioFileMgmt || audioBusy || (kind === "audio" && audioWouldLeaveNoAudio(track))) {
    mark.disabled = true;
    mark.title = !audioFileMgmt
      ? "File management is off"
      : (audioBusy ? "A rewrite is running"
        : "At least one audio track must remain");
  } else {
    mark.addEventListener("click", () => {
      if (marked) {
        audioDeleteSet.delete(index);
      } else {
        audioDeleteSet.add(index);
        if (kind === "audio" && audioDefault === index) audioDefault = null;
        if (kind === "subtitle" && audioDefaultSub === index) audioDefaultSub = null;
      }
      audioRender();
    });
  }
  row.appendChild(mark);
  return row;
}

function audioRenderSection(box, title, tracks, kind, emptyText) {
  box.appendChild(el("h4", { text: title }));
  if (!tracks.length) {
    box.appendChild(el("p", { class: "muted", text: emptyText }));
    return;
  }
  tracks.forEach((t) => box.appendChild(audioTrackRow(t, kind)));
}

function audioDefaultChange(kind) {
  const tracks = kind === "audio" ? audioTracks : audioSubtitles;
  const pending = kind === "audio" ? audioDefault : audioDefaultSub;
  if (pending === null) return null;
  return pending !== audioCurrentDefault(tracks) ? pending : null;
}

function audioHasChanges() {
  return audioDeleteSet.size > 0
    || audioDefaultChange("audio") !== null
    || audioDefaultChange("subtitle") !== null;
}

function audioSmartMarkTracks() {
  if (!audioFileMgmt || audioBusy) return;
  if (!audioPreferred.length) {
    audioNotice("Set your preferred subtitle languages on the Settings page first.", "error");
    return;
  }
  // Smart mark replaces subtitle marks only; audio marks are the user's own.
  audioSubtitles.forEach((t) => audioDeleteSet.delete(t.index));
  audioExtSubtitles.forEach((e) => audioDeleteSet.delete("ext:" + e.file));
  audioSmartMark.forEach((index) => audioDeleteSet.add(index));
  audioSmartMarkFiles.forEach((file) => audioDeleteSet.add("ext:" + file));
  if (audioDefaultSub !== null && audioSmartMark.includes(audioDefaultSub)) {
    audioDefaultSub = null;
  }
  const count = audioSmartMark.length + audioSmartMarkFiles.length;
  const languages = audioPreferred.join(", ");
  if (count) {
    audioNotice(
      "Marked " + count + " subtitle track" + (count === 1 ? "" : "s") +
      " (non-preferred or forced; preferred: " + languages + ").",
      "ok",
    );
  } else {
    audioNotice("No subtitle tracks to mark (preferred: " + languages + ").", "muted");
  }
  audioRender();
}

function audioRender() {
  const box = document.getElementById("audio-tracks");
  const footer = document.getElementById("audio-actions");
  if (!box) return;
  box.innerHTML = "";
  if (footer) footer.innerHTML = "";

  audioRenderSection(box, "Audio tracks", audioTracks, "audio",
    "No audio tracks on this file.");
  audioRenderSection(box, "Embedded subtitle tracks", audioSubtitles, "subtitle",
    "No embedded subtitle tracks on this file.");
  audioRenderSection(box, "External subtitle files", audioExtSubtitles, "ext",
    "No external subtitle files next to this video.");

  const actions = el("div", { class: "subdl-actions" });
  actions.appendChild(el("p", {
    class: "muted subdl-help",
    text: "One default per type; choosing a new one replaces the current default.",
  }));
  const smart = el("button", {
    type: "button",
    class: "subdl-btn subdl-smart",
    text: "Smart mark",
  });
  smart.disabled = !audioFileMgmt || audioBusy || !audioPreferred.length
    || (!audioSubtitles.length && !audioExtSubtitles.length);
  if (smart.disabled) {
    smart.title = !audioFileMgmt
      ? "File management is off"
      : (audioBusy ? "A rewrite is running"
        : (!audioPreferred.length
          ? "Set preferred subtitle languages on the Settings page first"
          : "No subtitles to mark"));
  } else {
    smart.addEventListener("click", audioSmartMarkTracks);
  }
  actions.appendChild(smart);
  const exec = el("button", { type: "button", class: "subdl-btn subdl-exec", text: "Execute" });
  exec.disabled = !audioFileMgmt || audioBusy || !audioHasChanges();
  if (exec.disabled) {
    exec.title = !audioFileMgmt
      ? "File management is off"
      : (audioBusy ? "A rewrite is running" : "Mark a track or choose a default first");
  } else {
    exec.addEventListener("click", audioExecute);
  }
  actions.appendChild(exec);
  // Keep Execute visible while the track list scrolls.
  (footer || box).appendChild(actions);
}

async function audioLoad() {
  audioTracks = [];
  audioSubtitles = [];
  audioExtSubtitles = [];
  audioDeleteSet = new Set();
  audioDefault = null;
  audioDefaultSub = null;
  audioPreferred = [];
  audioSmartMark = [];
  audioSmartMarkFiles = [];
  try {
    const data = await audioPost("/api/internal/existing", "path=" + encodeURIComponent(audioPath));
    if (!data || !data.ok) {
      audioNotice((data && data.error) || "Could not read the tracks.", "error");
      audioRender();
      return;
    }
    audioFileMgmt = !!data.file_mgmt;
    audioTracks = (data.audio || []).slice();
    audioSubtitles = (data.subtitles || []).slice();
    audioExtSubtitles = (data.external || []).slice();
    audioPreferred = (data.preferred_languages || []).slice();
    audioSmartMark = (data.smart_mark || []).slice();
    audioSmartMarkFiles = (data.smart_mark_files || []).slice();
    if (data.error) audioNotice("Probe warning: " + data.error, "error");
    const warn = document.getElementById("audio-fm");
    if (warn) warn.hidden = audioFileMgmt;
    audioRender();
  } catch (err) {
    audioNotice(String(err), "error");
    audioRender();
  }
}

function audioExecuteSummary() {
  const bits = [];
  const tracks = [...audioDeleteSet].filter((v) => typeof v === "number");
  const files = [...audioDeleteSet].filter((v) => typeof v === "string" && v.indexOf("ext:") === 0);
  if (tracks.length) {
    bits.push("delete " + tracks.length + " track" + (tracks.length === 1 ? "" : "s"));
  }
  if (files.length) {
    bits.push("delete " + files.length + " subtitle file" + (files.length === 1 ? "" : "s"));
  }
  const defA = audioDefaultChange("audio");
  if (defA !== null) {
    const track = audioFind(audioTracks, defA);
    bits.push("set default audio to " + (track ? audioLabel(track, "audio") : "track " + defA));
  }
  const defS = audioDefaultChange("subtitle");
  if (defS !== null) {
    const track = audioFind(audioSubtitles, defS);
    bits.push("set default subtitle to " + (track ? audioLabel(track, "subtitle") : "track " + defS));
  }
  return bits;
}

async function audioExecute() {
  if (!audioFileMgmt || audioBusy) return;
  const path = audioPath;
  const deleted = [...audioDeleteSet].filter((v) => typeof v === "number");
  const deletedFiles = [...audioDeleteSet]
    .filter((v) => typeof v === "string" && v.indexOf("ext:") === 0)
    .map((v) => v.slice(4));
  const defA = audioDefaultChange("audio");
  const defS = audioDefaultChange("subtitle");
  const summary = audioExecuteSummary();
  if (!summary.length) return;

  const rewrites = deleted.length > 0 || defA !== null || defS !== null;
  if (!window.confirm(
    "Apply these changes to " + path.replace(/^.*[\\/]/, "") + "?\n\n" +
    summary.join("\n") + "\n\n" +
    (rewrites
      ? "The video is rewritten in place and needs free space equal to its size."
      : "The marked subtitle files are deleted from disk.")
  )) return;

  audioBusy = true;
  audioUnlockSound();
  audioNotice("Executing — this can take a while…", "muted");
  audioRender();
  let body = "path=" + encodeURIComponent(path);
  if (deleted.length) body += "&delete=" + encodeURIComponent(deleted.join(","));
  if (deletedFiles.length) body += "&delete_files=" + encodeURIComponent(deletedFiles.join(","));
  if (defA !== null) body += "&default_audio=" + encodeURIComponent(defA);
  if (defS !== null) body += "&default_subtitle=" + encodeURIComponent(defS);
  try {
    const data = await audioPost("/api/internal/execute", body);
    if (!data || !data.ok) {
      if (audioPath === path) {
        audioBusy = false;
        audioNotice((data && data.error) || "Track edit failed.", "error");
        audioRender();
      }
      return;
    }
    if (path && window.audioRefresh && data.entry) window.audioRefresh(path, data.entry);
    if (audioPath !== path) return;
    audioBusy = false;
    audioDeleteSet = new Set();
    audioDefault = null;
    audioDefaultSub = null;
    audioTracks = ((data.entry && data.entry.audio) || []).slice();
    audioSubtitles = ((data.entry && data.entry.internal) || []).slice();
    audioExtSubtitles = ((data.entry && data.entry.external) || []).slice();
    if (Array.isArray(data.preferred_languages)) {
      audioPreferred = data.preferred_languages.slice();
    }
    audioSmartMark = (data.smart_mark || []).slice();
    audioSmartMarkFiles = (data.smart_mark_files || []).slice();
    let msg = data.message || "Updated.";
    if (data.jellyfin) msg += " (Jellyfin warning: " + data.jellyfin + ")";
    audioNotice(msg, "ok");
    audioPlayDone();
    audioRender();
  } catch (err) {
    if (audioPath === path) {
      audioBusy = false;
      audioNotice(String(err), "error");
      audioRender();
    }
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
  audioSubtitles = [];
  audioExtSubtitles = [];
  audioDeleteSet = new Set();
  audioDefault = null;
  audioDefaultSub = null;
  audioPreferred = [];
  audioSmartMark = [];
  audioSmartMarkFiles = [];
  audioBusy = false;
}

function audioOnKey(e) {
  if (e.key === "Escape") audioClose();
}

function audioOpen(path) {
  if (!path) return;
  audioClose();
  audioPath = path;
  audioTracks = [];
  audioSubtitles = [];
  audioExtSubtitles = [];

  const back = el("div", { class: "subdl-back" });
  const box = el("div", { class: "subdl" });
  box.appendChild(el("h3", { text: "Tracks" }));
  box.appendChild(el("p", { class: "muted", text: path.replace(/^.*[\\/]/, "") }));
  const warn = el("p", {
    class: "muted subdl-fm",
    hidden: true,
    text: "File management is off — editing tracks is disabled. Enable it on the Settings page.",
  });
  warn.id = "audio-fm";
  box.appendChild(warn);
  box.appendChild(el("p", {
    class: "muted subdl-help",
    text: "Mark tracks or subtitle files to delete, choose at most one default per type, then press Execute.",
  }));
  box.appendChild(el("div", { id: "audio-status", class: "subdl-status" }));
  box.appendChild(el("div", { id: "audio-tracks", class: "audio-scroll" }));
  box.appendChild(el("div", { id: "audio-actions" }));

  back.appendChild(box);
  document.body.appendChild(back);
  back.addEventListener("mousedown", (e) => { if (e.target === back) audioClose(); });
  document.addEventListener("keydown", audioOnKey);
  audioBack = back;

  audioLoad();
}
