"""Files a console reply names, made clickable (ui/FileLink.jsx).

  POST /api/files/resolve  {paths, bases}   which of these exist, and as what
  GET  /api/files/raw?path=                 the bytes, for the in-app viewer
  POST /api/files/open     {path, reveal}   `open` it, or select it in Finder

A reply says `~/Desktop/resume.pdf`, `/Users/…/shot.png`, a vault-relative
`01-Projects/…/x.pdf` or a project-relative `out/SCHEDULE.md`. The frontend
offers anything path-shaped; this decides what is real. Only a path that
resolves to an existing file under the home directory becomes a link.

The rules, all applied to the fully resolved path (so `..` and symlinks are
judged by where they land, not how they're spelled):

* **Under home, or nothing.** realpath(target) must sit inside realpath(~).
  A symlink in the project pointing at /etc is refused like /etc itself.
* **Not the secrets.** ~/.ssh, ~/.gnupg, keychains, cloud credentials and
  this app's own data dir (app.db holds OAuth tokens) don't resolve at all.
* **Never executed.** `open` on an app, a script, or anything with the exec
  bit would run it, so those are revealed in Finder instead — whatever the
  caller asked for.
* **Served as what we say it is.** raw serves only the viewer's kinds, under
  a type picked here from the extension (text of any kind as text/plain, so
  an .html or .svg-as-text never renders as a page), with nosniff and a
  sandbox CSP on everything but PDF, which the browser can't show sandboxed.

LocalGuard (Host + Origin on writes) covers all three routes like the rest.
"""

from __future__ import annotations

import mimetypes
import os
import stat
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import macos
from .config import DATA_DIR, VAULT_PATH

router = APIRouter(prefix="/api/files")

IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".svg", ".avif", ".ico", ".tif", ".tiff"}
VIDEO = {".mp4", ".mov", ".m4v", ".webm"}
AUDIO = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac"}
MARKDOWN = {".md", ".markdown"}
TEXT = {
    ".txt", ".log", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".xml", ".html", ".htm", ".css", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".py",
    ".sh", ".zsh", ".rb", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".hpp", ".swift", ".kt",
    ".sql", ".tex", ".bib", ".diff", ".patch", ".rst", ".org", ".conf", ".srt",
    ".vtt", ".lua", ".r", ".m", ".php", ".pl", ".scss", ".less", ".vue", ".svelte",
}
TEXT_NAMES = {"Makefile", "Dockerfile", "README", "LICENSE", ".gitignore", ".env.example"}
TEXT_MAX = 2 * 1024 * 1024   # bigger than this opens with the default app instead

# What `open` would run rather than show. Revealed in Finder instead. The
# exec bit catches the rest (an extensionless script opens in Terminal).
EXECUTABLE = {
    ".app", ".command", ".sh", ".bash", ".zsh", ".csh", ".ksh", ".fish", ".tool", ".terminal",
    ".py", ".pyc", ".rb", ".pl", ".php", ".js", ".mjs", ".jar", ".scpt", ".scptd",
    ".applescript", ".workflow", ".action", ".pkg", ".mpkg", ".dmg", ".prefpane", ".kext",
    ".osax", ".plugin", ".bundle", ".framework", ".appex", ".service", ".qlgenerator",
    ".saver", ".webloc", ".inetloc", ".fileloc", ".url", ".shortcut", ".xpc", ".dylib",
    ".so", ".bin", ".exe", ".msi", ".bat", ".cmd", ".ps1", ".mobileconfig", ".installer",
}

_SECRETS = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".netrc", ".pgpass",
            ".config/gh", ".config/gcloud", "Library/Keychains", "Library/Cookies",
            "Library/Application Support/com.apple.TCC")


def _real(p: str | Path) -> Path:
    return Path(os.path.realpath(p))


def _inside(p: Path, root: Path) -> bool:
    return p == root or root in p.parents


def allowed(p: Path) -> bool:
    """`p` is already resolved: under home, and not a secret."""
    home = _real(os.path.expanduser("~"))
    if not _inside(p, home):
        return False
    deny = [*(home / s for s in _SECRETS), _real(DATA_DIR)]
    return not any(_inside(p, d) for d in deny)


def kind_of(p: Path, st: os.stat_result) -> str:
    if stat.S_ISDIR(st.st_mode):
        return "app" if _is_bundle(p) else "dir"
    ext = p.suffix.lower()
    if ext in IMAGE:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in VIDEO:
        return "video"
    if ext in AUDIO:
        return "audio"
    if ext in MARKDOWN and st.st_size <= TEXT_MAX:
        return "markdown"
    if (ext in TEXT or p.name in TEXT_NAMES) and st.st_size <= TEXT_MAX:
        return "text"
    return "exec" if _is_executable(p, st) else "other"


def _is_bundle(p: Path) -> bool:
    return p.suffix.lower() in EXECUTABLE or (p / "Contents" / "Info.plist").exists()


def _is_executable(p: Path, st: os.stat_result) -> bool:
    if p.suffix.lower() in EXECUTABLE:
        return True
    return stat.S_ISREG(st.st_mode) and bool(st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def resolve(path: str, bases: list[str] | None = None) -> dict | None:
    """What `path` (as a reply wrote it) names on disk, or None.

    Absolute and ~ paths stand alone; a relative one is tried against each
    base (the tab's worktree, its project) and then the vault, first hit wins.
    Every base is itself held to the home rule."""
    raw = (path or "").strip()
    if not raw or "\0" in raw or len(raw) > 1024:
        return None
    raw = os.path.expanduser(raw) if raw.startswith("~/") or raw == "~" else raw
    if os.path.isabs(raw):
        candidates = [raw]
    else:
        roots = []
        for b in [*(bases or []), str(VAULT_PATH)]:
            if b and os.path.isabs(b) and allowed(_real(b)):
                roots.append(b)
        candidates = [os.path.join(b, raw) for b in roots]
    for c in candidates:
        real = _real(c)
        if not allowed(real):
            continue
        try:
            st = real.stat()
        except OSError:
            continue
        k = kind_of(real, st)
        # Viewing a script's source is fine; `open` on it is what would run it.
        runs = k in ("exec", "app") or (k != "dir" and _is_executable(real, st))
        return {"path": str(real), "name": real.name, "kind": k, "size": st.st_size,
                "viewable": k in VIEWABLE, "executable": runs,
                "vault": _inside(real, _real(VAULT_PATH))}
    return None


VIEWABLE = {"image", "pdf", "video", "audio", "markdown", "text"}


def _checked(path: str) -> dict:
    """The resolve() of an absolute path from the client, or 404. The client
    sends back what resolve returned, but it's re-judged here, never trusted."""
    if not (path or "").startswith(("/", "~/")):
        raise HTTPException(400, "absolute path required")
    hit = resolve(path)
    if hit is None:
        raise HTTPException(404, "no such file under your home folder")
    return hit


class ResolveIn(BaseModel):
    paths: list[str]
    bases: list[str] = []


@router.post("/resolve")
def post_resolve(body: ResolveIn):
    return {"files": {p: resolve(p, body.bases) for p in body.paths[:200]}}


@router.get("/raw")
def get_raw(path: str):
    hit = _checked(path)
    if not hit["viewable"]:
        raise HTTPException(415, f"{hit['kind']} files open outside the app")
    k = hit["kind"]
    if k in ("text", "markdown"):
        mime = "text/plain; charset=utf-8"
    else:
        mime = mimetypes.guess_type(hit["name"])[0] or "application/octet-stream"
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Cache-Control": "no-store",
    }
    if k != "pdf":
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'"
    return FileResponse(hit["path"], media_type=mime, headers=headers,
                        content_disposition_type="inline", filename=hit["name"])


class OpenIn(BaseModel):
    path: str
    reveal: bool = False


@router.post("/open")
def post_open(body: OpenIn):
    """`open` the file with its default app, or select it in Finder. An app,
    a script or an executable is always revealed: opening would run it."""
    hit = _checked(body.path)
    reveal = body.reveal or hit["executable"]
    try:
        if reveal:
            macos.reveal(hit["path"])
        else:
            macos.open_url(hit["path"])
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return {"ok": True, "revealed": reveal, "path": hit["path"]}
