"use strict";

// Formats tab.
//
// Lists the Movie Match rows with the compact format blob Jellyfin supplied
// (container, video codec/profile/size/depth/HDR type, audio codecs). Two
// verdicts are computed against the selected *device profile*:
//
//   V  Direct · Transcode · Transcode 4K · No video · No stream info · Unknown
//   A  Direct · → AAC · Some tracks · No audio · No stream info · Unknown
//
// "Will it play?" (audio) and "what will it cost?" (video) stay separate: an
// Opus track can abort playback while AV1 merely forces a software transcode.
//
// Jellyfin reports an empty stream list for files it has not probed. That is
// "No stream info", not "No video": the file may be perfectly playable, so the
// tab offers a Probe action that reads the file with ffprobe instead.
//
// The Corrupt filter is about broken files, not unsupported ones: it reads the
// Search media scan's probe error/duration and Jellyfin's own "no media
// source"/"no streams" gaps, and badges each affected row.
//
// Suggested searches are chips with live counts, so finding "the other Box
// Rooms" is one click rather than a filter-building exercise.

const FO = {
  rows: [], jellyfin: "", radarr: "", sonarr: "",
  fetched: false, page: 1, sort: { col: "playback", dir: -1 },
  filters: {
    lib: [], type: [], container: [], vcodec: [], depth: [], hdr: [],
    acodec: [], channels: [], playback: [], corrupt: [], file: "", jf: "", suggest: "",
  },
  ms: {},
  profiles: {}, builtin: [], saved: [], device: "", profile: {},
  combos: [],
};

const FO_LS_KEY = "jftools.formats.filters";
const FO_SORT_KEY = "jftools.formats.sort";
const FO_DEVICE_KEY = "jftools.formats.device";
const FO_PAGE_SIZE = 50;

// Codecs that no modern device decodes; grouped so they can be found with one
// chip instead of a seven-value multiselect.
const FO_LEGACY = new Set([
  "mpeg4", "mpeg2video", "msmpeg4v3", "msmpeg4v1", "mpeg1video", "wmv3", "vc1", "rv40",
]);

const FO_PLAYBACK_OPTIONS = [
  { section: "Video" },
  { value: "Video direct", label: "Video direct" },
  { value: "Video transcode", label: "Video transcode" },
  { value: "Video transcode 4K", label: "Video transcode 4K" },
  { value: "No video", label: "No video stream" },
  { value: "No stream info", label: "No stream info" },
  { section: "Audio" },
  { value: "Audio direct", label: "Audio direct" },
  { value: "Audio → AAC", label: "Audio → AAC" },
  { value: "Some audio tracks", label: "Some audio tracks" },
  { value: "No audio", label: "No audio stream" },
  { section: "Overall" },
  { value: "Remux only", label: "Remux only" },
  { value: "Won't direct-play", label: "Won't direct-play" },
  { value: "Unknown", label: "Unknown format" },
];

// Files that will not play because something is wrong with the file, as opposed
// to a device simply not decoding it. "Any corruption" is the shortcut for the
// broken-file options: it leaves out "Not checked" and the rows Playback
// already calls "No video"/"No audio".
const FO_CORRUPT_OPTIONS = [
  { section: "Overall" },
  { value: "Any corruption", label: "Any corruption" },
  { section: "File" },
  { value: "Unreadable", label: "Unreadable" },
  { value: "Missing file", label: "Missing file" },
  { value: "Empty file", label: "Empty file" },
  { value: "No duration", label: "No duration" },
  { value: "Not checked", label: "Not checked yet" },
  { section: "Jellyfin" },
  { value: "No media info", label: "No media info" },
  { value: "No streams", label: "No streams" },
  { value: "No video stream", label: "No video stream" },
  { value: "No audio stream", label: "No audio stream" },
  { value: "Video without size", label: "Video without size" },
  { value: "Unknown codec", label: "Unknown codec" },
];

const FO_CORRUPT_ANY = new Set([
  "Unreadable", "Missing file", "Empty file", "No duration",
  "No media info", "No streams", "Video without size", "Unknown codec",
]);

// Already visible in the V/A badges, so they are filterable but not badged.
const FO_CORRUPT_QUIET = new Set(["No video stream", "No audio stream"]);

const FO_CORRUPT_TITLE = {
  "Unreadable": "The media scan could not read this file.",
  "Missing file": "The file is not reachable on this machine.",
  "Empty file": "The file is 0 bytes.",
  "No duration": "The file opens but reports no runtime.",
  "No media info": "Jellyfin returned no media source for this file.",
  "No streams": "Jellyfin reports neither a video nor an audio stream.",
  "No video stream": "Jellyfin reports no video stream.",
  "No audio stream": "Jellyfin reports no audio stream.",
  "Video without size": "The video stream reports no width or height.",
  "Unknown codec": "Jellyfin reports an empty or unknown video codec.",
};

// ---- format helpers --------------------------------------------------------

function foFmt(r) { return r.fmt || null; }
function foVideoStream(r) { const f = foFmt(r); return (f && f.v) || null; }
function foAudioStreams(r) { const f = foFmt(r); return (f && f.a) || []; }
// Jellyfin knows the file exists but has no stream list for it: the verdict is
// unknown, not "No video". A separate Probe action reads it with ffprobe.
function foNoInfo(r) {
  const f = foFmt(r);
  return !!f && !f.v && !(f.a || []).length;
}
function foNeedsProbe(r) {
  const f = foFmt(r);
  return !f || foNoInfo(r);
}

// The compact media-scan summary formats.py attaches to each row, or null when
// the Search media scan has not touched the path:
// {e: probe error, s: scanned, d: duration seconds, z: size bytes}.
function foScan(r) { return r.scan || null; }

function foNotChecked(r) {
  const scan = foScan(r);
  return !scan || (!scan.s && !scan.e);
}

// Everything that points at a broken file rather than an unsupported one. The
// labels double as the Corrupt filter values.
function foCorruptReasons(r) {
  const out = [];
  const scan = foScan(r);
  const err = (scan && scan.e) || "";
  if (err) {
    out.push(err === "unreachable" || err === "not a file" ? "Missing file" : "Unreadable");
  }
  if (scan && scan.z !== null && scan.z !== undefined && Number(scan.z) === 0) {
    out.push("Empty file");
  }
  if (scan && scan.s && !err && !(Number(scan.d) > 0) && !out.includes("Empty file")) {
    out.push("No duration");
  }
  const f = foFmt(r);
  if (!f) {
    // An older cache has no ``fmt`` and no mark either: that is unknown, not
    // "Jellyfin could not read it".
    if (r.nosrc) out.push("No media info");
    return out;
  }
  const audio = f.a || [];
  if (!f.v && !audio.length) {
    out.push("No streams");
  } else {
    if (!f.v) out.push("No video stream");
    if (!audio.length) out.push("No audio stream");
    if (f.v) {
      if (!(Number(f.v.w) > 0 && Number(f.v.h) > 0)) out.push("Video without size");
      const codec = String(f.v.c || "").toLowerCase();
      if (!codec || codec === "unknown") out.push("Unknown codec");
    }
  }
  return out;
}

function foAnyCorrupt(r) {
  return foCorruptReasons(r).some((reason) => FO_CORRUPT_ANY.has(reason));
}

function foCorruptTitle(reason, r) {
  let title = FO_CORRUPT_TITLE[reason] || reason;
  const scan = foScan(r);
  if (reason === "Unreadable" && scan && scan.e) title += " " + scan.e;
  return title;
}

function foCodec(r) { const v = foVideoStream(r); return (v && v.c) || ""; }
function foHeight(r) { const v = foVideoStream(r); return (v && v.h) || 0; }
function foDepth(r) { const v = foVideoStream(r); return (v && v.d) || 0; }
function foRange(r) { const v = foVideoStream(r); return (v && v.r) || ""; }
function foAudioCodecs(r) {
  return [...new Set(foAudioStreams(r).map((t) => (t.c || "").toLowerCase()).filter(Boolean))];
}

function foChannels(n) {
  n = Number(n) || 0;
  if (n === 1) return "1.0";
  if (n === 2) return "2.0";
  if (n === 6) return "5.1";
  if (n === 8) return "7.1";
  return n ? n + "ch" : "";
}

function foVideoText(v) {
  if (!v) return "";
  const bits = [];
  if (v.c) bits.push(v.c);
  if (v.w && v.h) bits.push(v.w + "×" + v.h);
  if (v.d) bits.push(v.d + "-bit");
  if (v.r) bits.push(v.r);
  return bits.join(" · ");
}

function foAudioText(r) {
  const tracks = foAudioStreams(r);
  if (!tracks.length) return "";
  return tracks.map((t) => {
    const ch = foChannels(t.ch);
    return [t.c || "?", ch].filter(Boolean).join(" ");
  }).join(", ");
}

// ---- verdicts --------------------------------------------------------------

// A missing video stream is worse than an unsupported one: there is nothing to
// transcode. Rank orders the table (higher = worse) and drives the sort.
function foVideo(r) {
  const f = foFmt(r);
  if (!f) {
    return { key: "unknown", rank: 4, label: "Unknown",
      title: "No format data for this row — click Fetch." };
  }
  const v = f.v;
  if (!v) {
    if (foNoInfo(r)) {
      return { key: "noinfo", rank: 5, label: "No stream info",
        title: "Jellyfin has no stream list for this file — click Probe missing to read it." };
    }
    return { key: "none", rank: 3, label: "No video",
      title: "Jellyfin reports no video stream for this file." };
  }
  const allowed = (FO.profile && FO.profile.video) || {};
  const max = allowed[v.c];
  const detail = foVideoText(v) || "video";
  const tooDeep = max !== undefined && v.d && v.d > max;
  const bad = max === undefined || tooDeep;
  if (!bad) {
    return { key: "direct", rank: 0, label: "Direct",
      title: detail + " — this profile decodes it directly." };
  }
  const why = max === undefined
    ? v.c + " is not decoded by this profile"
    : v.d + "-bit exceeds the profile's " + max + "-bit limit";
  if ((v.h || 0) > 1080) {
    return { key: "transcode4k", rank: 2, label: "Transcode 4K",
      title: detail + " — " + why + "; a 4K transcode is expensive and may stutter." };
  }
  return { key: "transcode", rank: 1, label: "Transcode",
    title: detail + " — " + why + "; software transcode." };
}

function foAudio(r) {
  const f = foFmt(r);
  if (!f) {
    return { key: "unknown", rank: 4, label: "Unknown",
      title: "No format data for this row — click Fetch." };
  }
  const tracks = f.a || [];
  if (!tracks.length) {
    if (foNoInfo(r)) {
      return { key: "noinfo", rank: 5, label: "No stream info",
        title: "Jellyfin has no stream list for this file — click Probe missing to read it." };
    }
    return { key: "none", rank: 3, label: "No audio",
      title: "Jellyfin reports no audio stream for this file." };
  }
  const allowed = (FO.profile && FO.profile.audio) || [];
  const ok = tracks.filter((t) => allowed.includes((t.c || "").toLowerCase()));
  const detail = foAudioText(r) || "audio";
  if (ok.length === tracks.length) {
    return { key: "direct", rank: 0, label: "Direct",
      title: detail + " — every track is supported." };
  }
  if (ok.length === 0) {
    return { key: "aac", rank: 2, label: "→ AAC",
      title: detail + " — no track is supported; convert the audio to AAC." };
  }
  return { key: "some", rank: 1, label: "Some tracks",
    title: detail + " — " + ok.length + " of " + tracks.length +
      " tracks are supported; the rest need AAC." };
}

function foRemux(r) {
  const f = foFmt(r);
  if (!f || !f.c) return false;
  const containers = (FO.profile && FO.profile.containers) || [];
  return !containers.includes(f.c);
}

function foFullyDirect(r) {
  if (!foFmt(r)) return false;
  return foVideo(r).key === "direct" && foAudio(r).key === "direct" && !foRemux(r);
}

function foPlaybackRank(r) {
  if (!foFmt(r)) return 99;
  return foVideo(r).rank * 10 + foAudio(r).rank + (foRemux(r) ? 1 : 0);
}

function foPlaybackMatch(r, value) {
  const v = foVideo(r);
  const a = foAudio(r);
  switch (value) {
    case "Video direct": return v.key === "direct";
    case "Video transcode": return v.key === "transcode";
    case "Video transcode 4K": return v.key === "transcode4k";
    case "No video": return v.key === "none";
    case "No stream info": return v.key === "noinfo";
    case "Audio direct": return a.key === "direct";
    case "Audio → AAC": return a.key === "aac";
    case "Some audio tracks": return a.key === "some";
    case "No audio": return a.key === "none";
    case "Remux only": return foRemux(r);
    case "Won't direct-play": return !!foFmt(r) && !foNoInfo(r) && !foFullyDirect(r);
    case "Unknown": return !foFmt(r);
    default: return false;
  }
}

// ---- suggestions -----------------------------------------------------------

const FO_SUGGEST = {
  av1: (r) => foCodec(r) === "av1",
  av1_4k: (r) => foCodec(r) === "av1" && foHeight(r) > 1080,
  h264_10: (r) => foCodec(r) === "h264" && foDepth(r) > 8,
  dovi: (r) => foRange(r).startsWith("DOVI"),
  legacy: (r) => FO_LEGACY.has(foCodec(r)),
  opus: (r) => foAudioCodecs(r).includes("opus"),
  dts_truehd: (r) => foAudioCodecs(r).some((c) => c === "dts" || c === "truehd"),
  av1_opus: (r) => foCodec(r) === "av1" && foAudioCodecs(r).includes("opus"),
  wont: (r) => !!foFmt(r) && !foNoInfo(r) && !foFullyDirect(r),
  direct: (r) => foFullyDirect(r),
  novideo: (r) => !!foFmt(r) && !foVideoStream(r) && foAudioStreams(r).length > 0,
  noinfo: (r) => foNoInfo(r),
  needsprobe: (r) => foNeedsProbe(r),
  corrupt: (r) => foAnyCorrupt(r),
};

function foComboKey(r) {
  const f = foFmt(r);
  if (!f) return "";
  return [f.c || "?", (f.v && f.v.c) || "(none)", foAudioCodecs(r).join(",") || "(none)"].join("|");
}

function foSuggestMatch(r) {
  const s = FO.filters.suggest;
  if (!s) return true;
  if (s.startsWith("combo:")) return foComboKey(r) === s.slice(6);
  const fn = FO_SUGGEST[s];
  return fn ? fn(r) : true;
}

// ---- filtering -------------------------------------------------------------

function foSafeRe(text) {
  if (!text || !text.trim()) return null;
  try { return new RegExp(text, "i"); } catch (e) { return null; }
}

function foVisible() {
  const f = FO.filters;
  const fileRe = foSafeRe(f.file);
  const jfRe = foSafeRe(f.jf);
  return FO.rows.filter((r) => {
    if (f.lib.length && !f.lib.includes(r.library)) return false;
    if (f.type.length && !f.type.includes(r.type)) return false;
    const fmt = foFmt(r);
    if (f.container.length && (!fmt || !f.container.includes(fmt.c))) return false;
    if (f.vcodec.length) {
      const v = foVideoStream(r);
      if (!v || !f.vcodec.includes(v.c || "")) return false;
    }
    if (f.depth.length) {
      const d = foDepth(r);
      const label = d ? d + "-bit" : "Unknown";
      if (!f.depth.includes(label)) return false;
    }
    if (f.hdr.length) {
      const label = foRange(r) || "Unknown";
      if (!f.hdr.includes(label)) return false;
    }
    if (f.acodec.length) {
      const codecs = foAudioCodecs(r);
      if (!codecs.some((c) => f.acodec.includes(c))) return false;
    }
    if (f.channels.length) {
      const labels = foAudioStreams(r).map((t) => foChannels(t.ch)).filter(Boolean);
      if (!labels.some((c) => f.channels.includes(c))) return false;
    }
    if (f.playback.length && !f.playback.some((v) => foPlaybackMatch(r, v))) return false;
    if (f.corrupt.length) {
      const reasons = foCorruptReasons(r);
      const ok = f.corrupt.some((value) => {
        if (value === "Any corruption") return reasons.some((x) => FO_CORRUPT_ANY.has(x));
        if (value === "Not checked") return foNotChecked(r);
        return reasons.includes(value);
      });
      if (!ok) return false;
    }
    if (!foSuggestMatch(r)) return false;
    if (fileRe && !fileRe.test(r.file)) return false;
    if (jfRe && !jfRe.test(r.display || r.name)) return false;
    return true;
  });
}

// ---- persistence -----------------------------------------------------------

function foSaveFilters() {
  try { localStorage.setItem(FO_LS_KEY, JSON.stringify(FO.filters)); } catch (e) {}
}

function foLoadFilters() {
  try {
    const s = localStorage.getItem(FO_LS_KEY);
    if (!s) return;
    const f = JSON.parse(s);
    if (!f || typeof f !== "object") return;
    ["lib", "type", "container", "vcodec", "depth", "hdr", "acodec", "channels", "playback", "corrupt"]
      .forEach((k) => { if (Array.isArray(f[k])) FO.filters[k] = f[k]; });
    if (typeof f.file === "string") FO.filters.file = f.file;
    if (typeof f.jf === "string") FO.filters.jf = f.jf;
    if (typeof f.suggest === "string") FO.filters.suggest = f.suggest;
  } catch (e) {}
}

function foSaveSort() {
  try { localStorage.setItem(FO_SORT_KEY, JSON.stringify(FO.sort)); } catch (e) {}
}

function foLoadSort() {
  try {
    const s = localStorage.getItem(FO_SORT_KEY);
    if (!s) return;
    const o = JSON.parse(s);
    if (o && typeof o === "object" && SORTABLE[o.col]) {
      FO.sort = { col: o.col, dir: o.dir === -1 ? -1 : 1 };
    }
  } catch (e) {}
}

// ---- sorting ---------------------------------------------------------------

const SORTABLE = {
  type: (r) => r.type || "",
  path: (r) => r.path || "",
  file: (r) => r.file || "",
  display: (r) => r.display || r.name || "",
  container: (r) => { const f = foFmt(r); return (f && f.c) || ""; },
  video: (r) => foVideoText(foVideoStream(r)),
  audio: (r) => foAudioText(r),
  playback: (r) => foPlaybackRank(r),
};

function foCompareRows(a, b, col, dir) {
  const get = SORTABLE[col];
  if (!get) return 0;
  const va = get(a);
  const vb = get(b);
  const aEmpty = va === null || va === undefined || va === "";
  const bEmpty = vb === null || vb === undefined || vb === "";
  if (aEmpty && bEmpty) return 0;
  if (aEmpty) return 1;  // empties always sort last
  if (bEmpty) return -1;
  if (typeof va === "number" && typeof vb === "number") return (va - vb) * dir;
  return String(va).localeCompare(String(vb), undefined, { sensitivity: "base" }) * dir;
}

function foApplySort(list) {
  if (!FO.sort.col) return list;
  return list.slice().sort((a, b) => foCompareRows(a, b, FO.sort.col, FO.sort.dir));
}

function foUpdateSortIndicators() {
  document.querySelectorAll("#fo-table th[data-sort]").forEach((th) => {
    const arrow = th.querySelector(".sort-arrow");
    if (arrow) {
      arrow.textContent = th.dataset.sort === FO.sort.col ? (FO.sort.dir === 1 ? "▲" : "▼") : "";
    }
  });
}

function foSetSort(col) {
  if (FO.sort.col === col) FO.sort.dir = -FO.sort.dir;
  else FO.sort = { col: col, dir: 1 };
  FO.page = 1;
  foSaveSort();
  renderFoTable();
}

function foBindSortHeaders() {
  document.querySelectorAll("#fo-table th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => foSetSort(th.dataset.sort));
  });
}

// ---- rendering -------------------------------------------------------------

function foBadge(cls, text, title) {
  return el("span", { class: "fmt-badge " + cls, text: text, title: title });
}

function foPlaybackCell(r) {
  const td = el("td", { class: "fmt-playback" });
  const v = foVideo(r);
  const a = foAudio(r);
  td.appendChild(foBadge("fmt-v-" + v.key, "V " + v.label, v.title));
  td.appendChild(foBadge("fmt-a-" + a.key, "A " + a.label, a.title));
  if (foRemux(r)) {
    const f = foFmt(r);
    td.appendChild(foBadge("fmt-remux", "Remux",
      "Container " + ((f && f.c) || "?") + " is not in the profile; remux only."));
  }
  foCorruptReasons(r)
    .filter((reason) => !FO_CORRUPT_QUIET.has(reason))
    .forEach((reason) => {
      td.appendChild(foBadge("fmt-corrupt", "⚠ " + reason, foCorruptTitle(reason, r)));
    });
  return td;
}

function renderFoTable() {
  const body = document.getElementById("fo-body");
  const countEl = document.getElementById("fo-count");
  const visible = foApplySort(foVisible());
  body.innerHTML = "";
  foUpdateSortIndicators();

  if (!FO.fetched) {
    body.appendChild(el("tr", {}, [el("td", { class: "muted", colspan: "9", text: "Not fetched yet — click Fetch." })]));
    countEl.textContent = "";
    renderFoPager(0);
    return;
  }
  if (!FO.rows.some((r) => foFmt(r))) {
    body.appendChild(el("tr", {}, [el("td", { class: "muted", colspan: "9", text: "No format data yet — click Fetch to request Jellyfin's media info." })]));
    countEl.textContent = "";
    renderFoPager(0);
    return;
  }
  if (visible.length === 0) {
    body.appendChild(el("tr", {}, [el("td", { class: "muted", colspan: "9", text: "No items match the filters." })]));
    countEl.textContent = "";
    renderFoPager(0);
    return;
  }

  const pages = Math.max(1, Math.ceil(visible.length / FO_PAGE_SIZE));
  if (FO.page > pages) FO.page = pages;
  if (FO.page < 1) FO.page = 1;
  const start = (FO.page - 1) * FO_PAGE_SIZE;
  const pageRows = visible.slice(start, start + FO_PAGE_SIZE);

  countEl.textContent = visible.length + " / " + FO.rows.length + " shown";

  pageRows.forEach((r) => {
    const tr = el("tr");
    const f = foFmt(r);
    const v = foVideoStream(r);

    tr.appendChild(el("td", { text: r.type }));
    const parent = r.path ? r.path.replace(/[\\/][^\\/]*$/, "") : "";
    tr.appendChild(el("td", { class: "muted", text: parent, title: parent }));
    tr.appendChild(el("td", { text: r.file, title: r.file }));
    const nameTd = el("td", { title: r.display || r.name });
    nameTd.appendChild(el("div", { text: r.display || r.name }));
    const episodeLine = episodeSubLine(r);
    if (episodeLine) {
      nameTd.appendChild(el("div", { class: "muted name-sub", text: episodeLine }));
    }
    tr.appendChild(nameTd);

    tr.appendChild(el("td", { class: "muted", text: (f && f.c) || "—", title: (f && f.c) || "" }));
    tr.appendChild(el("td", {
      class: "muted",
      text: foVideoText(v) || "—",
      title: foVideoText(v) || (f ? (foNoInfo(r) ? "No stream info — click Probe missing" : "No video stream") : "No format data"),
    }));
    tr.appendChild(el("td", {
      class: "muted",
      text: foAudioText(r) || "—",
      title: foAudioText(r) || (f ? (foNoInfo(r) ? "No stream info — click Probe missing" : "No audio stream") : "No format data"),
    }));
    tr.appendChild(foPlaybackCell(r));
    tr.appendChild(rowActions(r, {
      urls: { jellyfin: FO.jellyfin, radarr: FO.radarr, sonarr: FO.sonarr },
      google: true,
    }));
    body.appendChild(tr);
    if (r.id) registerPopup(nameTd, { key: r.id, itemId: r.id });
  });

  renderFoPager(visible.length);
}

function renderFoPager(total) {
  const box = document.getElementById("fo-pager");
  if (!box) return;
  box.innerHTML = "";
  if (!total) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const pages = Math.max(1, Math.ceil(total / FO_PAGE_SIZE));

  const mkBtn = (label, disabled, onClick) => {
    const b = el("button", { type: "button", class: "page-btn", text: label });
    b.disabled = disabled;
    b.addEventListener("click", onClick);
    return b;
  };

  box.appendChild(mkBtn("« First", FO.page === 1, () => { FO.page = 1; renderFoTable(); }));
  box.appendChild(mkBtn("‹ Prev", FO.page === 1, () => { FO.page -= 1; renderFoTable(); }));
  box.appendChild(el("span", {
    class: "muted page-info",
    text: "Page " + FO.page + " of " + pages,
  }));
  box.appendChild(mkBtn("Next ›", FO.page === pages, () => { FO.page += 1; renderFoTable(); }));
  box.appendChild(mkBtn("Last »", FO.page === pages, () => { FO.page = pages; renderFoTable(); }));
}

// ---- suggestion chips ------------------------------------------------------

function foChip(label, count, active, onClick) {
  const b = el("button", {
    type: "button",
    class: "fmt-chip" + (active ? " fmt-chip-on" : ""),
    text: label + " (" + count + ")",
  });
  b.addEventListener("click", onClick);
  return b;
}

function foSetSuggest(key) {
  FO.filters.suggest = FO.filters.suggest === key ? "" : key;
  FO.page = 1;
  foSaveFilters();
  renderFoSuggestions();
  renderFoTable();
}

function foRenderChips(hostId, items) {
  const host = document.getElementById(hostId);
  if (!host) return;
  host.innerHTML = "";
  items.forEach((item) => host.appendChild(item));
}

function renderFoSuggestions() {
  const active = FO.filters.suggest;
  const count = (fn) => FO.rows.filter(fn).length;
  const preset = (key, label) => foChip(label, count(FO_SUGGEST[key]), active === key,
    () => foSetSuggest(key));

  foRenderChips("fo-sug-video", [
    preset("av1", "AV1"),
    preset("av1_4k", "AV1 4K"),
    preset("h264_10", "10-bit H.264"),
    preset("dovi", "Dolby Vision"),
    preset("legacy", "Legacy codecs"),
  ]);
  foRenderChips("fo-sug-audio", [
    preset("opus", "Opus"),
    preset("dts_truehd", "DTS/TrueHD"),
  ]);
  foRenderChips("fo-sug-combined", [
    preset("corrupt", "Corrupt"),
    preset("av1_opus", "AV1 + Opus"),
    preset("wont", "Won't direct-play"),
    preset("direct", "Direct play"),
    preset("novideo", "No video stream"),
    preset("needsprobe", "Needs probe"),
  ]);

  // The most common container|video|audio combinations in this library. Unlike
  // a hard-coded list these follow the data, so they stay honest as it changes.
  const counts = new Map();
  FO.rows.forEach((r) => {
    const key = foComboKey(r);
    if (!key) return;
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  const combos = [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 10)
    .map(([key, n]) => {
      const [c, v, a] = key.split("|");
      const label = [c, v, a.replace(/,/g, "+")].join(" · ");
      return foChip(label, n, active === "combo:" + key, () => foSetSuggest("combo:" + key));
    });
  foRenderChips("fo-sug-combos", combos);
}

// ---- device profiles -------------------------------------------------------

function foProfileHint() {
  const p = FO.profile || {};
  const bits = [];
  if ((p.containers || []).length) bits.push((p.containers || []).join("/"));
  const video = Object.entries(p.video || {}).map(([c, d]) => c + "≤" + d);
  if (video.length) bits.push(video.join(", "));
  if ((p.audio || []).length) bits.push((p.audio || []).join("/"));
  const hint = document.getElementById("fo-device-hint");
  if (hint) hint.textContent = bits.join(" · ");
}

function foPopulateDevices() {
  const sel = document.getElementById("fo-device");
  if (!sel) return;
  sel.innerHTML = "";
  const names = Object.keys(FO.profiles)
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
  names.forEach((name) => {
    const opt = el("option", { value: name, text: name });
    if (name === FO.device) opt.selected = true;
    sel.appendChild(opt);
  });
  if (!names.includes(FO.device)) FO.device = names[0] || "";
  sel.value = FO.device;
  FO.profile = FO.profiles[FO.device] || {};
  foProfileHint();
}

function foSetDevice(name) {
  FO.device = name;
  FO.profile = FO.profiles[name] || {};
  try { localStorage.setItem(FO_DEVICE_KEY, name); } catch (e) {}
  foProfileHint();
  renderFoTable();
}

// The profile editor reuses the Tracks/Subfind dialog shell (``subdl-*``).
function foOpenEditor(name, isNew) {
  const existing = FO.profiles[name];
  const seed = isNew ? JSON.parse(JSON.stringify(FO.profile || {})) : (existing || {});
  const saved = FO.saved.includes(name);

  const back = el("div", { class: "subdl-back" });
  const box = el("div", { class: "subdl fmt-editor" });
  const close = () => back.remove();

  box.appendChild(el("h3", { text: isNew ? "New device profile" : "Device profile" }));
  box.appendChild(el("p", { class: "muted", text: "What this device can decode without transcoding." }));

  const nameRow = el("label", { class: "fmt-editor-name" });
  nameRow.appendChild(el("span", { text: "Name" }));
  const nameInput = el("input", { type: "text", value: isNew ? "" : name, placeholder: "e.g. Bedroom TV" });
  nameRow.appendChild(nameInput);
  box.appendChild(nameRow);

  // Offer every format present in the library plus whatever the profile already
  // has, so a saved profile never loses an entry just because it is rare. The
  // small base list keeps the editor usable before a fetch has run.
  const containers = [...new Set([
    "mkv", "mp4", "avi", "mov", "webm",
    ...FO.rows.map((r) => { const f = foFmt(r); return (f && f.c) || ""; }).filter(Boolean),
    ...(seed.containers || []),
  ])].sort();
  const videoCodecs = [...new Set([
    "h264", "hevc", "av1", "vp9", "mpeg4", "mpeg2video",
    ...FO.rows.map((r) => foCodec(r)).filter(Boolean),
    ...Object.keys(seed.video || {}),
  ])].sort();
  const audioCodecs = [...new Set([
    "aac", "ac3", "eac3", "mp3", "opus", "dts", "truehd", "flac",
    ...FO.rows.flatMap((r) => foAudioCodecs(r)),
    ...(seed.audio || []),
  ])].sort();

  const containersBox = el("div", { class: "fmt-editor-grid" });
  const containerChecks = {};
  containers.forEach((c) => {
    const label = el("label", { class: "fmt-check" });
    const cb = el("input", { type: "checkbox" });
    cb.checked = (seed.containers || []).includes(c);
    containerChecks[c] = cb;
    label.appendChild(cb);
    label.appendChild(el("span", { text: c }));
    containersBox.appendChild(label);
  });

  const videoBox = el("div", { class: "fmt-editor-grid" });
  const videoChecks = {};
  videoCodecs.forEach((c) => {
    const label = el("label", { class: "fmt-check" });
    const cb = el("input", { type: "checkbox" });
    const has = Object.prototype.hasOwnProperty.call(seed.video || {}, c);
    cb.checked = has;
    const depth = el("input", { type: "number", class: "fmt-depth", min: "1", max: "16" });
    depth.value = has ? seed.video[c] : (["hevc", "vp9", "av1"].includes(c) ? 10 : 8);
    depth.disabled = !has;
    cb.addEventListener("change", () => { depth.disabled = !cb.checked; });
    videoChecks[c] = { cb, depth };
    label.appendChild(cb);
    label.appendChild(el("span", { text: c }));
    label.appendChild(depth);
    label.appendChild(el("span", { class: "muted", text: "bit" }));
    videoBox.appendChild(label);
  });

  const audioBox = el("div", { class: "fmt-editor-grid" });
  const audioChecks = {};
  audioCodecs.forEach((c) => {
    const label = el("label", { class: "fmt-check" });
    const cb = el("input", { type: "checkbox" });
    cb.checked = (seed.audio || []).includes(c);
    audioChecks[c] = cb;
    label.appendChild(cb);
    label.appendChild(el("span", { text: c }));
    audioBox.appendChild(label);
  });

  [["Containers", containersBox], ["Video (max bit depth)", videoBox], ["Audio", audioBox]]
    .forEach(([title, node]) => {
      box.appendChild(el("h4", { text: title }));
      box.appendChild(node);
    });

  const status = el("div", { class: "fmt-editor-status muted" });
  const collect = () => {
    const profile = { containers: [], video: {}, audio: [] };
    Object.entries(containerChecks).forEach(([c, cb]) => { if (cb.checked) profile.containers.push(c); });
    Object.entries(videoChecks).forEach(([c, pair]) => {
      if (!pair.cb.checked) return;
      const depth = parseInt(pair.depth.value, 10);
      if (depth > 0) profile.video[c] = depth;
    });
    Object.entries(audioChecks).forEach(([c, cb]) => { if (cb.checked) profile.audio.push(c); });
    return profile;
  };

  const buttons = el("div", { class: "fmt-editor-buttons" });
  const saveBtn = el("button", { type: "button", class: "primary", text: "Save" });
  const deleteBtn = el("button", { type: "button", class: "prof-del", text: "Delete" });
  const closeBtn = el("button", { type: "button", text: "Close" });
  deleteBtn.hidden = isNew || !saved;
  buttons.appendChild(saveBtn);
  buttons.appendChild(deleteBtn);
  buttons.appendChild(closeBtn);
  box.appendChild(status);
  box.appendChild(buttons);

  const save = async (deleted) => {
    const target = nameInput.value.trim();
    if (!target) { status.textContent = "Give the profile a name."; status.classList.add("error"); return; }
    status.classList.remove("error");
    status.textContent = deleted ? "Deleting…" : "Saving…";
    const body = "name=" + encodeURIComponent(target) +
      (deleted ? "" : "&profile=" + encodeURIComponent(JSON.stringify(collect())));
    try {
      const data = await postJSON("/api/formats/profiles", body);
      if (!data.ok) throw new Error(data.error || "Save failed");
      FO.profiles = data.profiles || {};
      FO.builtin = data.builtin || [];
      FO.saved = data.saved || [];
      FO.device = target;
      FO.profile = FO.profiles[target] || {};
      foPopulateDevices();
      renderFoTable();
      close();
    } catch (err) {
      status.textContent = String(err.message || err);
      status.classList.add("error");
    }
  };

  saveBtn.addEventListener("click", () => save(false));
  deleteBtn.addEventListener("click", () => save(true));
  closeBtn.addEventListener("click", close);
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  box.addEventListener("click", (e) => e.stopPropagation());

  back.appendChild(box);
  document.body.appendChild(back);
  nameInput.focus();
}

// ---- load / fetch ----------------------------------------------------------

function foApplyFilterUI() {
  FO.ms.lib.setValues(FO.filters.lib);
  FO.ms.type.setValues(FO.filters.type);
  FO.ms.container.setValues(FO.filters.container);
  FO.ms.vcodec.setValues(FO.filters.vcodec);
  FO.ms.depth.setValues(FO.filters.depth);
  FO.ms.hdr.setValues(FO.filters.hdr);
  FO.ms.acodec.setValues(FO.filters.acodec);
  FO.ms.channels.setValues(FO.filters.channels);
  FO.ms.playback.setValues(FO.filters.playback);
  FO.ms.corrupt.setValues(FO.filters.corrupt);
  document.getElementById("fo-search-file").value = FO.filters.file;
  document.getElementById("fo-search-jf").value = FO.filters.jf;
}

function foPopulateOptions() {
  const libs = [...new Set(FO.rows.map((r) => r.library).filter(Boolean))].sort();
  const types = [...new Set(FO.rows.map((r) => r.type).filter(Boolean))].sort();
  const containers = [...new Set(FO.rows.map((r) => { const f = foFmt(r); return (f && f.c) || ""; }).filter(Boolean))].sort();
  const vcodecs = [...new Set(FO.rows.map((r) => foCodec(r)).filter(Boolean))].sort();
  const depths = [...new Set(FO.rows.map((r) => {
    const d = foDepth(r);
    return d ? d + "-bit" : "";
  }).filter(Boolean))].sort((a, b) => parseInt(a, 10) - parseInt(b, 10));
  const hdrs = [...new Set(FO.rows.map((r) => foRange(r)).filter(Boolean))].sort();
  const acodecs = [...new Set(FO.rows.flatMap((r) => foAudioCodecs(r)))].sort();
  const channels = [...new Set(FO.rows.flatMap((r) => foAudioStreams(r).map((t) => foChannels(t.ch))).filter(Boolean))]
    .sort((a, b) => parseFloat(a) - parseFloat(b));

  const opts = (list) => list.map((v) => ({ value: v, label: v }));
  FO.ms.lib.setOptions(opts(libs));
  FO.ms.type.setOptions(opts(types));
  FO.ms.container.setOptions(opts(containers));
  FO.ms.vcodec.setOptions(opts(vcodecs));
  FO.ms.depth.setOptions(opts(depths));
  FO.ms.hdr.setOptions(opts(hdrs));
  FO.ms.acodec.setOptions(opts(acodecs));
  FO.ms.channels.setOptions(opts(channels));
  FO.ms.playback.setOptions(FO_PLAYBACK_OPTIONS);
  FO.ms.corrupt.setOptions(FO_CORRUPT_OPTIONS);
}

function foUpdateProbeButton() {
  const btn = document.getElementById("fo-probe");
  if (!btn) return;
  // An older cache has no format blobs at all: Fetch is the right action then,
  // not probing 26k files 500 at a time.
  const anyFormat = FO.rows.some((r) => !!foFmt(r));
  const n = anyFormat ? FO.rows.filter(foNeedsProbe).length : 0;
  btn.disabled = !n;
  btn.textContent = n ? "Probe missing (" + n + ")" : "Probe missing";
  btn.title = n
    ? n + " file(s) have no stream info in Jellyfin; read them with ffprobe."
    : anyFormat
      ? "Every file has Jellyfin stream info."
      : "Fetch first, then probe any file Jellyfin has no stream info for.";
}

async function foLoad() {
  const errEl = document.getElementById("fo-error");
  try {
    const data = await getJSON("/api/formats/items");
    FO.rows = data.rows || [];
    FO.jellyfin = data.jellyfin_url || "";
    FO.radarr = data.radarr_url || "";
    FO.sonarr = data.sonarr_url || "";
    FO.fetched = !!data.fetched;
    FO.profiles = data.profiles || {};
    FO.builtin = data.builtin || [];
    FO.saved = data.saved || [];
    try {
      const savedDevice = localStorage.getItem(FO_DEVICE_KEY);
      if (savedDevice) FO.device = savedDevice;
    } catch (e) {}
    if (!FO.profiles[FO.device]) {
      FO.device = FO.profiles["Vardagsrum TV"] ? "Vardagsrum TV" : (Object.keys(FO.profiles)[0] || "");
    }
    document.getElementById("fo-sync-age").textContent = fmtAgo(data.last_sync);
    foPopulateDevices();
    foPopulateOptions();
    foApplyFilterUI();
    renderFoSuggestions();
    renderFoTable();
    foUpdateProbeButton();
    errEl.hidden = true;
  } catch (err) {
    errEl.textContent = String(err);
    errEl.hidden = false;
  }
}

async function foFetch() {
  const btn = document.getElementById("fo-fetch");
  const prog = document.getElementById("fo-progress");
  const fill = document.getElementById("fo-progress-fill");
  const statusEl = document.getElementById("fo-status");
  btn.disabled = true;
  prog.hidden = false;
  let res;
  try {
    res = await postJSON("/api/jellyfin-id/refresh", null);
  } catch (e) {
    prog.hidden = true;
    btn.disabled = false;
    statusEl.textContent = String(e);
    return;
  }
  if (res && res.started === false) {
    prog.hidden = true;
    btn.disabled = false;
    statusEl.textContent = res.message || "Busy.";
    return;
  }
  startStatusPoll(
    "/api/jellyfin-id/status",
    (s) => {
      if (s.status === "running") {
        const pct = s.total ? Math.round(100 * s.fetched / s.total) : 0;
        fill.style.width = pct + "%";
        statusEl.textContent = "Fetching… " + s.fetched + "/" + (s.total || "?") + " (" + pct + "%)";
      } else {
        statusEl.textContent = s.status === "error" ? "Fetch failed: " + (s.error || "") : "Fetched.";
      }
    },
    async (s) => {
      prog.hidden = true;
      btn.disabled = false;
      if (s.status !== "error") await foLoad();
    },
    600
  );
}

async function foProbe() {
  const btn = document.getElementById("fo-probe");
  const statusEl = document.getElementById("fo-status");
  if (btn) btn.disabled = true;
  statusEl.textContent = "Probing files Jellyfin has no stream info for…";
  try {
    const res = await postJSON("/api/formats/probe", null);
    if (!res || !res.ok) throw new Error((res && res.error) || "Probe failed");
    const bits = ["Probed " + res.probed + " file(s)"];
    if (res.failed) bits.push(res.failed + " failed");
    if (res.remaining) bits.push(res.remaining + " still need probing — click again");
    statusEl.textContent = bits.join("; ") + ".";
    await foLoad();
  } catch (e) {
    statusEl.textContent = String(e.message || e);
    if (btn) btn.disabled = false;
  }
}

// ---- init ------------------------------------------------------------------

function foBindControls() {
  const onFilter = () => { FO.page = 1; foSaveFilters(); renderFoTable(); };

  FO.ms.lib = MultiSelect(document.getElementById("fo-lib"), [], (vals) => {
    FO.filters.lib = vals; onFilter();
  });
  FO.ms.type = MultiSelect(document.getElementById("fo-type"), [], (vals) => {
    FO.filters.type = vals; onFilter();
  });
  FO.ms.container = MultiSelect(document.getElementById("fo-container"), [], (vals) => {
    FO.filters.container = vals; onFilter();
  });
  FO.ms.vcodec = MultiSelect(document.getElementById("fo-vcodec"), [], (vals) => {
    FO.filters.vcodec = vals; onFilter();
  });
  FO.ms.depth = MultiSelect(document.getElementById("fo-depth"), [], (vals) => {
    FO.filters.depth = vals; onFilter();
  });
  FO.ms.hdr = MultiSelect(document.getElementById("fo-hdr"), [], (vals) => {
    FO.filters.hdr = vals; onFilter();
  });
  FO.ms.acodec = MultiSelect(document.getElementById("fo-acodec"), [], (vals) => {
    FO.filters.acodec = vals; onFilter();
  });
  FO.ms.channels = MultiSelect(document.getElementById("fo-channels"), [], (vals) => {
    FO.filters.channels = vals; onFilter();
  });
  FO.ms.playback = MultiSelect(document.getElementById("fo-playback"), FO_PLAYBACK_OPTIONS, (vals) => {
    FO.filters.playback = vals; onFilter();
  });
  FO.ms.corrupt = MultiSelect(document.getElementById("fo-corrupt"), FO_CORRUPT_OPTIONS, (vals) => {
    FO.filters.corrupt = vals; onFilter();
  });

  document.getElementById("fo-search-file").addEventListener("input", () => {
    FO.filters.file = document.getElementById("fo-search-file").value;
    onFilter();
  });
  document.getElementById("fo-search-jf").addEventListener("input", () => {
    FO.filters.jf = document.getElementById("fo-search-jf").value;
    onFilter();
  });

  document.getElementById("fo-fetch").addEventListener("click", foFetch);
  document.getElementById("fo-probe").addEventListener("click", foProbe);
  document.getElementById("fo-device").addEventListener("change", (e) => foSetDevice(e.target.value));
  document.getElementById("fo-edit-profile").addEventListener("click", () => foOpenEditor(FO.device, false));
  document.getElementById("fo-new-profile").addEventListener("click", () => foOpenEditor(FO.device, true));
}

document.addEventListener("DOMContentLoaded", () => {
  foLoadFilters();
  foLoadSort();
  foBindControls();
  foBindSortHeaders();
  window.audioRefresh = () => { /* Formats shows cached Jellyfin info, not scans. */ };
  FilterProfiles(document.getElementById("fo-profiles"),
    "formats",
    () => ({ filters: FO.filters, sort: FO.sort }),
    (p) => {
      const f = (p && p.filters) ? p.filters : (p || {});
      FO.filters = {
        lib: Array.isArray(f.lib) ? f.lib : [],
        type: Array.isArray(f.type) ? f.type : [],
        container: Array.isArray(f.container) ? f.container : [],
        vcodec: Array.isArray(f.vcodec) ? f.vcodec : [],
        depth: Array.isArray(f.depth) ? f.depth : [],
        hdr: Array.isArray(f.hdr) ? f.hdr : [],
        acodec: Array.isArray(f.acodec) ? f.acodec : [],
        channels: Array.isArray(f.channels) ? f.channels : [],
        playback: Array.isArray(f.playback) ? f.playback : [],
        corrupt: Array.isArray(f.corrupt) ? f.corrupt : [],
        file: typeof f.file === "string" ? f.file : "",
        jf: typeof f.jf === "string" ? f.jf : "",
        suggest: typeof f.suggest === "string" ? f.suggest : "",
      };
      const s = (p && p.sort) || {};
      if (SORTABLE[s.col]) FO.sort = { col: s.col, dir: s.dir === -1 ? -1 : 1 };
      FO.page = 1;
      foSaveFilters();
      foSaveSort();
      foApplyFilterUI();
      renderFoSuggestions();
      renderFoTable();
    });
  foLoad();
});
