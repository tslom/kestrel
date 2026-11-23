"""Documents the console can point into: a dropped PDF, kept, with a ref.

Reading a PDF to text and throwing the file away meant an answer about it
could only *describe* a page — "slide 12 shows the pipeline" — and the user had
to go find slide 12 to check. Keeping the bytes lets the answer point instead:
the model writes `[cite:d3f9a1#p12 "the quoted words"]` and the console opens
page 12 with those words highlighted, or `[page:d3f9a1#p12]` and the page
itself appears in the reply.

What lives where:

- **The file** is `DATA_DIR/docs/<sha256>.pdf` — the source of truth, named by
  content so dropping the same deck twice keeps one copy and one ref.
- **The row** (`docs`) is what's known about it: name, size, and pypdf's text
  page by page. Derived from the file.
- **The ref** is an ordinary citation (`citations.remember("doc", sha)`), so it
  resolves through `/api/cite` like an email does, and one the model made up
  renders dead like any other.

Nothing here parses the PDF. The text comes from the sandboxed worker
attachments.read_upload already runs, and rendering a page is pdf.js in the
browser — the same posture the Desktop tray takes with images: the browser
decodes what the user dragged in, the backend only hands the bytes over.

Page numbers are physical (1 = the first page of the file), which is what
both pypdf and pdf.js count. A deck whose footer says "Lecture 4 — 3" on its
fifth page is page 5 here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import citations, db
from .config import DATA_DIR

DOCS_DIR = DATA_DIR / "docs"

# How much page text rides on the pill. console.PILL_LIMIT (20k) is the floor
# under every pill; this leaves it room for the page markers and the closing
# note, so the cut lands on a page boundary here rather than mid-sentence there.
PILL_BUDGET = 18_000
# One doc_pages read. Large enough for a run of dense textbook pages, small
# enough that "read the whole thing" takes a few calls rather than one that
# fills the window.
READ_BUDGET = 40_000

DOC_REF_RE = re.compile(r"d[0-9a-f]{5,10}")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


_WORD_GAP = re.compile(r"[ \t]*\n[ \t]+\n[ \t]*")
_SPACES = re.compile(r"[ \t]{2,}")


def tidy(page: str) -> str:
    """pypdf's text with the layout noise taken out.

    A Google Docs export comes back one word per line — "large\\n \\nimpacts"
    — which doubled a 5-page essay's pill to 10k characters of mostly
    newlines. A newline-space-newline is a word gap there, never a paragraph
    break (those are a bare newline), so it becomes a space. Nothing else
    moves: the quote check runs on pdf.js's own text layer, not this."""
    page = _WORD_GAP.sub(" ", page or "")
    page = _SPACES.sub(" ", page)
    return "\n".join(line.strip() for line in page.split("\n"))


def keep(filename: str, mime: str, data: bytes, pages: list[str]) -> dict:
    """Store one document and mint its ref. Returns {ref, sha, page_count}.

    Idempotent by content: the same bytes again keep their first filename and
    their ref, and only `opened_at` moves. `pages` is stored tidied, and the
    tidied list comes back for the pill."""
    pages = [tidy(p) for p in pages]
    sha = hashlib.sha256(data).hexdigest()
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    path = DOCS_DIR / f"{sha}.pdf"
    if not path.exists():
        # Through a temp name, so a crash mid-write never leaves a truncated
        # file sitting under the name that says it's complete.
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_bytes(data)
        tmp.replace(path)
    now = _now()
    with db.session() as con:
        with con:
            con.execute(
                "INSERT INTO docs (sha, filename, mime, size_bytes, page_count,"
                " pages, added_at, opened_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(sha) DO UPDATE SET opened_at = excluded.opened_at,"
                "   pages = excluded.pages, page_count = excluded.page_count",
                (sha, filename, mime, len(data), len(pages),
                 json.dumps(pages), now, now))
        ref = citations.remember("doc", sha, filename, db.day_key(), con=con)
    return {"ref": ref, "sha": sha, "page_count": len(pages), "pages": pages}


def _pdf_path(raw: str) -> Path:
    """A PDF the console named by path, checked: under the home folder, a
    real file, a .pdf, under the attachment cap. Raises ValueError otherwise.

    The session can already read anything under home with its own tools, so
    the root isn't a secret being kept; it keeps a stray "/" or a symlink out
    of the docs store, and makes the refusal say why."""
    from .providers import attachments
    p = Path(str(raw or "").strip()).expanduser()
    if not p.is_absolute():
        raise ValueError(f"give an absolute path or ~/…, not {raw!r}")
    p = p.resolve()
    home = Path.home().resolve()
    if home not in p.parents:
        raise ValueError(f"{p} is outside the home folder")
    if p.suffix.lower() != ".pdf":
        raise ValueError(f"{p.name} isn't a PDF")
    if not p.is_file():
        raise ValueError(f"no file at {p}")
    if p.stat().st_size > attachments.ATTACHMENT_MAX_BYTES:
        raise ValueError(f"{p.name} is over the "
                         f"{attachments.ATTACHMENT_MAX_BYTES // 1_048_576}MB cap")
    return p


async def open_path(raw: str) -> dict:
    """Keep a PDF on disk as a document, as if it had been dropped: the same
    sandboxed read, the same store, the same ref (a file opened twice, or
    opened and dropped, is one document). Returns keep()'s dict plus `name`.

    This is what lets "go through handouts 9–16 in my Downloads" point at
    pages: read with pdftotext from the shell, the handouts had no ref, so
    nothing in the reply could open them beside it (11-07)."""
    import asyncio
    from . import agent
    from .providers import attachments
    p = await asyncio.to_thread(_pdf_path, raw)
    data = await asyncio.to_thread(p.read_bytes)
    with agent.interactive():
        out = await attachments.read_upload(p.name, data, "application/pdf")
    pages = out.get("pages")
    if not pages:
        raise ValueError(f"couldn't read {p.name} as a PDF ({out.get('note') or out.get('kind')})")
    kept = await asyncio.to_thread(keep, p.name, "application/pdf", data, pages)
    return {**kept, "name": p.name}


def _row(ref: str):
    ref = (ref or "").strip().lower()
    if not DOC_REF_RE.fullmatch(ref):
        return None
    with db.session() as con:
        return con.execute(
            "SELECT d.* FROM cite_refs c JOIN docs d ON d.sha = c.target"
            " WHERE c.ref = ? AND c.kind = 'doc'", (ref,)).fetchone()


def recent(limit: int = 20) -> list[dict]:
    """[{ref, name, pages, opened_at}] newest-opened first; the file must still exist."""
    with db.session() as con:
        rows = con.execute(
            "SELECT c.ref, d.sha, d.filename, d.page_count, d.opened_at"
            " FROM docs d JOIN cite_refs c ON c.target = d.sha AND c.kind = 'doc'"
            " ORDER BY d.opened_at DESC, c.ref LIMIT ?", (limit * 2,)).fetchall()
    out, seen = [], set()
    for r in rows:
        if r["sha"] in seen or not (DOCS_DIR / f"{r['sha']}.pdf").is_file():
            continue
        seen.add(r["sha"])
        out.append({"ref": r["ref"], "name": r["filename"], "pages": r["page_count"],
                    "opened_at": r["opened_at"]})
    return out[:limit]


def file_for(ref: str) -> Path | None:
    """The stored PDF behind a ref, or None if the ref or the file is gone."""
    row = _row(ref)
    if row is None:
        return None
    path = DOCS_DIR / f"{row['sha']}.pdf"
    return path if path.is_file() else None


def _marked(pages: list[str], numbers: list[int], budget: int) -> tuple[str, int]:
    """Pages under `[pN]` markers until the budget runs out.

    Returns (text, how many of `numbers` made it). A page is never cut in
    half: a quote has to come off one page, and half a page makes that a guess."""
    out: list[str] = []
    used = 0
    taken = 0
    for n in numbers:
        body = (pages[n - 1] or "").strip() or "(no extractable text on this page)"
        block = f"[p{n}]\n{body}"
        if out and used + len(block) > budget:
            break
        out.append(block[:budget])
        used += len(block) + 2
        taken += 1
    return "\n\n".join(out), taken


def pill_text(ref: str, pages: list[str]) -> str:
    """The text a dropped document's pill carries: its pages under markers,
    and, when they don't all fit, which ones didn't and how to read them."""
    text, taken = _marked(pages, list(range(1, len(pages) + 1)), PILL_BUDGET)
    if taken < len(pages):
        text += (f"\n\n[pages {taken + 1}–{len(pages)} of {len(pages)} are not "
                 f"included here — read them with doc_pages(ref=\"{ref}\", "
                 f"pages=\"{taken + 1}-{min(len(pages), taken + 20)}\")]")
    return text


def parse_spec(spec: str, count: int) -> list[int]:
    """'12', '3-7', '1,4,9-11' → page numbers, in order, in range, deduped."""
    out: list[int] = []
    for part in str(spec or "").replace(" ", "").split(","):
        if not part:
            continue
        a, _, b = part.partition("-")
        try:
            lo = int(a)
            hi = int(b) if b else lo
        except ValueError:
            raise ValueError(f"can't read page spec {part!r} — use '12', '3-7' or '1,4,9-11'")
        if hi < lo:
            lo, hi = hi, lo
        for n in range(max(1, lo), min(count, hi) + 1):
            if n not in out:
                out.append(n)
    return out


def read_pages(ref: str, spec: str) -> dict:
    """Pages of a kept document for the console: {name, page_count, text, more}.

    Raises LookupError for a ref with no document behind it, ValueError for a
    spec that names no page in it."""
    row = _row(ref)
    if row is None:
        raise LookupError(f"no document {ref!r} — use the ref from the file's "
                          "<document> tag")
    pages = json.loads(row["pages"])
    numbers = parse_spec(spec, len(pages))
    if not numbers:
        raise ValueError(f"{row['filename']} has {len(pages)} pages; "
                         f"{spec!r} names none of them")
    text, taken = _marked(pages, numbers, READ_BUDGET)
    rest = numbers[taken:]
    return {"name": row["filename"], "page_count": len(pages), "text": text,
            "more": (f"{rest[0]}-{rest[-1]}" if rest else "")}
