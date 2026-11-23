"""Look lab: verdicts on UI design variants, for the redesign rounds.

The redesign is an A/B loop rather than one big rewrite: a
round of looks ships as CSS scoped to `html[data-look=…]`
(frontend/src/looks/), you live with them and flip between two, and each
verdict lands here with a note. The next round's dev tab reads these
(`GET /api/looks`, or the `look_votes` table) to drop what lost, keep what
won, and try the next variable.

Which looks exist is the frontend's registry, not data: this only stores
what you said about them.

Except themes, which are data: a colour look Kestrel made
when asked ("make it warmer"). A theme is a set of **colour roles** and
nothing else, each a `#RRGGBB` that lands on one design token. Free CSS is
not accepted anywhere on this path, because guessed CSS is what failed in the
tool this was learned from, and its agent said "fixed" anyway. Every role set
is contrast-checked against the pairs the baseline was built to
(docs/ui-overhaul.md) before it is stored, and the tool's result is a WebKit
picture of it (`specimen_shot`), so "done" can't be said unseen.

A theme is a look like any other: `data-look="theme-<slug>"`, a chip in the
lab's Colour row, votes in `look_votes` under that id. Which theme is on is
held here (`active`), not per browser, so the one Kestrel set is the one the
window shows.
"""

import json
import re
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import db, webshot
from .config import BACKEND_PORT

router = APIRouter(prefix="/api/looks")


class VoteIn(BaseModel):
    look: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9-]+$")
    verdict: int = Field(ge=-1, le=1)          # -1 no, 0 a note only, 1 yes
    note: str = Field(default="", max_length=2000)
    against: str | None = Field(default=None, max_length=40)   # the B it was compared with
    view: str | None = Field(default=None, max_length=60)      # what was on screen


@router.post("/votes")
def vote(body: VoteIn):
    now = datetime.now().isoformat(timespec="seconds")
    with db.session() as con, con:
        cur = con.execute(
            "INSERT INTO look_votes (look, verdict, note, against, view, at, day)"
            " VALUES (?,?,?,?,?,?,?)",
            (body.look, body.verdict, body.note.strip() or None, body.against,
             body.view, now, db.day_key()))
    return {"id": cur.lastrowid}


@router.get("")
def summary():
    """Per look: yes/no counts, and every note newest first."""
    with db.session() as con:
        rows = con.execute(
            "SELECT id, look, verdict, note, against, view, at FROM look_votes"
            " ORDER BY at DESC, id DESC").fetchall()
    looks: dict[str, dict] = {}
    for r in rows:
        s = looks.setdefault(r["look"], {"look": r["look"], "yes": 0, "no": 0, "notes": []})
        if r["verdict"] > 0:
            s["yes"] += 1
        elif r["verdict"] < 0:
            s["no"] += 1
        if r["note"] or r["verdict"]:
            s["notes"].append({k: r[k] for k in ("id", "verdict", "note", "against", "view", "at")})
    return {"looks": list(looks.values())}


@router.delete("/votes/{vote_id}")
def unvote(vote_id: int):
    with db.session() as con, con:
        con.execute("DELETE FROM look_votes WHERE id = ?", (vote_id,))
    return {"ok": True}


# ---------- themes: colour roles Kestrel may set ----------

# Role -> the token it sets. The whole surface a theme can touch: colour
# tokens from the `:root` block of frontend/src/styles.css, and no others.
ROLES: dict[str, str] = {
    "desk": "--paper", "surface": "--paper-raise",
    "line": "--paper-line", "line_strong": "--paper-line-2", "boundary": "--paper-line-3",
    "text": "--paper-ink", "text_secondary": "--paper-mid", "text_faint": "--paper-faint",
    "console": "--ink", "console_raise": "--ink-raise",
    "console_line": "--ink-line", "console_line_strong": "--ink-line-2",
    "console_text": "--ink-text", "console_secondary": "--ink-mid", "console_faint": "--ink-faint",
    "accent": "--accent", "on_accent": "--on-accent", "accent_console": "--accent-ink",
    "data": "--data", "good": "--good", "bad": "--bad",
}

# The baseline a theme is laid over, per scheme: styles.css's values, held
# equal to it by test_looks.py. A role a theme leaves out keeps these.
_LIGHT = {
    "desk": "#F6F6F8", "surface": "#FFFFFF", "line": "#E6E6E9", "line_strong": "#D5D5D9",
    "boundary": "#8A8A8E", "text": "#19191C", "text_secondary": "#636368", "text_faint": "#808084",
    "console": "#1A1A1C", "console_raise": "#252527", "console_line": "#303032",
    "console_line_strong": "#3D3D3F", "console_text": "#EBEBED", "console_secondary": "#A4A4A8",
    "console_faint": "#747477", "accent": "#006CE2", "on_accent": "#FFFFFF",
    "accent_console": "#429AFE", "data": "#5C68AE", "good": "#1D7D3E", "bad": "#C51E21",
}
BASELINE: dict[str, dict[str, str]] = {
    "light": _LIGHT,
    "dark": {**_LIGHT,
             "desk": "#0F0F11", "surface": "#19191B", "line": "#27272A", "line_strong": "#363639",
             "boundary": "#6E6E72", "text": "#F0F0F2", "text_secondary": "#A4A4A8",
             "text_faint": "#7A7A7E", "console": "#080809", "console_raise": "#141416",
             "console_line": "#202022", "console_line_strong": "#2D2D30", "console_text": "#E7E7EA",
             "console_secondary": "#A1A1A4", "console_faint": "#717174", "accent": "#3293FC",
             "on_accent": "#08080A", "data": "#8C99DD", "good": "#47C86E", "bad": "#F45A51"},
}
SCHEMES = ("dark", "light")   # dark first: it is where the app is used

# (foreground, background, least ratio). WCAG 2.2: 4.5 for text, 3 for
# incidental text, component boundaries and graphics; 7 where the baseline
# holds it for body text. The baseline clears every row in both schemes.
CHECKS: tuple[tuple[str, str, float], ...] = (
    ("text", "surface", 7), ("text", "desk", 7),
    ("text_secondary", "surface", 4.5), ("text_secondary", "desk", 4.5),
    ("text_faint", "surface", 3), ("text_faint", "desk", 3),
    ("boundary", "surface", 3),
    ("accent", "surface", 4.5), ("accent", "desk", 4.5), ("on_accent", "accent", 4.5),
    ("console_text", "console", 7), ("console_secondary", "console", 4.5),
    ("console_faint", "console", 3), ("accent_console", "console", 4.5),
    ("good", "surface", 4.5), ("bad", "surface", 4.5), ("data", "surface", 3),
)

HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
THEME_PREFIX = "theme-"
THEMES_MAX = 24               # the lab's Colour row is one line of chips, not an archive


class ThemeError(ValueError):
    """A role set that can't be stored; the message says which role and why."""


def _luminance(hex_: str) -> float:
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(int(hex_[i:i + 2], 16) / 255) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two #RRGGBB colours."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def resolved(scheme: str, roles: dict[str, str]) -> dict[str, str]:
    return {**BASELINE[scheme], **roles}


def problems(scheme: str, roles: dict) -> list[str]:
    """Everything wrong with one scheme's roles, each line fixable on its own."""
    out = []
    for role, value in roles.items():
        if role not in ROLES:
            out.append(f"{scheme}: no role '{role}' (roles: {', '.join(ROLES)})")
        elif not isinstance(value, str) or not HEX.match(value):
            out.append(f"{scheme}.{role}: {value!r} isn't #RRGGBB")
    if out:
        return out
    full = resolved(scheme, roles)
    for fg, bg, least in CHECKS:
        # A pair the theme didn't touch is the baseline's, already checked.
        if fg not in roles and bg not in roles:
            continue
        got = contrast(full[fg], full[bg])
        if got < least:
            out.append(f"{scheme}: {fg} {full[fg]} on {bg} {full[bg]} is {got:.1f}:1, "
                       f"needs {least:g}:1")
    return out


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30].strip("-")
    if not s:
        raise ThemeError("a theme needs a name with a letter or digit in it")
    return THEME_PREFIX + s


def _row(r) -> dict:
    return {"id": r["id"], "name": r["name"], "note": r["note"],
            "dark": json.loads(r["dark"]), "light": json.loads(r["light"]),
            "updated_at": r["updated_at"]}


def themes(con=None) -> dict:
    """Every stored theme, which one is on, and the role -> token map the
    page writes its CSS from (so the mapping lives here only)."""
    with db.borrow(con) as con:
        rows = con.execute("SELECT * FROM look_themes ORDER BY rowid").fetchall()
    return {"themes": [_row(r) for r in rows],
            "active": next((r["id"] for r in rows if r["active"]), None),
            "roles": ROLES}


def save_theme(name: str, dark: dict, light: dict | None = None, note: str = "") -> dict:
    """Check, store and switch to a theme. Same name again replaces it."""
    name = " ".join(str(name or "").split())[:40]
    tid = slug(name)
    dark, light = dict(dark or {}), dict(light or {})
    if not dark and not light:
        raise ThemeError("a theme sets at least one role; dark first")
    wrong = problems("dark", dark) + problems("light", light)
    if wrong:
        raise ThemeError("not stored:\n" + "\n".join(wrong))
    upper = [{k: v.upper() for k, v in d.items()} for d in (dark, light)]
    now = datetime.now().isoformat(timespec="seconds")
    with db.session() as con, con:
        known = con.execute("SELECT 1 FROM look_themes WHERE id = ?", (tid,)).fetchone()
        count = con.execute("SELECT COUNT(*) FROM look_themes").fetchone()[0]
        if not known and count >= THEMES_MAX:
            raise ThemeError(f"{THEMES_MAX} themes are stored already; reuse a name to "
                             "replace one, or remove some in the Look lab")
        con.execute("UPDATE look_themes SET active = 0")
        con.execute(
            "INSERT INTO look_themes (id, name, note, dark, light, active, created_at, updated_at)"
            " VALUES (?,?,?,?,?,1,?,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name, note=excluded.note,"
            " dark=excluded.dark, light=excluded.light, active=1, updated_at=excluded.updated_at",
            (tid, name, str(note or "").strip()[:300] or None,
             json.dumps(upper[0]), json.dumps(upper[1]), now, now))
        return _row(con.execute("SELECT * FROM look_themes WHERE id = ?", (tid,)).fetchone())


def activate(theme_id: str | None) -> str | None:
    """Switch to a stored theme, or to none (the colours picked in the lab)."""
    with db.session() as con, con:
        if theme_id is not None and not con.execute(
                "SELECT 1 FROM look_themes WHERE id = ?", (theme_id,)).fetchone():
            raise ThemeError(f"no theme '{theme_id}'")
        con.execute("UPDATE look_themes SET active = (id IS ?)", (theme_id,))
    return theme_id


def report(theme: dict) -> str:
    """The measured ratios of what was stored, for the tool's result."""
    lines = []
    for scheme in SCHEMES:
        roles = theme[scheme]
        if not roles:
            continue
        full = resolved(scheme, roles)
        pairs = [f"{fg}/{bg} {contrast(full[fg], full[bg]):.1f}"
                 for fg, bg, _ in CHECKS if fg in roles or bg in roles]
        lines.append(f"{scheme}: {len(roles)} roles set; contrast {', '.join(pairs)}")
    return "\n".join(lines)


# The specimen: a page of the real stylesheet with every role in use
# (frontend/src/ui/ThemeSpecimen.jsx). Not the desk itself, on purpose: the
# desk opens a console socket (a CLI per load) and syncs providers on read.
def specimen_url(theme_id: str | None, scheme: str, base: str | None = None) -> str:
    base = base or f"http://127.0.0.1:{BACKEND_PORT}"
    return f"{base}/#specimen/{scheme}/{theme_id or 'baseline'}"


def specimen_shot(theme_id: str | None, scheme: str = "dark", base: str | None = None) -> bytes:
    """PNG bytes of the specimen under a theme. Raises webshot.ShotError."""
    with tempfile.TemporaryDirectory(prefix="kestrel-shot-") as tmp:
        out = webshot.shoot(specimen_url(theme_id, scheme, base), Path(tmp) / "specimen.png",
                            width=1180, height=660, scheme=scheme,
                            ready='document.documentElement.dataset.specimen === "ready"')
        return out.read_bytes()


class ActiveIn(BaseModel):
    id: str | None = Field(default=None, max_length=40, pattern=r"^theme-[a-z0-9-]+$")


@router.get("/themes")
def list_themes():
    return themes()


@router.put("/themes/active")
def set_active(body: ActiveIn):
    try:
        return {"active": activate(body.id)}
    except ThemeError as e:
        raise HTTPException(404, str(e)) from e


@router.delete("/themes/{theme_id}")
def remove_theme(theme_id: str):
    with db.session() as con, con:
        con.execute("DELETE FROM look_themes WHERE id = ?", (theme_id,))
    return {"ok": True}
