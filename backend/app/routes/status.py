"""GET /api/status: per-source health joining auth, last refresh (heartbeat.py,
poller-written) and cache freshness. Read-only; the only live probes are
localhost ones a cache can't answer. States are heartbeat.py's four (ok / stale /
broken / off). No secrets: auth rows come from the masked status() helpers.
Design: DESIGN.md → Source status (`/api/status`).
"""

import json
import os
import sqlite3
from datetime import datetime

from fastapi import APIRouter

from .. import db
from ..config import (
    APP_DB, GMAIL_ACCOUNT, GOOGLE_CALENDAR_ACCOUNT, GOOGLE_HEALTH_ACCOUNT,
    PROVIDER_REFRESH_SECONDS, VAULT_PATH,
)
from ..providers import REGISTRY, heartbeat, oauth, plaid, spotify

router = APIRouter(prefix="/api")

# Which login each source rides on. Shared accounts show one dead token on every source using it (intended).
GOOGLE_AUTH: dict[str, str] = {
    "calendar": GOOGLE_CALENDAR_ACCOUNT,
    "email": GMAIL_ACCOUNT,
    "health": GOOGLE_HEALTH_ACCOUNT,
}

# Worst-first, so the panel leads with what needs doing.
RANK = {"broken": 0, "stale": 1, "off": 2, "ok": 3}

# Event log slice per response: newest rows scanned once, bucketed by provider.
EVENTS_SCANNED = 120
EVENTS_PER_SOURCE = 5


def _worst(*states: str) -> str:
    return min((s for s in states if s), key=lambda s: RANK.get(s, 9), default="ok")


def _google_auth(source: str) -> dict | None:
    account = GOOGLE_AUTH.get(source)
    if account is None:
        return None
    row = oauth.status("google", account) or {}
    authorized = bool(row.get("has_refresh_token"))
    can_connect = oauth.stored_client("google", account) is not None
    return {
        "kind": "google", "account": account, "authorized": authorized,
        "can_connect": can_connect, "updated_at": row.get("updated_at"),
        # never bootstrapped = not set up, not failed (needs the CLI once for client_secret json)
        "state": "ok" if authorized else ("broken" if can_connect else "off"),
        "detail": "connected" if authorized else (
            f"google/{account} needs re-auth — refresh tokens expire ~weekly"
            " on a Testing-mode app" if can_connect else
            f"google/{account} never authorized — bootstrap once via"
            " `python -m app.providers.oauth authorize`"),
        "action": {"kind": "oauth", "account": account} if not authorized and can_connect else None,
    }


def _spotify_auth() -> dict:
    row = spotify.status()
    connected = bool(row.get("connected"))
    return {
        "kind": "spotify", "account": "personal", "authorized": connected,
        "can_connect": bool(row.get("client_id")), "updated_at": row.get("updated_at"),
        "state": "ok" if connected else "off",
        "detail": "connected" if connected else "not connected",
        "action": None if connected else {"kind": "spotify"},
    }


def _plaid_auth() -> dict:
    st = plaid.status()
    if not st.get("configured"):
        return {"kind": "plaid", "account": None, "authorized": False,
                "can_connect": False, "state": "off",
                "detail": "no Plaid credentials stored", "action": None}
    items = st.get("items", [])
    bad = [i for i in items if (i.get("status") or "ok") != "ok"]
    if not items:
        # first-ever link needs the Finance card's own flow
        return {"kind": "plaid", "account": None, "authorized": False,
                "can_connect": True, "state": "off",
                "detail": f"{st['env']}: no bank linked yet — link one from"
                          " the Finance card", "action": None}
    return {
        "kind": "plaid", "account": st.get("env"), "authorized": not bad,
        "can_connect": True,
        "updated_at": max((i.get("last_synced_at") or "" for i in items), default=None) or None,
        "state": "broken" if bad else "ok",
        "detail": (f"{len(bad)} of {len(items)} item(s) need reconnecting: "
                   + ", ".join(i["institution"] or i["item_id"] for i in bad)) if bad
                  else f"{len(items)} item(s) linked ({st['env']})",
        "items": items,
        # update mode: re-auths the broken item rather than adding a duplicate
        "action": {"kind": "plaid", "item_id": bad[0]["item_id"]} if bad else None,
    }


def _auth_for(source: str) -> dict | None:
    if source in GOOGLE_AUTH:
        return _google_auth(source)
    if source == "spotify":
        return _spotify_auth()
    if source == "finance":
        return _plaid_auth()
    return None


def _names(value: object) -> str:
    """Names from a provider's "couldn't reach" note (dict keyed by host, list or string)."""
    if isinstance(value, dict):
        return ", ".join(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def _notes(info_json: str | None) -> tuple[list[str], str | None]:
    """Partial trouble from a provider's info dict. Only `unreachable` implies a
    state (stale). `skipped` stays a note: a benign skip can't be told from a
    damning one, so sources where skip is failure say so in their health() probe."""
    if not info_json:
        return [], None
    try:
        info = json.loads(info_json)
    except (ValueError, TypeError):
        return [], None
    if not isinstance(info, dict):
        return [], None
    notes, state = [], None
    if skipped := info.get("skipped"):
        notes.append(f"skipped: {skipped}")
    if unreachable := info.get("unreachable"):
        notes.append(f"unreachable: {_names(unreachable)}")
        state = "stale"
    return notes, state


def _source_row(con: sqlite3.Connection, source: str, mod, health_rows: dict) -> dict:
    hb = health_rows.get(source)
    auth = _auth_for(source)
    out: dict = {
        "source": source,
        "refreshable": hasattr(mod, "refresh"),
        "auth": auth,
        "action": (auth or {}).get("action"),
        "notes": [],
    }
    states, details = [], []

    if auth:
        states.append(auth["state"])
        if auth["state"] != "ok":
            details.append(auth["detail"])

    # --- last refresh outcome (poller-written) ---
    if hb:
        out.update({
            "ok": bool(hb["ok"]), "error": hb["error"], "fails": hb["fails"],
            "duration_ms": hb["duration_ms"],
            "last_attempt_at": hb["last_attempt_at"], "last_ok_at": hb["last_ok_at"],
            "last_attempt_ago": heartbeat.since(hb["last_attempt_at"]),
            "last_ok_ago": heartbeat.since(hb["last_ok_at"]),
            # the last 24 refreshes, oldest first: o ok · x failed · . skipped
            "recent": hb["recent"],
            "persistently_failing": heartbeat.broken(hb["recent"]),
        })
        if not hb["ok"]:
            states.append("broken")
            details.append(f"last refresh failed ({hb['fails']}×): {hb['error']}")
        notes, note_state = _notes(hb["info"])
        out["notes"] += notes
        if note_state:
            # state only, not detail: the health() probe words it better
            states.append(note_state)
    elif out["refreshable"]:
        # no row = no completed cycle since boot; not an error, the cache check still runs
        out["notes"].append("no refresh recorded yet this boot")

    # --- cache freshness: the check that catches silent success ---
    if fresh := heartbeat.FRESHNESS.get(source):
        table, col, max_age = fresh
        newest, rows = heartbeat.newest_day(con, table, col)
        behind = heartbeat.age_days(newest)
        out.update({"newest_day": newest, "days_behind": behind, "rows": rows,
                    "stale_after_days": max_age})
        if rows == 0:
            states.append("stale")
            details.append(f"{table} is empty — nothing has ever synced")
        elif behind is not None and behind > max_age:
            states.append("stale")
            details.append(f"newest data is {behind}d old ({newest})")

    # --- provider's own probe, last word: it knows things nothing else can ---
    if hook := getattr(mod, "health", None):
        try:
            probe = hook()
        except Exception as e:  # a probe must never 500 the panel
            probe = {"state": "broken", "detail": f"health probe raised: {e!r}"}
        out["probe"] = probe
        states.append(probe.get("state", "ok"))
        if probe.get("state") != "ok" or not details:
            details.append(probe.get("detail", ""))

    out["state"] = _worst(*states)
    if out["state"] == "ok" and not details:
        # No detail beats one repeating the state column (poller-less sources).
        ago = out.get("last_ok_ago")
        if ago:
            details.append(f"synced {ago}")
    out["detail"] = "; ".join(d for d in details if d)
    return out


def _infra() -> list[dict]:
    """Non-provider things that break the same way (vault, db, ...)."""
    out = []

    vault_ok = VAULT_PATH.is_dir()
    out.append({
        "name": "vault", "state": "ok" if vault_ok else "broken",
        "detail": str(VAULT_PATH) if vault_ok
                  else f"VAULT_PATH does not exist: {VAULT_PATH}",
    })

    try:
        size = os.path.getsize(APP_DB) / 1e6
        with db.session() as con:
            v = con.execute("SELECT v FROM schema_version").fetchone()
        out.append({"name": "app.db", "state": "ok",
                    "detail": f"schema v{v['v'] if v else 0}, {size:.1f} MB"})
    except (OSError, sqlite3.Error) as e:
        out.append({"name": "app.db", "state": "broken", "detail": repr(e)})

    return out


@router.get("/status")
def status():
    """Per-source health for the status panel: auth, last refresh, freshness,
    and the recent event log — provider_health only ever holds the current
    cycle, so without the log a fault that healed itself leaves no trace."""
    health_rows = heartbeat.rows()
    log: dict[str, list[dict]] = {}
    for e in heartbeat.events(limit=EVENTS_SCANNED):
        log.setdefault(e["provider"], []).append(
            {"at": e["at"], "ago": heartbeat.since(e["at"]),
             "kind": e["kind"], "detail": e["detail"]})
    with db.session() as con:
        sources = [_source_row(con, name, mod, health_rows) |
                   {"events": log.get(name, [])[:EVENTS_PER_SOURCE]}
                   for name, mod in REGISTRY.items()]
    sources.sort(key=lambda s: (RANK.get(s["state"], 9), s["source"]))
    infra = _infra()

    counts: dict[str, int] = {}
    for row in sources + infra:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "refresh_seconds": PROVIDER_REFRESH_SECONDS,
        "worst": _worst(*(r["state"] for r in sources + infra)),
        "counts": counts,
        "sources": sources,
        "infra": infra,
    }
