"""Look lab verdicts (app/looks.py): stored as said, summed per look."""

from app import looks


def test_votes_sum_per_look_with_notes_newest_first(router_client):
    c = router_client(looks.router)
    assert c.post("/api/looks/votes", json={"look": "cupertino", "verdict": 1,
                                            "note": "cards finally breathe", "against": "baseline"}).status_code == 200
    c.post("/api/looks/votes", json={"look": "cupertino", "verdict": -1, "note": "blue is too loud"})
    c.post("/api/looks/votes", json={"look": "studio", "verdict": 0, "note": "console looks right"})
    got = {s["look"]: s for s in c.get("/api/looks").json()["looks"]}
    assert (got["cupertino"]["yes"], got["cupertino"]["no"]) == (1, 1)
    assert got["cupertino"]["notes"][0]["note"] == "blue is too loud"
    assert got["studio"]["notes"][0]["verdict"] == 0
    vid = got["studio"]["notes"][0]["id"]
    c.delete(f"/api/looks/votes/{vid}")
    assert "studio" not in {s["look"] for s in c.get("/api/looks").json()["looks"]}


def test_a_look_name_is_a_slug_and_a_verdict_is_minus_one_to_one(router_client):
    c = router_client(looks.router)
    assert c.post("/api/looks/votes", json={"look": "../x", "verdict": 1}).status_code == 422
    assert c.post("/api/looks/votes", json={"look": "a", "verdict": 3}).status_code == 422


# ---------- themes: colour roles only ----------

import asyncio
import re
from pathlib import Path

import pytest

from app import console_tools as ct, webshot

WARM = {"desk": "#14110F", "surface": "#1E1A17", "accent": "#E8A04C", "on_accent": "#1A1206"}


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})\b", block))


def test_the_baseline_is_the_stylesheets_and_passes_its_own_checks():
    """BASELINE is a copy of styles.css's colour tokens; this holds them equal,
    so a contrast verdict is about the colours the page really falls back to."""
    css = (Path(__file__).parents[2] / "frontend/src/styles.css").read_text()
    light = _tokens(css[css.index(":root {"):css.index("@media (prefers-color-scheme: dark)")])
    dark = _tokens(css[css.index(':root[data-theme="dark"] {'):css.index(':root[data-theme="light"]')])
    for role, token in looks.ROLES.items():
        assert looks.BASELINE["light"][role] == light[token], role
        assert looks.BASELINE["dark"][role] == dark.get(token, light[token]), role
    for scheme in looks.SCHEMES:
        for fg, bg, least in looks.CHECKS:
            got = looks.contrast(looks.BASELINE[scheme][fg], looks.BASELINE[scheme][bg])
            assert got >= least, (scheme, fg, bg, got)


def test_a_theme_is_stored_switched_to_and_replaced_by_name(router_client):
    c = router_client(looks.router)
    t = looks.save_theme("Warm dusk", WARM, note="amber on brown")
    assert t["id"] == "theme-warm-dusk" and t["dark"]["accent"] == "#E8A04C"
    got = c.get("/api/looks/themes").json()
    assert got["active"] == "theme-warm-dusk" and got["roles"]["accent"] == "--accent"
    looks.save_theme("warm dusk", {**WARM, "accent": "#F0B060"})
    looks.save_theme("Cool", {"accent": "#5AB0FF"})
    got = c.get("/api/looks/themes").json()
    assert [x["id"] for x in got["themes"]] == ["theme-warm-dusk", "theme-cool"]
    assert got["themes"][0]["dark"]["accent"] == "#F0B060"
    assert got["active"] == "theme-cool"
    assert c.put("/api/looks/themes/active", json={"id": None}).json() == {"active": None}
    assert c.put("/api/looks/themes/active", json={"id": "theme-nope"}).status_code == 404
    c.delete("/api/looks/themes/theme-cool")
    assert [x["id"] for x in c.get("/api/looks/themes").json()["themes"]] == ["theme-warm-dusk"]
    # a theme's id is a vote's look
    assert c.post("/api/looks/votes", json={"look": t["id"], "verdict": 1}).status_code == 200


@pytest.mark.parametrize("dark, says", [
    ({"text": "#555555"}, "text #555555 on surface #19191B is 2.4:1, needs 7:1"),
    ({"surface": "#808080"}, "on surface #808080"),
    ({"accent": "red"}, "dark.accent: 'red' isn't #RRGGBB"),
    ({"accent": "#3293FC; } body { display: none"}, "isn't #RRGGBB"),
    ({"font": "#FFFFFF"}, "no role 'font'"),
    ({}, "at least one role"),
])
def test_a_role_set_that_fails_is_refused_and_says_what_to_fix(dark, says, con):
    with pytest.raises(looks.ThemeError, match=re.escape(says)):
        looks.save_theme("Bad", dark)
    assert con.execute("SELECT COUNT(*) FROM look_themes").fetchone()[0] == 0


def test_light_roles_are_checked_against_the_light_baseline(dbfile):
    with pytest.raises(looks.ThemeError, match="light: accent #3293FC on surface #FFFFFF"):
        looks.save_theme("Both", WARM, light={"accent": "#3293FC"})


# ---------- the tool ----------

def _set(args):
    return asyncio.run(ct.theme_set.handler(args))


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def test_theme_set_returns_the_picture_with_the_result(monkeypatch, dbfile):
    shots = []
    monkeypatch.setattr(looks, "specimen_shot",
                        lambda theme_id, scheme="dark", base=None: shots.append((theme_id, scheme)) or PNG)
    out = _set({"name": "Warm dusk", "dark": WARM})
    assert not out.get("is_error")
    text, image = out["content"]
    assert "Stored 'Warm dusk'" in text["text"] and "on_accent/accent" in text["text"]
    assert image["type"] == "image" and image["mimeType"] == "image/png"
    assert shots == [("theme-warm-dusk", "dark")]
    # name alone switches back to it; reset leaves it stored
    _set({"reset": True})
    assert looks.themes()["active"] is None and shots[-1] == (None, "dark")
    _set({"name": "warm dusk"})
    assert looks.themes()["active"] == "theme-warm-dusk"


def test_theme_set_without_a_picture_says_it_has_not_been_seen(monkeypatch, dbfile):
    def no_shot(*a, **k):
        raise webshot.ShotError("the page didn't load")
    monkeypatch.setattr(looks, "specimen_shot", no_shot)
    out = _set({"name": "Warm dusk", "dark": WARM})
    said = out["content"][0]["text"]
    assert len(out["content"]) == 1 and "You have NOT seen this theme" in said
    assert looks.themes()["active"] == "theme-warm-dusk"   # applied all the same


def test_theme_set_refuses_a_failing_set_before_any_picture(monkeypatch, dbfile):
    monkeypatch.setattr(looks, "specimen_shot", lambda *a, **k: pytest.fail("shot a refused theme"))
    out = _set({"name": "Grey", "dark": {"text": "#555555"}})
    assert out["is_error"] and "needs 7:1" in out["content"][0]["text"]
    out = _set({"name": "never made"})
    assert out["is_error"] and "no theme" in out["content"][0]["text"]


def test_theme_set_schema_takes_roles_and_hexes_only():
    props = ct.theme_set.input_schema["properties"]
    assert props["dark"]["propertyNames"]["enum"] == list(looks.ROLES)
    assert props["dark"]["additionalProperties"]["pattern"] == looks.HEX.pattern
    assert ct.theme_set.input_schema["additionalProperties"] is False
    assert all(role in ct.theme_set.description for role in looks.ROLES)


def test_a_screenshot_is_of_this_machine_only():
    assert webshot.loopback("http://127.0.0.1:8788/#specimen/dark/baseline")
    for url in ("https://127.0.0.1/", "http://example.com/", "file:///etc/passwd",
                "http://127.0.0.1.evil.example/"):
        assert not webshot.loopback(url)
        with pytest.raises(webshot.ShotError):
            webshot.shoot(url, Path("/tmp/never.png"))
