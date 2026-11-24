"""The failure log (v44): a repeat costs one row, distinct breaks stay distinct, and no recording path may raise into what it instruments.

Design: DESIGN.md → `tests/test_failures.py`.
"""

import json
import sqlite3

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import db, failures


@pytest.fixture
def api(router_client):
    return router_client(failures.router)


def _rows():
    with db.session() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM failures ORDER BY last_at DESC")]


def test_repeat_bumps_count_instead_of_appending(dbfile):
    """The table is keyed by fingerprint: a widget that throws every re-render is one row, not thousands an hour."""
    for _ in range(50):
        failures.record("desk", "render", "Finance",
                        detail="Cannot read properties of undefined")

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["count"] == 50
    assert rows[0]["first_at"] <= rows[0]["last_at"]


def test_same_label_different_error_stays_two_rows(dbfile):
    """Two ways for one card to break stay two rows, not collapsed by label."""
    failures.record("desk", "render", "Finance", detail="undefined is not a function")
    failures.record("desk", "render", "Finance", detail="x.map is not a function")
    assert len(_rows()) == 2


def test_origins_do_not_collide(dbfile):
    """The same label from two layers is two findings."""
    failures.record("api", "unhandled", "boom", detail="same")
    failures.record("desk", "render", "boom", detail="same")
    assert {r["origin"] for r in _rows()} == {"api", "desk"}


def test_newest_trace_wins_and_missing_one_does_not_erase(dbfile):
    """A later report with no trace must not blank the trace you had."""
    failures.record("api", "unhandled", "GET /api/x", detail="e", trace="first")
    failures.record("api", "unhandled", "GET /api/x", detail="e", trace="second")
    assert _rows()[0]["trace"] == "second"

    failures.record("api", "unhandled", "GET /api/x", detail="e")
    assert _rows()[0]["trace"] == "second"


def test_trim_keeps_the_most_recent(dbfile, monkeypatch):
    """Trimming is by recency, not insertion: a long-standing failure that still fires holds the oldest `first_at`."""
    monkeypatch.setattr(failures, "FAILURE_KEEP", 5)
    for i in range(12):
        failures.record("api", "unhandled", f"GET /api/{i}", detail=str(i))

    rows = _rows()
    assert len(rows) == 5
    assert {r["label"] for r in rows} == {f"GET /api/{i}" for i in range(7, 12)}


def test_long_detail_and_trace_are_clipped(dbfile):
    """A message built from a response body has no natural ceiling."""
    failures.record("desk", "render", "Big", detail="x" * 50_000,
                    trace="y" * 50_000)
    row = _rows()[0]
    assert len(row["detail"]) == failures.DETAIL_MAX + 1      # + the ellipsis
    assert len(row["trace"]) == failures.TRACE_MAX + 1


def test_record_never_raises(dbfile, monkeypatch):
    """Recording never raises: it is called from middleware and the console stream loop."""
    def boom():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "session", boom)
    failures.record("api", "unhandled", "GET /api/x", detail="e")   # no raise


def test_recent_survives_a_broken_db(dbfile, monkeypatch):
    """The read side degrades too: the status panel must render when the fault log can't be read."""
    def boom():
        raise sqlite3.OperationalError("no such table: failures")

    monkeypatch.setattr(db, "session", boom)
    assert failures.recent() == []


def test_post_reports_under_desk_origin(api):
    """`origin` is not taken from the body; anything hitting the endpoint came from the browser."""
    r = api.post("/api/failures", json={
        "kind": "render", "label": "Health", "detail": "boom"})
    assert r.status_code == 200

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["origin"] == "desk"
    assert rows[0]["label"] == "Health"


def test_post_rejects_an_unknown_kind(api):
    """The kinds are a closed set, so a typo fails loudly."""
    assert api.post("/api/failures", json={
        "kind": "whatever", "label": "Health"}).status_code == 422


def test_list_counts_distinct_and_occurrences(api):
    """Two numbers: how many things are broken, and whether one is on fire right now."""
    for _ in range(9):
        failures.record("desk", "render", "Finance", detail="a")
    failures.record("api", "unhandled", "GET /api/health", detail="b")

    body = api.get("/api/failures").json()
    assert body["distinct"] == 2
    assert body["occurrences"] == 10
    assert body["failures"][0]["ago"] is not None


def test_list_filters_by_origin(api):
    failures.record("desk", "render", "Finance", detail="a")
    failures.record("api", "unhandled", "GET /api/health", detail="b")

    assert api.get("/api/failures?origin=api").json()["distinct"] == 1
    # an unknown origin must not filter to nothing: that reads as "all clear"
    assert api.get("/api/failures?origin=nope").json()["distinct"] == 2


def test_delete_clears(api):
    failures.record("desk", "render", "Finance", detail="a")
    assert api.delete("/api/failures").json() == {"cleared": 1}
    assert _rows() == []


# --- the middleware ---------------------------------------------------------

@pytest.fixture
def instrumented(dbfile):
    """The real `failures.middleware` main.py registers, over routes that fail in different ways."""
    app = FastAPI()
    failures.install(app)

    @app.get("/api/raises")
    def raises():
        raise KeyError("missing")

    @app.get("/api/http500")
    def http500():
        raise HTTPException(502, "upstream is down")

    @app.get("/api/chained")
    def chained(n: int):
        try:
            raise TimeoutError(f"health API read timed out after {n}s")
        except TimeoutError as e:
            raise HTTPException(
                502, f"energy needs the health provider: {e}") from e

    @app.get("/api/other-reason")
    def other_reason():
        raise HTTPException(502, "weather API unavailable")

    @app.get("/api/email/{msg_id}/body")
    def body(msg_id: str):
        raise HTTPException(502, "gmail unavailable")

    @app.get("/api/item/{item_id}/boom")
    def boom(item_id: str):
        raise KeyError(item_id)

    @app.get("/api/fine")
    def fine():
        return {"ok": True}

    @app.get("/api/bad-request")
    def bad_request():
        raise HTTPException(400, "your fault, not mine")

    return TestClient(app, raise_server_exceptions=False)


def test_unhandled_exception_is_recorded_with_its_traceback(instrumented):
    """providers/api.py maps ValueError to 400; everything else is a 500 that must land here, not only in a /tmp log."""
    assert instrumented.get("/api/raises").status_code == 500

    row = _rows()[0]
    assert row["label"] == "GET /api/raises"
    assert "KeyError" in row["detail"]
    assert "raise KeyError" in row["trace"]


def test_rows_are_labelled_by_route_not_by_id(instrumented):
    """Two ids failing the same way are one finding under the route template, for 5xx and exceptions alike."""
    for mid in ("1a0ea66b275339b2", "1a0ea722163c0b58"):
        assert instrumented.get(f"/api/email/{mid}/body").status_code == 502
        assert instrumented.get(f"/api/item/{mid}/boom").status_code == 500

    by_label = {r["label"]: r for r in _rows()}
    assert set(by_label) == {"GET /api/email/{msg_id}/body",
                             "GET /api/item/{item_id}/boom"}
    assert by_label["GET /api/email/{msg_id}/body"]["count"] == 2


def test_hand_raised_5xx_is_recorded_too(instrumented):
    """A returned 502 never hits the except block but is the same failure as a 500."""
    r = instrumented.get("/api/http500")
    assert r.status_code == 502
    # FastAPI's own response is untouched — the handler only takes a note
    assert r.json() == {"detail": "upstream is down"}
    row = _rows()[0]
    assert row["detail"] == "HTTP 502: upstream is down"
    assert "raise HTTPException(502" in row["trace"]


def test_5xx_reason_keeps_its_cause_in_the_trace(instrumented):
    """`raise HTTPException(502, …) from e`: the row carries the upstream error."""
    instrumented.get("/api/chained?n=10")
    row = _rows()[0]
    assert row["detail"] == ("HTTP 502: energy needs the health provider: "
                             "health API read timed out after 10s")
    assert "TimeoutError" in row["trace"]


def test_5xx_reason_varying_only_in_numbers_is_one_row(instrumented):
    """A detail built from `{e}` varies per request; fingerprint the shape and show the newest text."""
    instrumented.get("/api/chained?n=10")
    instrumented.get("/api/chained?n=30")
    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["count"] == 2
    assert rows[0]["detail"].endswith("after 30s")


def test_different_5xx_reasons_on_one_route_stay_apart(instrumented):
    instrumented.get("/api/http500")
    instrumented.get("/api/other-reason")
    instrumented.get("/api/chained?n=1")
    assert len(_rows()) == 3


def test_stable_masks_numbers_and_ids_but_not_words():
    assert failures._stable("msg 1a0ea66b275339b2 failed after 30s on :8788") \
        == "msg # failed after #s on :#"
    assert failures._stable("deadbeefcafe unavailable") \
        == "deadbeefcafe unavailable"   # all-letter hex is a word, not an id


def test_success_and_4xx_are_not_recorded(instrumented):
    """A 400 is the caller's fault, not the app's; logging it would be mostly noise."""
    instrumented.get("/api/fine")
    instrumented.get("/api/bad-request")
    assert _rows() == []


# --- desk traces through the sourcemap --------------------------------------

_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def _enc(*values):
    out = ""
    for v in values:
        v = (-v << 1) | 1 if v < 0 else v << 1
        while True:
            digit, v = v & 31, v >> 5
            out += _B64[digit | (32 if v else 0)]
            if not v:
                break
    return out


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """A two-line bundle whose map points into two sources; line 2's segments are deltas from line 1's state (easy to decode wrong)."""
    assets = tmp_path / "assets"
    assets.mkdir()
    mappings = ";".join([
        # line 1: col 0 -> a.jsx 1:1 ; col 10 -> a.jsx 5:3 ; col 20 unmapped
        ",".join([_enc(0, 0, 0, 0), _enc(10, 0, 4, 2), _enc(10)]),
        # line 2: col 4 -> b.jsx 42:7 (src +1, line +37, col +4 from 5:3)
        _enc(4, 1, 37, 4),
    ])
    (assets / "index-AbC123.js.map").write_text(json.dumps({
        "version": 3, "sources": ["../../src/ui/a.jsx", "../../src/b.jsx"],
        "names": [], "mappings": mappings}))
    monkeypatch.setattr(failures, "DIST_ASSETS", assets)
    failures._load_map.cache_clear()
    return "http://127.0.0.1:8788/assets/index-AbC123.js"


def test_vlq_decodes_signs_and_continuations():
    assert failures._vlq(_enc(0, -1, 16, -2000, 123456)) \
        == [0, -1, 16, -2000, 123456]


def test_minified_frames_resolve_to_source_lines(bundle):
    trace = (f"gk@{bundle}:1:15\n"                      # WebKit, as the .app logs
             f"    at Xy ({bundle}:2:9)\n"              # V8
             f"render@{bundle}:1:25")                   # unmapped span
    out = failures.resolve_trace(trace)
    resolved, minified = out.split("\n\n— minified —\n")
    assert minified == trace
    lines = resolved.splitlines()
    assert lines[0] == "gk@frontend/src/ui/a.jsx:5:3"
    assert lines[1] == "    at Xy (frontend/src/b.jsx:42:7)"
    assert lines[2] == f"render@{bundle}:1:25"


def test_frame_without_its_map_is_left_alone(bundle):
    """A window still running a bundle a later build replaced."""
    trace = "gk@http://127.0.0.1:8788/assets/index-Gone99.js:55:48155"
    assert failures.resolve_trace(trace) == trace


def test_a_broken_map_never_raises(bundle):
    (failures.DIST_ASSETS / "index-AbC123.js.map").write_text("{not json")
    failures._load_map.cache_clear()
    trace = f"gk@{bundle}:1:15"
    assert failures.resolve_trace(trace) == trace


def test_desk_report_stores_the_resolved_trace(api, bundle):
    api.post("/api/failures", json={
        "kind": "unhandled", "label": "TypeError",
        "detail": "{} is not iterable", "trace": f"gk@{bundle}:2:5"})
    trace = _rows()[0]["trace"]
    assert trace.startswith("gk@frontend/src/b.jsx:42:7")
    assert f"gk@{bundle}:2:5" in trace


def test_dist_does_not_serve_sourcemaps(tmp_path):
    from app.main import _Dist

    (tmp_path / "index.html").write_text("<p>hi</p>")
    (tmp_path / "a.js").write_text("1")
    (tmp_path / "a.js.map").write_text("{}")
    app = FastAPI()
    app.mount("/", _Dist(directory=tmp_path, html=True))
    c = TestClient(app)
    assert c.get("/a.js").status_code == 200
    assert c.get("/a.js.map").status_code == 404
    assert c.get("/").status_code == 200
