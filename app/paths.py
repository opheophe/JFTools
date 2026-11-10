"""Resolve Jellyfin-reported paths to paths this machine can actually open.

Jellyfin reports each item's ``Path`` from inside its own container (e.g.
``/Filmer/Movie/movie.mkv``). When JFTools runs somewhere else — a Windows box,
or a container with different mounts — that path may not exist. The "Media
paths" settings translate a Jellyfin path prefix to a local prefix so the app
(and tools like ffmpeg) can open the real file.

A mapping may also carry an optional ``local`` path: the same folder as other
apps on this machine see it (e.g. a UNC share for Subber). It is used only by
features that hand a path to those apps (``Queue in Subber``); an item whose
mapping has no ``local`` path simply does not offer them.

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


def _best_mapping(
    path: str, path_maps: list[dict] | None, key: str
) -> tuple[str | None, str | None]:
    """The longest mapping for ``path`` whose ``key`` is set.

    Returns ``(from, destination)`` with the original casing, or
    ``(None, None)`` when nothing matches.
    """
    if not path or not path_maps:
        return None, None

    path_norm = _norm(path)
    windows = _is_windows()

    best_len = -1
    best_src = None  # original "from" string (case preserved)
    best_dst = None  # original destination string
    for m in path_maps:
        if not isinstance(m, dict):
            continue
        src = str(m.get("from") or "").strip()
        dst = str(m.get(key) or "").strip()
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
    return best_src, best_dst


def _apply(path: str, src: str, dst: str) -> str:
    """Replace the ``src`` prefix of ``path`` with ``dst``."""
    src_norm = _norm(src)
    rest = _norm(path)[len(src_norm):]  # "/sub/..." or ""
    joined = _norm(dst) + rest
    return joined.replace("/", "\\") if _is_windows() else joined


def resolve_path(path: str, path_maps: list[dict] | None = None) -> str:
    """Map ``path`` through ``path_maps`` and return a local path.

    Each mapping is ``{"from": "/Filmer", "to": "Z:\\Filmer"}``. The longest
    matching ``from`` prefix wins; the matched prefix is replaced with ``to``.
    If nothing matches the input is returned unchanged (e.g. when the container
    already mounts media at the same targets Jellyfin uses).
    """
    src, dst = _best_mapping(path, path_maps, "to")
    if src is None:
        return path
    return _apply(path, src, dst)


def resolve_local_path(path: str, path_maps: list[dict] | None = None) -> str:
    """Map ``path`` to the local path other apps (Subber) use, or ``""``.

    Only mappings with a non-empty ``local`` participate, so an item is offered
    to Subber only when its library has a local path configured. The longest
    matching ``from`` prefix wins, exactly as in :func:`resolve_path`.
    """
    src, dst = _best_mapping(path, path_maps, "local")
    if src is None:
        return ""
    return _apply(path, src, dst)
