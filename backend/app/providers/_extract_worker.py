"""Parses one untrusted file, in a process that is expected to die.

Run as a child of providers/attachments.py, never imported by the backend:
`python -I app/providers/_extract_worker.py <path> <mime>`. Imports nothing from
`app` (so `-I` works), hence the by-hand mime duplicates below. stdout is one JSON
object and nothing else. Images are never decoded here. Memory is NOT capped here
(Darwin refuses); the parent's RSS watchdog does it.
Design: DESIGN.md → Attachments.
"""

import json
import resource
import sys

MEM_LIMIT_BYTES = 1024 * 1024 * 1024   # Linux only; fits a 50MB scanned PDF's page tree
CPU_LIMIT_SECONDS = 20
TEXT_CAP = 20_000
# Per-page caps bound the JSON the parent reads back (a 300-page chapter is ~600k chars).
PAGES_MAX = 1000
PAGES_CAP = 1_500_000
PAGE_CAP = 20_000

# ---- Apple Wallet passes ----------------------------------------------------
# Keep in step with attachments.PKPASS_MIMES by hand (no `app` imports under `-I`).
PKPASS_MIMES = ("application/vnd.apple.pkpass", "com.apple.pkpass")

PKPASS_JSON_CAP = 1_000_000

# A pass carries exactly one of these, and it names what the thing *is*.
PASS_STYLES = {"boardingPass": "boarding pass", "eventTicket": "event ticket",
               "coupon": "coupon", "storeCard": "store card",
               "generic": "pass"}

# Wallet's own display order, front of the pass to back.
PASS_SECTIONS = ("headerFields", "primaryFields", "secondaryFields",
                 "auxiliaryFields", "backFields")

BARCODE_CAP = 500


def _limit(name: str, value: int) -> bool:
    """Best-effort setrlimit; True if it took. Unsettable is normal on Darwin: skip, never raise, so the next limit still applies."""
    which = getattr(resource, name, None)
    if which is None:
        return False
    try:
        resource.setrlimit(which, (value, value))
        return True
    except (ValueError, OSError):
        return False


def _harden() -> None:
    """Apply every limit the platform accepts, before the file is opened. FSIZE and NPROC go first."""
    _limit("RLIMIT_FSIZE", 0)
    _limit("RLIMIT_NPROC", 0)
    _limit("RLIMIT_CORE", 0)
    _limit("RLIMIT_CPU", CPU_LIMIT_SECONDS)
    _limit("RLIMIT_AS", MEM_LIMIT_BYTES)   # Linux only; a no-op on Darwin


def _pdf(path: str) -> tuple[str, str, list[str]]:
    """(status, text, pages). `text` is the first TEXT_CAP chars; `pages` is each page's text in order.
    A page that fails to extract stays as "" — dropping it would renumber the rest and break citations."""
    from pypdf import PdfReader

    reader = PdfReader(path)
    if reader.is_encrypted:
        # Empty-password decrypt covers owner-password-only files; a real password means no text.
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
    """One pass value as text, or "" if it isn't a scalar (containers are dropped, not str()'d)."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return value.strip() if isinstance(value, str) else ""


def _pass_fields(section) -> list[str]:
    """One `label: value` line per field, in the pass's order.

    Pairing is kept as authored even when odd (never guess which half is the
    label); `attributedValue` is only the fallback to `value`."""
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
    """pass.json → the pass as a human would read it. Skips `authenticationToken` and
    `webServiceURL` on purpose: a credential, and extracted text reaches prompts."""
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

    # `barcodes` is current, `barcode` the pre-iOS 9 form; issuers emit either.
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
        return ""       # a `{}` stub: main() files it as `empty`

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
    """Read pass.json from a .pkpass zip and nothing else: no image decode, no signature check."""
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            with archive.open("pass.json") as fh:
                # Bounded read, never ZipInfo.file_size: the sender wrote that number.
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
# Keep in step with attachments.DOCX_MIME by hand.
DOCX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document")

DOCX_XML_CAP = 5_000_000    # a real 50-page handout's document.xml is 54KB

# Pictures go back undecoded. Caps bound stdout JSON (parent's _STDOUT_CAP is 16MB, base64 adds a
# third) and vision calls billed per file; per-image matches ATTACHMENT_VISION_MAX_BYTES.
DOCX_IMAGE_MAX = 12
DOCX_IMAGE_CAP = 3_500_000
DOCX_IMAGES_TOTAL_CAP = 8_000_000

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_V = "{urn:schemas-microsoft-com:vml}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _image_sig(data: bytes) -> bool:
    """Signature check only: the four formats attachments._sniff accepts; others are skipped."""
    return (data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff"
            or data[:6] in (b"GIF87a", b"GIF89a")
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


def _bounded(archive, name: str, cap: int) -> bytes | None:
    """A member's bytes, or None past the cap (bounded read, not ZipInfo.file_size)."""
    with archive.open(name) as fh:
        raw = fh.read(cap + 1)
    return None if len(raw) > cap else raw


def _safe_xml(raw: bytes):
    """Parse, refusing any DTD (OOXML has none: damage or entity expansion)."""
    import xml.etree.ElementTree as ET
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise ValueError("DTD in OOXML")
    return ET.fromstring(raw)


def _docx(path: str) -> tuple[str, str, list[dict]]:
    """(status, text, images) for a Word file.

    Text is in reading order: a paragraph per line, a table row per line with cells
    joined by " | ". Each embedded picture becomes an "[image N]" marker and its raw
    bytes come back in `images` for the parent to read."""
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
        # RLIMIT_AS (Linux) turns a runaway allocation into this.
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
