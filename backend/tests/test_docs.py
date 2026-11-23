"""Documents the console can point into (app/docs.py).

What matters is the chain from a dropped PDF to a citation that lands: the
worker keeps every page in its slot (so page 12 is page 12), the drop keeps
the file and mints a `d…` ref that resolves like any other citation, the
prompt carries that ref outside the untrusted fence, and the bytes come back
by ref and only by ref.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import citations, console, db, docs, voice
from app.providers import attachments


def make_pdf(pages: list[str]) -> bytes:
    """A real PDF with one line of Helvetica text per page ("" = blank page).

    Built by hand because pypdf reads text but can't write it, and nothing
    else that could is (or should be) a dependency."""
    objs: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font = 3 + 2 * n
    for i, text in enumerate(pages):
        esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = (f"BT /F1 12 Tf 72 720 Td ({esc}) Tj ET" if text else "").encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
                    f" /Resources << /Font << /F1 {font} 0 R >> >>"
                    f" /Contents {4 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream
                    + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for k, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % k + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref))
    return bytes(out)


@pytest.fixture
def docs_dir(tmp_path, monkeypatch):
    d = tmp_path / "docs"
    monkeypatch.setattr(docs, "DOCS_DIR", d)
    return d


@pytest.fixture
def api(dbfile, docs_dir):
    app = FastAPI()
    app.include_router(console.router)
    from app.routes import cite
    app.include_router(cite.router)
    return TestClient(app)


# ---------- the worker ----------

def test_the_worker_keeps_every_page_in_its_slot():
    """A blank page keeps its place: dropping it would make every later
    citation one page early, which the highlight would then fail to find."""
    out = attachments._extract_local(
        make_pdf(["Pipelining overview", "", "Hazards and stalls"]),
        "application/pdf")
    assert out["status"] == "ok"
    assert [p.strip() for p in out["pages"]] == [
        "Pipelining overview", "", "Hazards and stalls"]


def test_a_long_document_does_not_wedge_the_pipe():
    """Per-page text runs past the 64KB pipe buffer. The parent used to read
    stdout only after the child exited, so a child blocked on a full pipe sat
    until the wall clock killed a parse that had already finished."""
    pages = [f"page {i} " + "lorem ipsum " * 300 for i in range(60)]
    out = attachments._extract_local(make_pdf(pages), "application/pdf")
    assert out["status"] == "ok"
    assert len(out["pages"]) == 60
    assert out["pages"][59].startswith("page 59")


# ---------- the drop ----------

def test_a_dropped_pdf_is_kept_and_gets_a_ref(api, docs_dir):
    data = make_pdf(["Lecture 4: caches", "Direct-mapped caches index by low bits"])
    body = api.post("/api/console/upload",
                    files={"file": ("lec4.pdf", data, "application/pdf")}).json()
    ref = body["doc"]["ref"]
    assert ref.startswith("d") and body["doc"]["pages"] == 2
    assert "pages" not in body                   # not for the pill
    assert "[p1]\nLecture 4: caches" in body["text"]
    assert "[p2]\nDirect-mapped caches" in body["text"]
    assert "2 pages" in body["note"]
    assert len(list(docs_dir.glob("*.pdf"))) == 1

    # It resolves like any other citation, and a second drop is the same doc.
    got = citations.resolve(ref)
    assert got["found"] and got["kind"] == "doc" and got["title"] == "lec4.pdf"
    again = api.post("/api/console/upload",
                     files={"file": ("copy.pdf", data, "application/pdf")}).json()
    assert again["doc"]["ref"] == ref
    assert len(list(docs_dir.glob("*.pdf"))) == 1


def test_the_file_comes_back_by_ref_only(api):
    data = make_pdf(["hello"])
    ref = api.post("/api/console/upload",
                   files={"file": ("a.pdf", data, "application/pdf")}).json()["doc"]["ref"]
    r = api.get(f"/api/cite/{ref}/file")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content == data
    # An email ref, an invented ref, and a path are all nothing.
    assert api.get("/api/cite/d000000/file").status_code == 404
    assert api.get("/api/cite/e7a3f1/file").status_code == 404
    assert api.get("/api/cite/..%2Fapp.db/file").status_code == 404


def test_a_text_file_is_still_not_kept(api, docs_dir):
    body = api.post("/api/console/upload",
                    files={"file": ("n.txt", b"plain", "text/plain")}).json()
    assert "doc" not in body
    assert not docs_dir.exists()


# ---------- the pill and the read ----------

def test_the_pill_cuts_on_a_page_and_says_how_to_read_the_rest():
    pages = ["x" * 7000 for _ in range(5)]
    text = docs.pill_text("d3f9a1", pages)
    assert "[p2]" in text and "[p3]" not in text
    assert 'doc_pages(ref="d3f9a1", pages="3-5")' in text
    assert len(text) < console.PILL_LIMIT


def test_tidy_joins_a_word_per_line_export():
    """Google Docs PDFs come out of pypdf one word per line."""
    assert docs.tidy("large\n \nimpacts\n \nin  society.\nNew para") == \
        "large impacts in society.\nNew para"


def test_parse_spec():
    assert docs.parse_spec("12", 20) == [12]
    assert docs.parse_spec("3-5, 1, 4", 20) == [3, 4, 5, 1]
    assert docs.parse_spec("18-25", 20) == [18, 19, 20]
    assert docs.parse_spec("7-5", 20) == [5, 6, 7]
    with pytest.raises(ValueError):
        docs.parse_spec("seven", 20)


def test_read_pages(dbfile, docs_dir):
    kept = docs.keep("r.pdf", "application/pdf", b"%PDF-fake",
                     ["one", "two", "three"])
    out = docs.read_pages(kept["ref"], "2-3")
    assert out["text"] == "[p2]\ntwo\n\n[p3]\nthree"
    assert out["more"] == ""
    with pytest.raises(ValueError):
        docs.read_pages(kept["ref"], "9")
    with pytest.raises(LookupError):
        docs.read_pages("d000000", "1")


def test_doc_pages_restates_the_ref_outside_the_fence(dbfile, docs_dir):
    """A live session read pages 31-43 through doc_pages and cited none of
    them; the result carried the text but not how to point at it."""
    import asyncio
    from app import console_tools as ct
    kept = docs.keep("lec.pdf", "application/pdf", b"%PDF-fake", ["a", "b"])
    out = asyncio.run(ct.doc_pages.handler({"ref": kept["ref"], "pages": "2"}))
    text = out["content"][0]["text"]
    head, _, _ = text.partition("<untrusted_attachment")
    assert f'<document ref="{kept["ref"]}"' in head
    assert f'[page:{kept["ref"]}#pN]' in head
    assert "[p2]\nb" in text


def test_doc_pages_opens_a_pdf_on_disk_by_path(dbfile, docs_dir, tmp_path, monkeypatch):
    """Handouts read with pdftotext had no ref, so the reply couldn't show a
    page (11-07). By path, the file is kept like a drop — same ref as
    dropping the same bytes — and the result is citable."""
    import asyncio
    from app import console_tools as ct
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    data = make_pdf(["Recursion base case", "Tail recursion"])
    (home / "Downloads" / "h9.pdf").write_bytes(data)

    out = asyncio.run(ct.doc_pages.handler({"path": "~/Downloads/h9.pdf", "pages": "2"}))
    text = out["content"][0]["text"]
    assert not out.get("is_error"), text
    ref = text.split('<document ref="')[1].split('"')[0]
    assert docs.DOC_REF_RE.fullmatch(ref)
    assert "[p2]\nTail recursion" in text
    assert docs.file_for(ref).read_bytes() == data
    # Dropping the same bytes later is the same document.
    assert docs.keep("other.pdf", "application/pdf", data, ["x", "y"])["ref"] == ref

    for bad, why in [(str(tmp_path / "out.pdf"), "outside the home"),
                     ("~/Downloads/missing.pdf", "no file"),
                     ("Downloads/h9.pdf", "absolute"),
                     ("~/Downloads/notes.txt", "isn't a PDF")]:
        (tmp_path / "out.pdf").write_bytes(data)
        (home / "Downloads" / "notes.txt").write_text("hi")
        got = asyncio.run(ct.doc_pages.handler({"path": bad}))
        assert got.get("is_error") and why in got["content"][0]["text"], (bad, got)
    assert asyncio.run(ct.doc_pages.handler({})).get("is_error")


# ---------- the prompt ----------

def test_the_ref_rides_outside_the_fence():
    """The ref is ours; the page text is the file's. A document that prints
    `ref="d…"` in its own text is still inside the untrusted fence."""
    prompt = console.build_prompt({"text": "quiz me", "context": [{
        "kind": "file", "label": "lec4.pdf", "data": "[p1]\nCaches",
        "doc": {"ref": "d3f9a1", "pages": 1}}]})
    head, _, rest = prompt.partition("<untrusted_attachment")
    assert '<document ref="d3f9a1"' in head and 'pages="1"' in head
    assert "</document>" in rest


def test_a_malformed_doc_ref_is_dropped():
    prompt = console.build_prompt({"text": "hi", "context": [{
        "kind": "file", "label": "x.pdf", "data": "text",
        "doc": {"ref": 'd1" onload="x', "pages": 1}}]})
    assert "<document" not in prompt and "<untrusted_attachment" in prompt


# ---------- the markers ----------

def test_refs_in_reads_a_document_citation():
    text = 'Caches index by low bits [cite:d3f9a1#p12 "index by the low bits"].'
    assert citations.refs_in(text) == ["d3f9a1"]


def test_markers_are_not_read_aloud():
    said = voice.speakable(
        'It stalls on a hazard [cite:d3f9a1#p12 "stalls the pipeline"].\n'
        "[page:d3f9a1#p12]\nThat's the diagram.")
    assert "d3f9a1" not in said and "page:" not in said
    assert "stalls on a hazard" in said and "diagram" in said


def test_recent_docs_lists_what_was_kept_newest_first(api, docs_dir):
    """The document pane's picker: any kept PDF, not only what a reply cited."""
    up = lambda name, text: api.post(
        "/api/console/upload", files={"file": (name, make_pdf([text]), "application/pdf")}).json()["doc"]["ref"]
    a = up("hw8.pdf", "Homework 8")
    b = up("handout13.pdf", "Lecture 13")
    with db.session() as con, con:
        con.execute("UPDATE docs SET opened_at = '2000-01-01T00:00:00' WHERE filename = 'hw8.pdf'")
    got = api.get("/api/cite/docs/recent").json()["docs"]
    assert [d["ref"] for d in got] == [b, a]
    assert got[0]["name"] == "handout13.pdf" and got[0]["pages"] == 1
    # A file gone from disk isn't offered.
    for p in docs_dir.glob("*.pdf"):
        p.unlink()
    assert api.get("/api/cite/docs/recent").json()["docs"] == []
