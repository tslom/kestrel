"""Parses one untrusted file, in a process that is expected to die.

Run as a child of providers/attachments.py, never imported by the backend:

    python -I app/providers/_extract_worker.py <path> <mime>

Standalone on purpose — it imports nothing from `app`, so it can run under
`python -I` (which drops cwd from sys.path) with no package on the path.

Everything here assumes the input is hostile. A PDF that arrived by text
message is attacker-authored in the literal sense — whoever sent it chose
every byte — so the design goal is not "parse safely" (no parser promises
that) but "contain the blast": limits are set before the file is opened, and
anything that trips one takes down a child process holding nothing.

What the limits actually stop:
  - RLIMIT_CPU     parser infinite loops and quadratic blowup, and most
                   decompression bombs — inflating gigabytes burns CPU, so
                   this fires well before the machine notices.
  - RLIMIT_FSIZE=0 no file can receive a single byte (EFBIG at flush, verified
                   — creating an empty file still succeeds, so this bounds
                   content, not existence). A parser bug that becomes an
                   arbitrary-write primitive has nowhere to put a payload.
  - RLIMIT_NPROC   no fanning out into more processes.
  - RLIMIT_CORE=0  no core dumps of the file's contents.

Memory is NOT capped here, and that's a platform fact rather than a choice:
Darwin accepts RLIMIT_AS/DATA/RSS in the API and then refuses every attempt to
lower them ("current limit exceeds maximum limit" even when the hard limit
reads as unlimited) — `ulimit -v` is likewise a no-op on macOS. So the memory
ceiling is enforced from the outside, by the parent watching this process's
RSS and killing it (attachments.py `_run_guarded`). Everything settable is
still set here, because the ones that do work are worth having and this file
should stay correct if it ever runs on Linux, where RLIMIT_AS does work.

Each limit is applied independently: one unsettable limit must not skip the
rest, which is exactly the bug that a single try-block around all of them
would create.

What they don't stop: a memory-safety exploit in a C extension that achieves
code execution *within* these limits. Nothing here is a security boundary
against that — it's a macOS process with the user's normal privileges. The
mitigation for that class is upstream, in what we're willing to parse at all:
only pypdf (pure Python), plain text, the JSON inside a .pkpass, and the XML
inside a .docx (expat, with any DTD refused before it parses) run here.
Image decoding — the whole libjpeg/libwebp/libtiff CVE family — deliberately
never happens locally; see attachments.py, which sends image bytes to the model
instead of decoding them. That rule is why a pass gives up its text but not its
artwork: the same zip holds PNGs this will not open. A Word file's pictures go
one step further — their bytes are handed back undecoded, signature-checked
only, for the parent to show a vision model.

stdout is one JSON object and nothing else, so a parser that scribbles on
stdout can't forge a result; the parent treats unparseable output as failure.
"""

import json
import resource
import sys

# Generous enough for a real 50MB scanned PDF's page tree, small enough that a
# bomb hits it in well under a second.
MEM_LIMIT_BYTES = 1024 * 1024 * 1024
CPU_LIMIT_SECONDS = 20
TEXT_CAP = 20_000
# Per-page text for a document read page by page. A 300-page textbook chapter
# runs ~600k characters, so these bound the JSON the parent reads back rather
# than any real deck or reading; the CPU limit still bounds the work.
PAGES_MAX = 1000
PAGES_CAP = 1_500_000
PAGE_CAP = 20_000

# ---- Apple Wallet passes ----------------------------------------------------
# Both spellings of the type, because the two carriers name the same file
# differently: Gmail repeats the sender's header, chat.db records the UTI. Kept
# in step with attachments.PKPASS_MIMES by hand — this module imports nothing
# from `app` (see the docstring), so the duplication is the price of `-I`.
PKPASS_MIMES = ("application/vnd.apple.pkpass", "com.apple.pkpass")

# pass.json is a form, not a document: a few dozen short fields. A megabyte is
# already absurd for one, and well under what the CPU limit would catch.
PKPASS_JSON_CAP = 1_000_000

# A pass carries exactly one of these, and it names what the thing *is*.
PASS_STYLES = {"boardingPass": "boarding pass", "eventTicket": "event ticket",
               "coupon": "coupon", "storeCard": "store card",
               "generic": "pass"}

# Wallet's own display order, front of the pass to back.
PASS_SECTIONS = ("headerFields", "primaryFields", "secondaryFields",
                 "auxiliaryFields", "backFields")

# The barcode message is the ticket — an order id, a URL, sometimes a signed
# blob. Worth keeping, not worth 20KB of it.
BARCODE_CAP = 500


def _limit(name: str, value: int) -> bool:
    """Best-effort setrlimit. Returns whether it took.

    Unsettable is normal, not exceptional — see the module docstring on
    Darwin's memory limits — so a failure here is skipped rather than raised,
    and never prevents the next limit from being applied."""
    which = getattr(resource, name, None)
    if which is None:
        return False
    try:
        resource.setrlimit(which, (value, value))
        return True
    except (ValueError, OSError):
        return False


def _harden() -> None:
    """Apply every limit that this platform will accept, before the file is
    opened. FSIZE and NPROC go first so the child loses the ability to write
    or fork even if everything after them fails."""
    _limit("RLIMIT_FSIZE", 0)
    _limit("RLIMIT_NPROC", 0)
    _limit("RLIMIT_CORE", 0)
    _limit("RLIMIT_CPU", CPU_LIMIT_SECONDS)
    _limit("RLIMIT_AS", MEM_LIMIT_BYTES)   # Linux only; a no-op on Darwin


def _pdf(path: str) -> tuple[str, str, list[str]]:
    """(status, text, pages). `text` is the first TEXT_CAP characters, as it
    always was; `pages` is every page's own text, in order, for a document the
    console can cite by page (app/docs.py). A page that fails to extract keeps
    its slot as "" — dropping it would renumber every page after it, and a
    page number is the one thing a citation into this has to get right."""
    from pypdf import PdfReader

    reader = PdfReader(path)
    if reader.is_encrypted:
        # Empty-password decrypt covers the common "encrypted but not really"
        # case (owner password only). A real password means no text, not an
        # error worth retrying.
        try:
            if not reader.decrypt(""):
                return "encrypted", "", []
        except Exception:
            return "encrypted", "", []
    pages: list[str] = []
    total = 0
    for page in reader.pages:
        if len(pages) >= PAGES_MAX or total >= PAGES_CAP:
            break
        try:
            chunk = page.extract_text() or ""
        except Exception:
            chunk = ""  # one broken page shouldn't lose the other forty
        pages.append(chunk[:PAGE_CAP])
        total += len(pages[-1])
    return "ok", "\n".join(pages)[:TEXT_CAP], pages


def _scalar(value) -> str:
    """One pass value as text, or "" if it isn't one.

    Everything in pass.json was typed by whoever made the pass, so a field's
    `value` is a string, a number, an ISO date — or a list, because nothing
    validates it. Containers are dropped rather than str()'d: a rendered
    `{'k': ...}` in the middle of a ticket is noise, not information."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return value.strip() if isinstance(value, str) else ""


def _pass_fields(section) -> list[str]:
    """One `label: value` line per field, in the order the pass lists them.

    The pairing is reproduced as-authored even where it reads oddly — real
    passes put "August 20" under the label "12:15 pm" — because the alternative
    is guessing which half is the label, and a wrong guess quietly rewrites a
    ticket. `attributedValue` is the fallback rather than the preference: it's
    the same text with a link wrapped around it, and one pass here fills it
    with the word "hello"."""
    out: list[str] = []
    if not isinstance(section, list):
        return out
    for field in section:
        if not isinstance(field, dict):
            continue
        value = _scalar(field.get("value")) or _scalar(field.get("attributedValue"))
        if not value:
            continue
        label = _scalar(field.get("label")) or _scalar(field.get("key"))
        out.append(f"{label}: {value}" if label else value)
    return out


def _pass_text(p: dict) -> str:
    """pass.json → the pass as a human would read it.

    Skips `authenticationToken` and `webServiceURL` deliberately: they are the
    credential for the issuer's update endpoint, they say nothing about the
    event, and extracted text ends up in prompts."""
    style = next((s for s in PASS_STYLES if isinstance(p.get(s), dict)), None)
    lines: list[str] = []

    body = p.get(style) if style else None
    if isinstance(body, dict):
        for section in PASS_SECTIONS:
            lines += _pass_fields(body.get(section))

    if isinstance(p.get("locations"), list):
        for loc in p["locations"][:5]:
            if isinstance(loc, dict) and (text := _scalar(loc.get("relevantText"))):
                lines.append(f"Location: {text}")

    # `barcodes` is the current spelling and `barcode` the pre-iOS 9 one; issuers
    # still emit either, and one pass here has only the old form.
    codes = p.get("barcodes") if isinstance(p.get("barcodes"), list) else [p.get("barcode")]
    for code in codes[:4]:
        if not isinstance(code, dict):
            continue
        if message := _scalar(code.get("message")):
            fmt = _scalar(code.get("format")).replace("PKBarcodeFormat", "")
            lines.append(f"Barcode ({fmt or 'unknown'}): {message[:BARCODE_CAP]}")

    title = " — ".join(t for t in (_scalar(p.get("organizationName")),
                                   _scalar(p.get("description"))) if t)
    if not (title or style or lines):
        # Valid JSON in the right place, saying nothing — a `{}` or a stub.
        # Returning "" lets main() file it as `empty` rather than as a pass
        # whose entire content is the word "pass".
        return ""

    meta = [PASS_STYLES.get(style, "pass")]
    if p.get("voided") is True:
        meta.append("VOIDED")
    if relevant := _scalar(p.get("relevantDate")):
        meta.append(f"relevant {relevant}")
    if expires := _scalar(p.get("expirationDate")):
        meta.append(f"expires {expires}")

    # backFields habitually repeat the front of the pass verbatim.
    seen: set[str] = set()
    lines = ([title] if title else []) + [" · ".join(meta)] + lines
    return "\n".join(ln for ln in lines if not (ln in seen or seen.add(ln)))


def _pkpass(path: str) -> tuple[str, str]:
    """Read the one file in a pass that is text.

    A .pkpass is a zip: pass.json, artwork, a manifest of SHA-1s, and Apple's
    detached signature. Only pass.json is opened — the PNGs would mean a local
    image decode, which is the thing this codebase doesn't do (see
    attachments.py) — and the signature is not checked, because it answers "did
    the issuer write this", which is a different question from "what does it
    say". A forged pass is a forged pass; it is not a way into this process."""
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            with archive.open("pass.json") as fh:
                # Bounded read, not a check against ZipInfo.file_size: the
                # declared size is a number the sender wrote in the archive, and
                # believing it is how a 4KB file becomes 4GB of memory. One byte
                # past the cap is enough to know it's over.
                raw = fh.read(PKPASS_JSON_CAP + 1)
    except KeyError:
        return "unsupported", ""        # a zip, but not a pass
    except (zipfile.BadZipFile, OSError, EOFError):
        return "unsupported", ""        # or not a zip at all
    if len(raw) > PKPASS_JSON_CAP:
        return "too_big", ""
    try:
        data = json.loads(raw.decode("utf-8-sig", errors="replace"))
    except json.JSONDecodeError:
        return "error", ""
    if not isinstance(data, dict):
        return "error", ""
    return "ok", _pass_text(data)[:TEXT_CAP]


# ---- Word (.docx) -----------------------------------------------------------
# Kept in step with attachments.DOCX_MIME by hand, like PKPASS_MIMES above.
DOCX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document")

# document.xml for a real handout is tens of KB; the 50-page one here is 54KB.
DOCX_XML_CAP = 5_000_000

# Embedded pictures are handed back as bytes for the parent to show a vision
# model — never decoded here (see the module docstring). These bound the JSON
# on stdout (attachments._STDOUT_CAP is 16MB, and base64 adds a third) and the
# number of vision calls one file can bill. Per-image matches the parent's
# ATTACHMENT_VISION_MAX_BYTES, which it re-checks anyway.
DOCX_IMAGE_MAX = 12
DOCX_IMAGE_CAP = 3_500_000
DOCX_IMAGES_TOTAL_CAP = 8_000_000

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_V = "{urn:schemas-microsoft-com:vml}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _image_sig(data: bytes) -> bool:
    """Signature check only, the same four formats attachments._sniff accepts.
    An EMF/WMF/TIFF in a Word file is skipped, not converted."""
    return (data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff"
            or data[:6] in (b"GIF87a", b"GIF89a")
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


def _bounded(archive, name: str, cap: int) -> bytes | None:
    """A member's bytes, or None past the cap. Read, not trusted from
    ZipInfo.file_size — see _pkpass on why the declared size is no answer."""
    with archive.open(name) as fh:
        raw = fh.read(cap + 1)
    return None if len(raw) > cap else raw


def _safe_xml(raw: bytes):
    """Parse, refusing any DTD. OOXML never carries one, so a DOCTYPE in a
    .docx is either damage or an entity-expansion attempt, and either way
    the answer is the same."""
    import xml.etree.ElementTree as ET
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise ValueError("DTD in OOXML")
    return ET.fromstring(raw)


def _docx(path: str) -> tuple[str, str, list[dict]]:
    """(status, text, images) for a Word file.

    Text is the body in reading order: paragraphs one per line, a table row
    per line with its cells joined by " | " — a nutrition table's row is the
    unit that means something. Each embedded picture becomes an "[image N]"
    marker where it sits, and its raw bytes come back in `images` for the
    parent to read; a handout that is all labels (three of the four a USC
    dietitian sent on 10-16) is otherwise five item names and nothing else."""
    import base64
    import zipfile

    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, EOFError):
        return "unsupported", "", []
    with archive:
        try:
            raw = _bounded(archive, "word/document.xml", DOCX_XML_CAP)
        except KeyError:
            return "unsupported", "", []    # a zip, but not a Word file
        if raw is None:
            return "too_big", "", []
        body = _safe_xml(raw)

        targets: dict[str, str] = {}
        try:
            rels = _bounded(archive, "word/_rels/document.xml.rels",
                            DOCX_XML_CAP)
        except KeyError:
            rels = None
        if rels:
            for rel in _safe_xml(rels).iter(f"{_PKG_REL}Relationship"):
                if rel.get("TargetMode") == "External":
                    continue                # a linked picture has no bytes here
                target = rel.get("Target") or ""
                targets[rel.get("Id") or ""] = (
                    target.lstrip("/") if target.startswith("/")
                    else "word/" + target)

        numbered: dict[str, int] = {}
        images: list[dict] = []
        budget = [DOCX_IMAGES_TOTAL_CAP]

        def picture(rid: str | None) -> str:
            if not rid or rid not in targets:
                return ""
            if rid not in numbered:
                numbered[rid] = len(numbered) + 1
                n = numbered[rid]
                if len(images) < DOCX_IMAGE_MAX:
                    try:
                        data = _bounded(archive, targets[rid], DOCX_IMAGE_CAP)
                    except KeyError:
                        data = None
                    if (data and _image_sig(data)
                            and len(data) <= budget[0]):
                        budget[0] -= len(data)
                        images.append({"n": n, "data":
                                       base64.b64encode(data).decode()})
            return f"[image {numbered[rid]}]"

        def walk(el, out: list[str]) -> None:
            tag = el.tag
            if tag == f"{_W}t":
                out.append(el.text or "")
            elif tag == f"{_W}tab":
                out.append("\t")
            elif tag in (f"{_W}br", f"{_W}cr"):
                out.append("\n")
            elif tag == f"{_A}blip":
                out.append(picture(el.get(f"{_R}embed")))
            elif tag == f"{_V}imagedata":
                out.append(picture(el.get(f"{_R}id")))
            elif tag == f"{_W}tr":
                cells = []
                for tc in el.iter(f"{_W}tc"):
                    paras = []
                    for p in tc.findall(f"{_W}p"):
                        bit: list[str] = []
                        walk(p, bit)
                        if (s := "".join(bit).strip()):
                            paras.append(s)
                    cells.append(" / ".join(paras))
                if any(cells):
                    out.append(" | ".join(cells) + "\n")
            elif tag == f"{_W}p":
                for child in el:
                    walk(child, out)
                out.append("\n")
            else:
                for child in el:
                    walk(child, out)

        out: list[str] = []
        walk(body, out)
    lines = [ln.rstrip() for ln in "".join(out).split("\n")]
    text = "\n".join(ln for ln in lines if ln.strip())
    return "ok", text[:TEXT_CAP], images


def _plain(path: str) -> tuple[str, str]:
    with open(path, "rb") as fh:
        raw = fh.read(TEXT_CAP * 4)
    return "ok", raw.decode("utf-8", errors="replace")[:TEXT_CAP]


def main() -> int:
    _harden()
    path, mime = sys.argv[1], (sys.argv[2] or "").lower()
    pages: list[str] = []
    images: list[dict] = []
    try:
        if mime == "application/pdf":
            status, text, pages = _pdf(path)
            method = "pypdf"
        elif mime == DOCX_MIME:
            status, text, images = _docx(path)
            method = "docx"
        elif mime in PKPASS_MIMES:
            status, text = _pkpass(path)
            method = "pkpass"
        elif mime.startswith("text/") or mime in ("application/json",):
            status, text = _plain(path)
            method = "plain"
        else:
            status, text, method = "unsupported", "", None
    except MemoryError:
        # The bomb case, reported rather than crashed: RLIMIT_AS turned a
        # runaway allocation into an exception we can still name.
        status, text, method = "too_big", "", None
    except Exception as e:  # any parser failure is just a status
        json.dump({"status": "error", "detail": str(e)[:200]}, sys.stdout)
        return 0
    text = text.strip()
    if status == "ok" and not text and not images:
        status = "empty"
    out = {"status": status, "method": method, "text": text}
    if pages:
        out["pages"] = pages
    if images:
        out["images"] = images
    json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
