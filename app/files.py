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

from fastapi import APIRouter, Depends, Form, Header, Request
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
    if not _within(target, roots) or _denied(target):
        return None
    return target


def _within(target: str, roots: list[str]) -> bool:
    tp = Path(target)
    for root in roots:
        try:
            tp.relative_to(Path(root))
            return True
        except ValueError:
            continue
    return False


def _denied(target: str) -> bool:
    tp = Path(target)
    for deny in _deny_dirs():
        try:
            tp.relative_to(Path(deny))
            return True
        except ValueError:
            continue
    return False


def _root_paths(roots: list[str]) -> set[str]:
    return {os.path.normcase(r) for r in roots}


# ---- mutation helpers -------------------------------------------------------


def _bad_name(name: str) -> bool:
    n = (name or "").strip()
    if not n or n in (".", "..") or len(n) > 200:
        return True
    if "/" in n or "\\" in n:
        return True
    if os.name == "nt" and any(ch in n for ch in ':<>"|?*'):
        return True
    return False


def _mut_source(raw: str, roots: list[str]) -> str | None:
    """Path to act on, validated without resolving its final component.

    The parent is canonicalised (so a symlinked parent cannot smuggle the
    operation outside the media folders) but the entry itself is not, so
    symlinks are renamed/moved/deleted as links, never through their target.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    local = resolve_path(raw, config.settings.path_maps)
    try:
        src = os.path.abspath(local)
    except (OSError, ValueError):
        return None
    name = os.path.basename(src)
    if name in ("", ".", ".."):
        return None
    try:
        parent_real = os.path.realpath(os.path.dirname(src))
    except OSError:
        return None
    if not _within(parent_real, roots) or _denied(parent_real):
        return None
    full = os.path.join(parent_real, name)
    if not os.path.lexists(full):
        return None
    try:
        if os.path.normcase(os.path.realpath(full)) in _root_paths(roots):
            return None  # never operate on a browser root itself
    except OSError:
        pass
    return full


def _mut_dir(raw: str, roots: list[str]) -> str | None:
    """An existing directory inside a root (symlink-canonicalised)."""
    target = _resolve(raw, roots)
    if target is None or not os.path.isdir(target):
        return None
    return target


def rename_entry(path: str, new_name: str, roots: list[str]) -> dict:
    src = _mut_source(path, roots)
    if not src:
        return {"ok": False, "error": "That path is outside the media folders."}
    if _bad_name(new_name):
        return {"ok": False, "error": "Invalid name."}
    dst = os.path.join(os.path.dirname(src), new_name.strip())
    if os.path.normcase(os.path.realpath(dst)) == os.path.normcase(src):
        return {"ok": True, "src": src, "dst": dst}  # no-op
    if os.path.lexists(dst):
        return {"ok": False, "error": "A file with that name already exists."}
    try:
        os.rename(src, dst)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "src": src, "dst": dst}


def move_entry(path: str, target_dir: str, roots: list[str]) -> dict:
    src = _mut_source(path, roots)
    if not src:
        return {"ok": False, "error": "That path is outside the media folders."}
    dst_dir = _mut_dir(target_dir, roots)
    if not dst_dir:
        return {"ok": False, "error": "Target folder is outside the media folders or not a folder."}
    name = os.path.basename(src)
    if os.path.normcase(os.path.realpath(dst_dir)) == os.path.normcase(os.path.dirname(src)):
        return {"ok": True, "src": src, "dst": os.path.join(dst_dir, name)}  # no-op
    if os.path.isdir(src) and not os.path.islink(src):
        # cannot move a folder into itself or one of its descendants
        sr = os.path.normcase(os.path.realpath(src))
        dr = os.path.normcase(dst_dir)
        if dr == sr or dr.startswith(sr + os.sep):
            return {"ok": False, "error": "Cannot move a folder into itself."}
    dst = os.path.join(dst_dir, name)
    if os.path.lexists(dst):
        return {"ok": False, "error": "A file with that name already exists there."}
    try:
        shutil.move(src, dst)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "src": src, "dst": dst}


def delete_entry(path: str, roots: list[str]) -> dict:
    src = _mut_source(path, roots)
    if not src:
        return {"ok": False, "error": "That path is outside the media folders."}
    try:
        if os.path.isdir(src) and not os.path.islink(src):
            shutil.rmtree(src)
        else:
            os.remove(src)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "src": src}


def mkdir_entry(path: str, name: str, roots: list[str]) -> dict:
    parent = _mut_dir(path, roots)
    if not parent:
        return {"ok": False, "error": "That folder is outside the media folders or missing."}
    if _bad_name(name):
        return {"ok": False, "error": "Invalid name."}
    dst = os.path.join(parent, name.strip())
    if os.path.lexists(dst):
        return {"ok": False, "error": "That name already exists."}
    try:
        os.mkdir(dst)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "src": dst}


def _mutation_gate(x_jftools: str):
    """403 unless file management is enabled and the CSRF header is present."""
    if not config.settings.file_mgmt:
        return JSONResponse(
            {"ok": False, "error": "File management is disabled. Enable it on the Settings page."},
            status_code=403,
        )
    if x_jftools != "1":
        return JSONResponse({"ok": False, "error": "Missing security header."}, status_code=403)
    return None


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
            "mgmt": config.settings.file_mgmt,
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
            "mgmt": config.settings.file_mgmt,
        }
    )


# ---- mutations --------------------------------------------------------------
# These are sync endpoints (run in FastAPI's thread pool) because the file
# operations can be slow (large files on a NAS).


def _mutation_error(resp: dict) -> JSONResponse:
    return JSONResponse({"ok": False, "error": resp.get("error", "Failed.")}, status_code=400)


@router.post("/api/files/rename")
def api_files_rename(
    path: str = Form(""),
    name: str = Form(""),
    user: str = Depends(require_login),
    x_jftools: str = Header(default=""),
):
    gate = _mutation_gate(x_jftools)
    if gate:
        return gate
    roots = browser_roots()
    if not roots:
        return JSONResponse({"ok": False, "error": "No media folders found."}, status_code=403)
    result = rename_entry(path, name, roots)
    if result["ok"]:
        log("Files", "INFO", f"Renamed: {result['src']} -> {result['dst']}")
        return JSONResponse({"ok": True, "message": "Renamed."})
    log("Files", "WARN", f"Rename refused ({result.get('error')}): {path}")
    return _mutation_error(result)


@router.post("/api/files/move")
def api_files_move(
    path: str = Form(""),
    target: str = Form(""),
    user: str = Depends(require_login),
    x_jftools: str = Header(default=""),
):
    gate = _mutation_gate(x_jftools)
    if gate:
        return gate
    roots = browser_roots()
    if not roots:
        return JSONResponse({"ok": False, "error": "No media folders found."}, status_code=403)
    result = move_entry(path, target, roots)
    if result["ok"]:
        log("Files", "INFO", f"Moved: {result['src']} -> {result['dst']}")
        return JSONResponse({"ok": True, "message": "Moved."})
    log("Files", "WARN", f"Move refused ({result.get('error')}): {path}")
    return _mutation_error(result)


@router.post("/api/files/delete")
def api_files_delete(
    path: str = Form(""),
    user: str = Depends(require_login),
    x_jftools: str = Header(default=""),
):
    gate = _mutation_gate(x_jftools)
    if gate:
        return gate
    roots = browser_roots()
    if not roots:
        return JSONResponse({"ok": False, "error": "No media folders found."}, status_code=403)
    result = delete_entry(path, roots)
    if result["ok"]:
        log("Files", "WARN", f"Deleted: {result['src']}")
        return JSONResponse({"ok": True, "message": "Deleted."})
    log("Files", "WARN", f"Delete refused ({result.get('error')}): {path}")
    return _mutation_error(result)


@router.post("/api/files/mkdir")
def api_files_mkdir(
    path: str = Form(""),
    name: str = Form(""),
    user: str = Depends(require_login),
    x_jftools: str = Header(default=""),
):
    gate = _mutation_gate(x_jftools)
    if gate:
        return gate
    roots = browser_roots()
    if not roots:
        return JSONResponse({"ok": False, "error": "No media folders found."}, status_code=403)
    result = mkdir_entry(path, name, roots)
    if result["ok"]:
        log("Files", "INFO", f"Created folder: {result['src']}")
        return JSONResponse({"ok": True, "message": "Folder created."})
    log("Files", "WARN", f"Mkdir refused ({result.get('error')}): {path}")
    return _mutation_error(result)
