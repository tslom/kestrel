"""The phone door (app/phone.py): the one exception to localhost-only, so
what it serves, who it lets in and what an answer can touch are held here."""

import asyncio
import json
import socket

import pytest
from fastapi.testclient import TestClient

from app import console_link, phone, webpush

ME = "owner@example.com"
TS = {"Tailscale-User-Login": ME}
SUB = {"endpoint": "https://web.push.apple.com/QOa",
       "keys": {"p256dh": "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
                "auth": "BTBZMqHH6r4Tts7J_aSIgg"}}


@pytest.fixture
def door(dbfile, monkeypatch):
    phone._set("owner_login", ME)
    phone._pair.clear()
    phone._first_seen.clear()
    phone._pushed.clear()
    monkeypatch.setattr(console_link, "links", {})
    return TestClient(phone.door, headers=TS)


def _paired(door) -> dict:
    token = door.post("/api/pair", json={"code": phone.new_pair_code()}).json()["token"]
    return {**TS, "Authorization": f"Bearer {token}"}


class FakeLink:
    """A tab with cards waiting, as console.py leaves one on its Link."""

    def __init__(self, *requests):
        self.turn = list(requests)
        self.open = {r["request_id"] for r in requests if r.get("request_id")}
        self.inbox = asyncio.Queue()
        self.sent = []
        self.open_request = lambda rid: rid in self.open

    async def send_json(self, msg):
        self.sent.append(msg)


def _card(rid="r1", **kw):
    return {"type": "permission_request", "request_id": rid, "tool": "mcp__vault__email_schedule",
            "input": {"to": "dana@example.com", "body": "the whole private body " * 40},
            "inputs": [{}], "preview": [{"lead": "Email to Dana, Tue 9:00"}],
            "why": "You asked me to send this tomorrow.", "guard": None, "danger": None, **kw}


# ---------- what the door is ----------

def test_it_binds_loopback_and_serves_only_its_allowlist():
    assert phone.HOST == "127.0.0.1"
    served = {(m, r.path) for r in phone.door.routes for m in r.methods - {"HEAD"}}
    assert served == phone.DOOR_ROUTES
    # none of the main app's surface: no console, no db, no files, no docs
    assert not [p for _, p in served if p.startswith(("/api/console", "/api/db", "/ws", "/docs"))]


def test_the_door_listens_on_loopback_only_and_closes(dbfile, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(phone, "PORT", port)
    phone._set("owner_login", ME)

    async def run():
        await phone.sync()
        assert not phone.listening()          # off until switched on
        phone._set("enabled", "1")
        await phone.sync()
        assert phone.listening()
        await asyncio.sleep(0.3)
        bound = [s.getsockname() for srv in phone._server.servers for s in srv.sockets]
        assert bound == [("127.0.0.1", port)]
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"GET /api/status HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        assert b"403" in await r.readline()   # no tailnet login on it
        w.close()
        serving = phone._serving
        phone._set("enabled", None)
        await phone.sync()
        assert not phone.listening()
        await asyncio.wait_for(serving, 5)
    asyncio.run(run())
    with socket.socket() as s:                # the port is free again
        s.bind(("127.0.0.1", port))


def test_a_taken_port_is_reported_not_fatal(dbfile, monkeypatch):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        monkeypatch.setattr(phone, "PORT", taken.getsockname()[1])
        phone._set("enabled", "1")
        asyncio.run(phone.sync())
        assert not phone.listening() and "taken" in phone._bind_error


# ---------- who gets in ----------

@pytest.mark.parametrize("headers, why", [
    ({"Tailscale-User-Login": ""}, "not the owner"),
    ({"Tailscale-User-Login": "someone@else.com"}, "not the owner"),
    ({"Host": "evil.example"}, "host not allowed"),
    ({"Host": "kestrel.ts.net.evil.example"}, "host not allowed"),
])
def test_the_guard_refuses_before_any_route(door, headers, why):
    for path in ("/", "/app.js", "/api/status"):
        r = door.get(path, headers=headers)
        assert r.status_code == 403 and why in r.text


def test_the_tailnet_name_is_let_in_and_headers_are_set(door):
    r = door.get("/", headers={"Host": "macbook.tail1234.ts.net:8443"})
    assert r.status_code == 200 and "Kestrel" in r.text
    assert r.headers["content-security-policy"].startswith("default-src 'self'")
    assert door.get("/sw.js").headers["content-type"].startswith("text/javascript")
    assert door.get("/icon.png").content[:4] == b"\x89PNG"


def test_no_owner_set_means_nothing_is_served(door):
    phone._set("owner_login", None)
    assert door.get("/").status_code == 403
    assert door.get("/", headers={"Tailscale-User-Login": ""}).status_code == 403


def test_a_write_must_be_json_from_our_own_page(door):
    code = phone.new_pair_code()
    assert door.post("/api/pair", json={"code": code},
                     headers={"Origin": "https://evil.example"}).status_code == 403
    assert door.post("/api/pair", data={"code": code}).status_code == 403    # a form: no preflight
    host = "macbook.tail1234.ts.net:8443"
    ok = door.post("/api/pair", json={"code": code}, headers={"Host": host, "Origin": f"https://{host}"})
    assert ok.status_code == 200


def test_sockets_are_refused(door):
    with pytest.raises(Exception):
        with door.websocket_connect("/ws/console", headers=TS):
            pass


# ---------- pairing ----------

def test_a_code_pairs_once_and_only_its_hash_is_stored(door, con):
    code = phone.new_pair_code()
    got = door.post("/api/pair", json={"code": f"{code[:4]}-{code[4:]}", "name": "iPhone"}).json()
    assert got["scopes"] == ["status", "approve"]
    row = con.execute("SELECT * FROM phone_devices").fetchone()
    assert row["token_hash"] == phone._hash(got["token"]) and got["token"] not in dict(row).values()
    assert door.post("/api/pair", json={"code": code}).status_code == 403      # spent


def test_wrong_guesses_void_the_code_and_time_does_too(door):
    code = phone.new_pair_code()
    wrong = "00000000" if code != "00000000" else "11111111"
    for _ in range(phone.PAIR_TRIES):
        assert door.post("/api/pair", json={"code": wrong}).status_code == 403
    assert door.post("/api/pair", json={"code": code}).status_code == 403
    code = phone.new_pair_code()
    phone._pair["expires"] = 0.0        # ten minutes later
    assert door.post("/api/pair", json={"code": code}).status_code == 403


def test_a_token_is_needed_and_a_revoked_one_stops_working(door):
    assert door.get("/api/status").status_code == 401
    assert door.get("/api/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    auth = _paired(door)
    assert door.get("/api/status", headers=auth).json()["cards"] == []
    phone.revoke(phone.devices()[0]["id"])
    assert door.get("/api/status", headers=auth).status_code == 401
    assert phone.devices() == []


def test_a_device_without_the_approve_scope_can_look_but_not_answer(door, con):
    auth = _paired(door)
    with con:
        con.execute("UPDATE phone_devices SET scopes = 'status'")
    console_link.links["t1"] = FakeLink(_card())
    assert door.get("/api/status", headers=auth).json()["can_answer"] is False
    assert door.post("/api/cards/r1", json={"allow": True}, headers=auth).status_code == 403
    assert console_link.links["t1"].inbox.empty()


# ---------- cards ----------

def test_status_lists_open_cards_with_the_lead_line_not_the_input(door):
    console_link.links["t1"] = FakeLink(
        _card("r1"), _card("r2", guard="send mail, which leaves at once"),
        _card("plan", plan="a long plan"),
        {"type": "question_request", "request_id": "q1", "questions": [{"question": "Which?"}]},
        {"type": "text_delta", "text": "private reply text"})
    console_link.links["t1"].open.discard("r2")          # answered at the desk already
    got = door.get("/api/status", headers=_paired(door)).json()
    assert [c["id"] for c in got["cards"]] == ["r1"]
    card = got["cards"][0]
    assert card["tool"] == "email_schedule" and card["detail"] == "Email to Dana, Tue 9:00"
    assert card["why"].startswith("You asked me")
    assert "private" not in json.dumps(got) and got["working"] == 1


def test_an_answer_goes_to_the_tabs_inbox_as_the_desks_frame(door):
    link = console_link.links["t1"] = FakeLink(_card("r1"))
    auth = _paired(door)
    assert door.post("/api/cards/r1", json={"allow": True}, headers=auth).json() == {"ok": True}
    assert json.loads(link.inbox.get_nowait()) == {
        "type": "permission", "request_id": "r1", "allow": True, "always": False, "reason": None}
    assert link.sent == [{"type": "permission_answered", "request_id": "r1", "allow": True, "by": "phone"}]
    kinds = [e["kind"] for e in phone.state()["events"]]
    assert kinds[0] == "allowed"


def test_a_stale_or_unknown_id_is_refused_and_nothing_is_queued(door):
    link = console_link.links["t1"] = FakeLink(
        _card("r1"), _card("plan", plan="x"),
        {"type": "question_request", "request_id": "q1"})
    auth = _paired(door)
    link.open.discard("r1")
    for rid in ("r1", "plan", "q1", "never"):
        r = door.post(f"/api/cards/{rid}", json={"allow": True}, headers=auth)
        assert r.status_code == 409, rid
    assert link.inbox.empty() and link.sent == []


def test_a_deny_says_where_it_came_from(door):
    link = console_link.links["t1"] = FakeLink(_card("r1"))
    door.post("/api/cards/r1", json={"allow": False}, headers=_paired(door))
    frame = json.loads(link.inbox.get_nowait())
    assert frame["allow"] is False and frame["reason"] == "Denied from the phone."


# ---------- the push ----------

def test_subscribe_takes_only_a_real_push_service(door, con):
    auth = _paired(door)
    bad = {**SUB, "endpoint": "https://127.0.0.1:8788/api/console/notify"}
    assert door.post("/api/push/subscribe", json={"subscription": bad}, headers=auth).status_code == 422
    assert door.post("/api/push/subscribe", json={"subscription": {**SUB, "extra": "x"}},
                     headers=auth).status_code == 200
    assert json.loads(con.execute("SELECT push FROM phone_devices").fetchone()[0]) == SUB
    key = door.get("/api/push/key", headers=auth).json()["key"]
    assert len(webpush.unb64u(key)) == 65 and key == phone.vapid_keys()[1]


def test_a_card_pushes_once_after_waiting_with_nobody_at_the_desk(door, monkeypatch):
    auth = _paired(door)
    door.post("/api/push/subscribe", json={"subscription": SUB}, headers=auth)
    sent, at_desk = [], [True]
    monkeypatch.setattr(webpush, "send", lambda sub, payload, **k: sent.append(payload))
    monkeypatch.setattr(phone.notify, "at_desk", lambda: at_desk[0])
    console_link.links["t1"] = FakeLink(_card("r1"))

    def tick(now):
        return asyncio.run(phone.watch_once(now))
    assert tick(100.0) == 0                              # just appeared
    assert tick(100.0 + phone.PUSH_AFTER_S) == 0         # waited, but someone's at the desk
    at_desk[0] = False
    assert tick(101.0 + phone.PUSH_AFTER_S) == 1
    assert tick(200.0) == 0                              # once per card
    assert len(sent) == 1
    assert sent[0]["url"] == "/#card=r1" and sent[0]["body"].endswith("needs an answer")
    assert "Dana" not in json.dumps(sent[0])             # the tab, never the card's contents


def test_a_dead_subscription_is_dropped(door, monkeypatch, con):
    door.post("/api/push/subscribe", json={"subscription": SUB}, headers=_paired(door))

    def gone(*a, **k):
        raise webpush.PushError("the phone's subscription is gone", gone=True)
    monkeypatch.setattr(webpush, "send", gone)
    assert phone.push_all({"title": "x"})["failed"] == 1
    assert con.execute("SELECT push FROM phone_devices").fetchone()[0] is None


# ---------- Settings ----------

def test_settings_never_show_a_secret_and_need_an_owner_to_switch_on(router_client, monkeypatch):
    monkeypatch.setattr(phone, "tailscale", lambda: {"installed": True, "online": False, "login": ME,
                                                     "name": "macbook.tail1234.ts.net", "cli": "tailscale"})
    monkeypatch.setattr(phone, "PORT", 1)                # never really opened here: bind fails soft
    c = router_client(phone.router)
    assert c.put("/api/phone", json={"enabled": True}).status_code == 422
    assert c.post("/api/phone/pair").status_code == 409
    got = c.put("/api/phone", json={"owner_login": " Owner@Example.com ", "enabled": True}).json()
    assert got["enabled"] and got["owner_login"] == ME and got["suggested_login"] == ME
    assert got["url"] == "https://macbook.tail1234.ts.net:8443"
    assert got["serve_command"].endswith("serve --bg --https=8443 http://127.0.0.1:1")
    phone.vapid_keys()
    pairing = c.post("/api/phone/pair").json()
    assert pairing["qr"].startswith("<svg") and len(pairing["code"]) == 9
    token = phone.redeem(pairing["code"], "iPhone")
    blob = json.dumps(c.get("/api/phone").json())
    assert token not in blob and phone._hash(token) not in blob
    assert phone._get("vapid_private") not in blob
    assert c.put("/api/phone", json={"url": "javascript:alert(1)"}).status_code == 422
    dev = c.get("/api/phone").json()["devices"][0]
    c.delete(f"/api/phone/devices/{dev['id']}")
    assert c.get("/api/phone").json()["devices"] == []
    c.put("/api/phone", json={"enabled": False})
