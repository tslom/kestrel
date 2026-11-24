"""Vault views: generated tasks.md, the generated-notes inbox, note open/search.

tasks.md is a GENERATED, read-only render of the tasks DB (hand edits are
overwritten). Notes search reads index.db, which is derived.
Design: DESIGN.md → Vault views.
"""

import json
import os
import re
import shutil
import sys
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import frontmatter

from . import config as _config
from .config import VAULT_PATH, TASKS_VIEW_FILE
from . import db, indexer, macos

HEADER = (
    "<!-- AUTO-GENERATED from app.db — do not edit by hand.\n"
    "     Add/complete tasks via the dashboard or console. -->\n"
)


def _task_line(t: dict, indent: str = "") -> str:
    extra = []
    if t.get("defer"):
        extra.append(f"→ defer {t['defer']}")
    if t.get("due"):
        extra.append(f"due {t['due']}"
                     + (f" {t['due_time']}" if t.get("due_time") else ""))
    if t.get("priority"):
        extra.append(f"p{t['priority']}")
    if t.get("status") == "waiting" and t.get("waiting_on"):
        extra.append(f"⏳ waiting: {t['waiting_on']}")
    if t.get("owed_to"):
        extra.append(f"owed to: {t['owed_to']}")
    suffix = f"  ({', '.join(extra)})" if extra else ""
    return f"{indent}- [ ] {t['title']}{suffix}  <!--task:{t['id']}-->"


def regenerate_tasks_view() -> str:
    tasks = db.list_tasks(status="open")  # top-level only; subtasks ride along nested
    by_project: dict[str, list] = {}
    for t in tasks:
        by_project.setdefault(t["project"] or "Unsorted", []).append(t)
    # Count and render in one traversal so the header total matches the lines.
    body: list[str] = []
    count = 0
    for project in sorted(by_project):
        body.append(f"## {project}")
        for t in by_project[project]:
            body.append(_task_line(t))
            count += 1
            for sub in t.get("subtasks") or []:
                if sub["status"] == "open":  # this view only ever shows open tasks
                    body.append(_task_line(sub, indent="  "))
                    count += 1
        body.append("")
    lines = [HEADER, f"# Tasks ({count} open)", "", *body]
    lines.append(f"*Regenerated {datetime.now().isoformat(timespec='seconds')}*\n")
    path = VAULT_PATH / TASKS_VIEW_FILE
    # Any thread can write: serialize, and swap the file in whole. The dot-name
    # keeps the temp file from Obsidian and the watcher.
    tmp = path.with_name(f".{path.name}.tmp")
    with _regen_lock:
        tmp.write_text("\n".join(lines), encoding="utf-8")
        os.replace(tmp, path)
    return str(path)


_regen_lock = threading.Lock()


def _on_task_write() -> None:
    """db's task-write hook. Skips a redirected app.db (test) paired with the
    real vault, which would overwrite the real tasks.md with fixtures."""
    if db.APP_DB != _config.APP_DB and VAULT_PATH == _config.VAULT_PATH:
        return
    regenerate_tasks_view()


db.on_task_write(_on_task_write)


# ---------- generated-notes inbox ----------

def list_generated_unreviewed() -> list[dict]:
    """Unreviewed agent-created notes, from the derived index."""
    con = indexer.connect()
    out = []
    for row in con.execute("SELECT path, title, frontmatter FROM notes"):
        meta = json.loads(row["frontmatter"] or "{}")
        if meta.get("origin") == "generated" and not meta.get("reviewed"):
            out.append({
                "path": row["path"],
                "title": row["title"],
                "generated_by": meta.get("generated_by"),
                "generated_at": meta.get("generated_at"),
            })
    con.close()
    return sorted(out, key=lambda n: str(n["generated_at"] or ""), reverse=True)


def promote_generated(rel_path: str, dest_dir: str | None = None,
                      curated: bool = False) -> dict:
    """Mark reviewed; optionally move out of 06-Generated/ into a PARA folder."""
    src = (VAULT_PATH / rel_path).resolve()
    if VAULT_PATH not in src.parents and src != VAULT_PATH:
        raise ValueError("path escapes vault")
    post = frontmatter.load(src)
    post.metadata["reviewed"] = True
    if curated:
        post.metadata["origin"] = "curated"
    target = src
    if dest_dir:
        target_dir = (VAULT_PATH / dest_dir).resolve()
        if VAULT_PATH not in target_dir.parents and target_dir != VAULT_PATH:
            raise ValueError("dest escapes vault")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / src.name
    src.write_text(frontmatter.dumps(post), encoding="utf-8")
    if target != src:
        shutil.move(str(src), str(target))
    return {"path": str(target.relative_to(VAULT_PATH))}


# ---------- opening a note in Obsidian ----------

# Generous about the form of the link, strict about the result: anything that
# isn't a real file inside the vault is a miss (a repo `README.md` mention
# must never open an unrelated note).

_LINK_SUFFIX = re.compile(r"[#|^].*$")      # #heading, |alias, ^block-id


def note_anchor(target: str) -> str:
    """The `#heading` / `^block` a wikilink asked for, or "" (the `|alias` half names no position)."""
    text = (target or "").strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]
    text = text.split("|", 1)[0]
    m = re.search(r"[#^].*$", text)
    return m.group(0).strip() if m else ""


def resolve_note(target: str) -> str:
    """Vault-relative path for a wikilink name, relative path, or abs path.

    Raises FileNotFoundError when the text names nothing in the vault.
    """
    text = (target or "").strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]
    text = _LINK_SUFFIX.sub("", text).strip()
    if not text:
        raise FileNotFoundError("empty link")

    candidates = []
    if text.startswith("/"):
        candidates.append(Path(text))
    else:
        # Wikilinks and prose both drop the extension; paths keep it.
        candidates.append(VAULT_PATH / text)
        if not text.endswith(".md"):
            candidates.append(VAULT_PATH / f"{text}.md")
    for cand in candidates:
        try:
            resolved = cand.resolve()
        except OSError:
            continue
        if resolved.is_file() and VAULT_PATH in resolved.parents:
            return str(resolved.relative_to(VAULT_PATH))

    # Obsidian's rule for a bare `[[name]]`: match the file name anywhere, but
    # only when unique (several `README.md`); a first-listed guess opens the wrong note.
    stem = Path(text).name.removesuffix(".md").casefold()
    con = indexer.connect()
    try:
        rows = [r["path"] for r in con.execute("SELECT path FROM notes")]
    finally:
        con.close()
    hits = [p for p in rows if Path(p).stem.casefold() == stem]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise FileNotFoundError(
            f"{text} names {len(hits)} notes — say which folder")
    raise FileNotFoundError(f"not a note in the vault: {target}")


def obsidian_uri(rel_path: str, anchor: str = "") -> str:
    """`obsidian://open` URI by vault *name* + relative file (survives a symlinked vault).
    `anchor` is the wikilink's `#heading` / `^block`."""
    uri = ("obsidian://open?vault=" + quote(VAULT_PATH.name, safe="")
           + "&file=" + quote(rel_path, safe=""))
    if anchor.startswith("#"):
        uri += "&heading=" + quote(anchor[1:], safe="")
    elif anchor.startswith("^"):
        uri += "&block=" + quote(anchor[1:], safe="")
    return uri


def open_in_obsidian(target: str) -> dict:
    """Resolve `target` and hand its URI to the OS (macOS-only). `open -g` keeps focus in the dashboard."""
    rel = resolve_note(target)
    uri = obsidian_uri(rel, note_anchor(target))
    if sys.platform != "darwin":
        raise RuntimeError("opening in Obsidian is macOS-only")
    macos.open_url(uri, background=True)    # RuntimeError on refusal/timeout
    return {"path": rel, "uri": uri}


# ---------- notes search, one note, pinned (plan view: ⌘K + Vault notes) ----------
#
# Reads index.db only: Obsidian edits show after the watcher's next rebuild.

GENERATED_DIR = "06-Generated/"

# Where a term matched, best first; summed over the query's terms.
_FIELD_WEIGHT = {"title": 0, "path": 1, "body": 3}


def _snippet(body: str, term: str, width: int = 120) -> str:
    """A line of body text around the first hit of `term` ("" if none)."""
    at = body.casefold().find(term)
    if at < 0:
        return ""
    start = max(0, at - width // 3)
    text = " ".join(body[start:start + width].split())
    return ("…" if start else "") + text + ("…" if start + width < len(body) else "")


def search_notes(q: str, limit: int = 20) -> list[dict]:
    """Notes holding every word of `q`, ranked by match field then recency.
    Under two characters returns [] rather than the whole vault."""
    terms = [t for t in q.casefold().split() if t]
    if len("".join(terms)) < 2:
        return []
    with indexer.session() as con:
        rows = con.execute(
            "SELECT n.path, n.title, n.mtime, COALESCE(t.body, '') AS body"
            " FROM notes n LEFT JOIN note_text t USING (path)").fetchall()
    hits = []
    for r in rows:
        fields = {"title": (r["title"] or "").casefold(),
                  "path": r["path"].casefold(), "body": r["body"].casefold()}
        score = 0
        for term in terms:
            where = next((f for f in ("title", "path", "body") if term in fields[f]), None)
            if where is None:
                break
            score += _FIELD_WEIGHT[where]
        else:
            body_hit = next((t for t in terms if t not in fields["title"]), terms[0])
            hits.append((score, -r["mtime"], {
                "path": r["path"], "title": r["title"],
                "folder": os.path.dirname(r["path"]),
                "edited": datetime.fromtimestamp(r["mtime"]).isoformat(timespec="minutes"),
                "snippet": _snippet(r["body"], body_hit),
            }))
    hits.sort(key=lambda h: h[:2])
    return [h[2] for h in hits[:limit]]


def recent_notes(folder: str = "", limit: int = 30) -> list[dict]:
    """Notes under `folder` (vault-relative prefix; "" = all), newest edit first."""
    prefix = folder.strip("/") + "/" if folder.strip("/") else ""
    with indexer.session() as con:
        rows = con.execute(
            "SELECT path, title, mtime FROM notes WHERE path LIKE ? ESCAPE '\\'"
            " ORDER BY mtime DESC LIMIT ?",
            (db.like_contains(prefix)[1:] if prefix else "%", limit)).fetchall()
    return [{"path": r["path"], "title": r["title"], "folder": os.path.dirname(r["path"]),
             "edited": datetime.fromtimestamp(r["mtime"]).isoformat(timespec="minutes"),
             "generated": r["path"].startswith(GENERATED_DIR)}
            for r in rows]


def _titled(con, paths) -> list[dict]:
    titles = {r["path"]: r["title"] for r in con.execute(
        f"SELECT path, title FROM notes WHERE path IN ({','.join('?' * len(paths))})",
        list(paths))} if paths else {}
    return [{"path": p, "title": titles.get(p, Path(p).stem)} for p in paths]


def _excerpt(body: str, chars: int = 600) -> str:
    """The note's opening prose: leading headings and blank lines skipped."""
    lines = [ln for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    text = "\n".join(lines)
    return text if len(text) <= chars else text[:chars].rsplit(" ", 1)[0] + "…"


def note_detail(target: str) -> dict:
    """One note for its tab in the full view: what it is, how it begins, and
    what it links to and from. FileNotFoundError when it isn't in the index."""
    rel = resolve_note(target)
    with indexer.session() as con:
        row = con.execute(
            "SELECT n.path, n.title, n.mtime, n.frontmatter, t.body FROM notes n"
            " LEFT JOIN note_text t USING (path) WHERE n.path=?", (rel,)).fetchone()
        if row is None:
            raise FileNotFoundError(f"not indexed yet: {rel}")
        out_rows = con.execute(
            "SELECT target, dst FROM links WHERE src=? ORDER BY rowid", (rel,)).fetchall()
        linked_to = list(dict.fromkeys(r["dst"] for r in out_rows if r["dst"] and r["dst"] != rel))
        unresolved = [r["target"] for r in out_rows if not r["dst"]]
        linked_from = [r["src"] for r in con.execute(
            "SELECT DISTINCT src FROM links WHERE dst=? AND src<>? ORDER BY src", (rel, rel))]
        links_to = _titled(con, linked_to)
        backlinks = _titled(con, linked_from)
    return {
        "path": rel, "title": row["title"],
        "folder": os.path.dirname(rel),
        "edited": datetime.fromtimestamp(row["mtime"]).isoformat(timespec="minutes"),
        "generated": rel.startswith(GENERATED_DIR),
        "frontmatter": json.loads(row["frontmatter"] or "{}"),
        "excerpt": _excerpt(row["body"] or ""),   # the pin payload's budgeted copy
        "body": row["body"] or "",                 # what the tab renders
        "links_to": links_to,
        "unresolved": unresolved,
        "linked_from": backlinks,
    }


def _bookmarked_files(items: list) -> list[str]:
    out = []
    for it in items or []:
        if it.get("type") == "group":
            out += _bookmarked_files(it.get("items"))
        elif it.get("type") == "file" and str(it.get("path", "")).endswith(".md"):
            out.append(it["path"])
    return out


def pinned_notes() -> dict:
    """Obsidian's own bookmarks, read-only: the vault's settings are the truth
    for what's pinned, so pinning happens in Obsidian and shows up here.

    `.obsidian/bookmarks.json` only exists once something has been bookmarked,
    so a missing file is the ordinary empty case, not an error. Groups are
    flattened; bookmarked folders, searches and headings aren't notes and are
    left out; a bookmark whose note has since moved is dropped too.
    """
    f = VAULT_PATH / ".obsidian" / "bookmarks.json"
    try:
        items = json.loads(f.read_text()).get("items", [])
    except FileNotFoundError:
        return {"pinned": []}
    except (OSError, ValueError, AttributeError) as e:
        return {"pinned": [], "error": f"couldn't read bookmarks.json: {e}"}
    paths = list(dict.fromkeys(_bookmarked_files(items)))
    with indexer.session() as con:
        known = {r["path"] for r in con.execute("SELECT path FROM notes")}
        return {"pinned": _titled(con, [p for p in paths if p in known])}
