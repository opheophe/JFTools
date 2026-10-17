"""Resolve Jellyfin-reported paths to paths this machine can actually open.

Jellyfin reports each item's ``Path`` from inside its own container (e.g.
``/Filmer/Movie/movie.mkv``). When JFTools runs somewhere else — a Windows box,
or a container with different mounts — that path may not exist. The "Media
paths" settings translate a Jellyfin path prefix to a local prefix so the app
(and tools like ffmpeg) can open the real file.

Matching is longest-prefix and path-boundary aware, case-insensitive on
Windows, and normalises separators to the host OS.
"""
from __future__ import annotations


def _norm(p: str) -> str:
    """Forward slashes only, no trailing slash (used for comparison)."""
    return (p or "").replace("\\", "/").rstrip("/")


def _is_windows() -> bool:
    import os

    return os.name == "nt"


def resolve_path(path: str, path_maps: list[dict] | None = None) -> str:
    """Map ``path`` through ``path_maps`` and return a local path.

    Each mapping is ``{"from": "/Filmer", "to": "Z:\\Filmer"}``. The longest
    matching ``from`` prefix wins; the matched prefix is replaced with ``to``.
    If nothing matches the input is returned unchanged (e.g. when the container
    already mounts media at the same targets Jellyfin uses).
    """
    if not path or not path_maps:
        return path

    path_norm = _norm(path)
    windows = _is_windows()

    best_len = -1
    best_src = None  # original "from" string (case preserved)
    best_dst = None  # original "to" string
    for m in path_maps:
        if not isinstance(m, dict):
            continue
        src = str(m.get("from") or "").strip()
        dst = str(m.get("to") or "").strip()
        if not src or not dst:
            continue
        src_norm = _norm(src)
        if windows:
            p, s = path_norm.casefold(), src_norm.casefold()
        else:
            p, s = path_norm, src_norm
        if p == s or (s and p.startswith(s + "/")):
            if len(src_norm) > best_len:
                best_len = len(src_norm)
                best_src = src
                best_dst = dst

    if best_src is None:
        return path

    src_norm = _norm(best_src)
    rest = path_norm[len(src_norm):]  # "/sub/..." or ""
    joined = _norm(best_dst) + rest
    return joined.replace("/", "\\") if windows else joined
