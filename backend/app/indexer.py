"""Derived SQLite index over the vault.

Markdown is the source of truth; index.db is a disposable cache rebuilt FROM
the files, never written here first.
"""

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote

import frontmatter

from .config import VAULT_PATH, TASKS_VIEW_FILE, INDEX_DB

TASK_RE = re.compile(r"^\s*[-*]\s*\[(?P<state>[ xX])\]\s*(?P<text>.+)$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    path TEXT PRIMARY KEY,          -- vault-relative path
    title TEXT,
    mtime REAL,
    frontmatter TEXT                -- raw YAML frontmatter as JSON
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    file TEXT,
    line INTEGER,
    text TEXT,
    done INTEGER
);
-- The note's body, frontmatter stripped: what notes search reads (plan view's
-- ⌘K notes group and the Vault notes view).
CREATE TABLE IF NOT EXISTS note_text (
    path TEXT PRIMARY KEY,
    body TEXT
);
-- One row per link out of a note. `dst` is the vault path it resolves to, or
-- NULL for a link to a note that doesn't exist (yet) or names several.
CREATE TABLE IF NOT EXISTS links (
    src TEXT NOT NULL,
    target TEXT NOT NULL,           -- as written, minus #heading / |alias
    dst TEXT
);
CREATE INDEX IF NOT EXISTS links_dst ON links (dst);
"""

# [[Note]], [[folder/Note#Heading|alias]], ![[embed]] — and [text](Note.md).
WIKILINK_RE = re.compile(r"!?\[\[([^\]\n]+?)\]\]")
MDLINK_RE = re.compile(r"\[[^\]\n]*\]\(([^)\s]+?\.md)(?:#[^)]*)?\)")
_LINK_SUFFIX = re.compile(r"[#|^].*$")


def link_targets(body: str) -> list[str]:
    """Every note a body links to, as written, in order, de-duplicated.

    A wikilink with a non-.md extension (image, PDF) isn't a note link.
    """
    out: list[str] = []
    for m in WIKILINK_RE.finditer(body):
        # In a markdown table the alias pipe is escaped (`[[Note\|alias]]`).
        t = _LINK_SUFFIX.sub("", m.group(1)).strip().removesuffix("\\")
        if t and (not Path(t).suffix or t.endswith(".md")):
            out.append(t)
    for m in MDLINK_RE.finditer(body):
        t = unquote(m.group(1)).strip()
        if "://" not in t:
            out.append(t)
    return list(dict.fromkeys(out))


def resolve_link(target: str, src: str, paths: set[str],
                 by_stem: dict[str, list[str]]) -> str | None:
    """The vault path a link lands on, by vault_views.resolve_note's rules (so
    list and click agree): exact path (with or without .md), then a bare name
    unique across the vault; ambiguous -> None. Markdown links (ending .md) are
    also tried against the linking note's folder; wikilinks never are."""
    t = target.removeprefix("/")
    cands = [t, f"{t}.md"]
    if t.endswith(".md"):
        cands.append(os.path.normpath(os.path.join(os.path.dirname(src), t)))
    for cand in cands:
        if cand in paths:
            return cand
    hits = by_stem.get(Path(t).name.removesuffix(".md").casefold(), [])
    return hits[0] if len(hits) == 1 else None


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(INDEX_DB)
    con.execute("PRAGMA busy_timeout=5000")  # watcher rebuilds race widget reads
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


@contextmanager
def session():
    """connect() that always closes (db.session()'s twin). A rebuild walks the
    whole vault in one connection; a leaked handle held the write lock."""
    con = connect()
    try:
        yield con
    finally:
        con.close()


def index_note(con: sqlite3.Connection, path: Path) -> bool:
    rel = str(path.relative_to(VAULT_PATH))
    try:
        post = frontmatter.load(path)
    except Exception:
        return False  # unparseable file: skip, never crash the index
    meta = {k: _jsonable(v) for k, v in post.metadata.items()}
    con.execute(
        "INSERT OR REPLACE INTO notes (path, title, mtime, frontmatter) VALUES (?,?,?,?)",
        (rel, path.stem, path.stat().st_mtime, json.dumps(meta)),
    )
    con.execute("INSERT OR REPLACE INTO note_text (path, body) VALUES (?,?)",
                (rel, post.content))
    # Resolved in rebuild(), once every note is known.
    con.executemany("INSERT INTO links (src, target) VALUES (?,?)",
                    [(rel, t) for t in link_targets(post.content)])

    # Checkbox tasks in notes, for a by-hand reconcile sweep; the generated tasks.md is skipped.
    if rel != TASKS_VIEW_FILE:
        for i, line in enumerate(post.content.splitlines(), start=1):
            m = TASK_RE.match(line)
            if m:
                con.execute(
                    "INSERT INTO tasks (file, line, text, done) VALUES (?,?,?,?)",
                    (rel, i, m.group("text").strip(), int(m.group("state").lower() == "x")),
                )
    return True


def _jsonable(v):
    """JSON-safe, all the way down: one nested YAML date must not flatten its
    container to a str (office-hours `except: [{date: …}]` silently vanished)."""
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    try:
        json.dumps(v)
        return v
    except TypeError:
        return str(v)


def rebuild() -> dict:
    """Full rebuild. Fast enough for a personal vault; optimize later if needed."""
    if not VAULT_PATH.exists():
        raise FileNotFoundError(f"VAULT_PATH does not exist: {VAULT_PATH}")
    with session() as con, con:
        con.execute("DROP TABLE IF EXISTS daily")  # removed feature; index.db is disposable
        con.execute("DELETE FROM notes")
        con.execute("DELETE FROM tasks")
        con.execute("DELETE FROM note_text")
        con.execute("DELETE FROM links")
        count = 0
        skipped = []
        for path in VAULT_PATH.rglob("*.md"):
            if any(part.startswith(".") for part in path.parts):
                continue  # skip .obsidian, .claude, .trash, etc.
            if index_note(con, path):
                count += 1
            else:
                # Unparseable frontmatter: invisible to the dashboard, so name it.
                skipped.append(str(path.relative_to(VAULT_PATH)))

        paths = {r["path"] for r in con.execute("SELECT path FROM notes")}
        by_stem: dict[str, list[str]] = {}
        for p in paths:
            by_stem.setdefault(Path(p).stem.casefold(), []).append(p)
        con.executemany(
            "UPDATE links SET dst=? WHERE rowid=?",
            [(resolve_link(r["target"], r["src"], paths, by_stem), r["rowid"])
             for r in con.execute("SELECT rowid, src, target FROM links").fetchall()])
    return {"indexed_notes": count, "skipped": skipped}
