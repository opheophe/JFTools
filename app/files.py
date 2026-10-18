"""Read-only file browser over the media folders.

Every path is resolved through the Media-path mappings and must land inside a
"browser root" — a mapping target or a Jellyfin library location — after
symlink-canonicalisation. Anything else (traversal, symlink escapes, the app's
own directories) is refused, so the browser can never leave the media folders.
"""

from __future__ import annotations

import os
import shutil
import stat as stat_mod
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import config
from .logs import log
from .paths import resolve_path
from .web import BASE_DIR, require_login, templates

router = APIRouter()

_MAX_ENTRIES = 2000


def _deny_dirs() -> list[str]:
    """Directories the browser must never enter, even if mapped as media."""
    return [
        os.path.realpath(str(BASE_DIR)),
        os.path.realpath(str(config.data_dir)),
    ]


def browser_roots() -> list[str]:
    """Local directories the browser may enter, deduplicated and canonical."""
    roots: list[str] = []
    seen: set[str] = set()

    def add(p: str) -> None:
        p = (p or "").strip()
        if not p:
            return
        try:
            rp = os.path.realpath(p)
        except OSError:
            return
        key = os.path.normcase(rp)
        if not key or key in seen:
            return
        seen.add(key)
        roots.append(rp)

    # Explicit mapping targets.
    for m in config.settings.path_maps:
        if isinstance(m, dict):
            add(str(m.get("to") or ""))

    # Jellyfin library locations, translated through the mappings.
    try:
        from . import libraries

        for lib in (libraries._state.get("libraries") or []):
            for loc in (lib.get("locations") or []):
                add(resolve_path(str(loc), config.settings.path_maps))
    except Exception:  # noqa: BLE001 — the browser must never crash on this
        pass

    deny = {os.path.normcase(d) for d in _deny_dirs()}
    return [r for r in roots if os.path.normcase(r) not in deny]


def _resolve(raw: str, roots: list[str]) -> str | None:
    """Canonical local path if it lands inside a root, else None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    local = resolve_path(raw, config.settings.path_maps)
    try:
        target = os.path.realpath(os.path.abspath(local))
    except (OSError, ValueError):
        return None
    tp = Path(target)
    allowed = False
    for root in roots:
        try:
            tp.relative_to(Path(root))
            allowed = True
            break
        except ValueError:
            continue
    if not allowed:
        return None
    for deny in _deny_dirs():
        try:
            tp.relative_to(Path(deny))
            return None
        except ValueError:
            continue
    return target


def _crumb(dir_path: str) -> list[list[str]]:
    """Path segments as [[full_path, label], ...] from the drive root down."""
    parts = Path(dir_path).parts
    if not parts:
        return []
    acc = Path(parts[0])
    crumbs = []
    for part in parts[1:]:
        acc = acc / part
        crumbs.append([str(acc), part])
    return crumbs


# ---- routes -----------------------------------------------------------------


@router.get("/files", response_class=HTMLResponse)
async def files_page(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(request, "files.html", {"request": request})


@router.get("/api/files/roots")
async def api_files_roots(user: str = Depends(require_login)):
    roots = browser_roots()
    return JSONResponse(
        {
            "ok": True,
            "roots": [{"path": r, "name": Path(r).name or r} for r in roots],
        }
    )


@router.get("/api/files/list")
async def api_files_list(path: str = "", user: str = Depends(require_login)):
    roots = browser_roots()
    if not roots:
        return JSONResponse(
            {
                "ok": True,
                "pick": True,
                "roots": [],
                "error": "No media folders found yet. Add mappings under Settings → Media paths, or refresh Libraries once.",
            }
        )

    target = _resolve(path, roots)
    if target is None:
        return JSONResponse(
            {"ok": False, "error": "That path is outside the media folders."},
            status_code=403,
        )

    if os.path.isfile(target):
        list_dir = os.path.dirname(target)
        highlight = os.path.basename(target)
    elif os.path.isdir(target):
        list_dir = target
        highlight = None
    else:
        return JSONResponse(
            {"ok": False, "error": "No such file or folder."}, status_code=404
        )

    try:
        names = os.listdir(list_dir)
    except PermissionError:
        log("Files", "ERROR", f"Listing denied: {list_dir}")
        return JSONResponse(
            {"ok": False, "error": "Permission denied reading that folder."},
            status_code=403,
        )
    except OSError as exc:
        log("Files", "ERROR", f"Listing failed: {list_dir}: {exc}")
        return JSONResponse(
            {"ok": False, "error": f"Could not read that folder: {exc}"},
            status_code=500,
        )

    entries = []
    for name in names:
        full = os.path.join(list_dir, name)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        is_dir = stat_mod.S_ISDIR(st.st_mode)
        entries.append(
            {
                "name": name,
                "path": full,
                "dir": is_dir,
                "size": st.st_size if stat_mod.S_ISREG(st.st_mode) else None,
                "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                "ext": "Folder" if is_dir else (Path(name).suffix.lstrip(".").upper() or "File"),
            }
        )

    entries.sort(key=lambda e: (not e["dir"], e["name"].casefold()))
    truncated = len(entries) > _MAX_ENTRIES
    if truncated:
        entries = entries[:_MAX_ENTRIES]

    parent = os.path.dirname(list_dir)
    if _resolve(parent, roots) is None:
        parent = None

    usage = None
    try:
        u = shutil.disk_usage(list_dir)
        usage = {"total": u.total, "used": u.used, "free": u.free}
    except OSError:
        pass

    return JSONResponse(
        {
            "ok": True,
            "path": list_dir,
            "parent": parent,
            "crumb": _crumb(list_dir),
            "entries": entries,
            "highlight": highlight,
            "writable": os.access(list_dir, os.W_OK),
            "usage": usage,
            "truncated": truncated,
        }
    )
