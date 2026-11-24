"""Which models exist, which ones each Claude account may run, and what "opus" / "grunt" mean today.

Every model choice asks here (`pick` for Claude, `outside_pick` for a delegate): a model
the account can't run is swapped for one it can, with a note. Lists are derived and
refreshed every CATALOG_TTL and on an account switch; per-account access is found by
one-token probes, because /v1/models doesn't differ between Pro and Max.
Design: DESIGN.md → Models.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import claude_accounts, claude_quota, db
from .config import DEFAULT_MODEL, DEV_MODEL
from .delegates import registry

log = logging.getLogger(__name__)
router = APIRouter()

MODELS_API = "https://api.anthropic.com/v1/models?limit=1000"
MESSAGES_API = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
HTTP_TIMEOUT = 20
CATALOG_TTL = 6 * 3600
ACCESS_TTL = 7 * 86400
TICK_SECONDS = 600
FIRST_TICK_DELAY = 20

# Most to least capable: the fallback order. An unnamed new family ranks above all of these.
LADDER = ("fable", "opus", "sonnet", "haiku")
# The tiers and what each runs out of the box.
TIERS = {"plan": "fable", "decide": "opus", "grunt": "haiku"}
TIER_HINTS = {"plan": "the hardest thinking: plans, architecture",
              "decide": "judgment: reviews, choices, real coding",
              "grunt": "background jobs: triage, extraction, titles"}
MODES = ("general", "study", "dev")
OUTSIDE = tuple(p for p, prov in registry.PROVIDERS.items() if prov.outside)

_SUFFIX = re.compile(r"(\[[^\]]*\])$")
_CLAUDE_ID = re.compile(r"^claude-([a-z]+)-\d")
# The CLI's words when the API refuses a model ("Fable 5.2 requires usage credits…").
_REFUSAL = re.compile(r"requires usage credits|not available (?:on|for|with) your "
                      r"(?:plan|account|subscription)|(?:don't|do not|doesn't) have access "
                      r"to (?:this )?model", re.IGNORECASE)
_lock = threading.Lock()


# --- names ------------------------------------------------------------------------------

def family_of(name: str | None, provider: str = "claude") -> str:
    """"claude-opus-5-5" → "opus", "opus[1m]" → "opus"; an outside id loses
    its version numbers ("gpt-6.2-sol" → "gpt-sol")."""
    base = _SUFFIX.sub("", (name or "").strip().lower())
    if provider != "claude":
        return re.sub(r"-+", "-", re.sub(r"\d+(?:[.-]\d+)*", "", base)).strip("-") or base
    if m := _CLAUDE_ID.match(base):
        return m.group(1)
    return base


def _version(model_id: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", model_id))


def refusal_in(text: str | None) -> bool:
    """Whether a turn's text is the CLI saying this account can't run the model."""
    return bool(text) and bool(_REFUSAL.search(text))


# --- the lists --------------------------------------------------------------------------

def catalog(provider: str = "claude", everything: bool = False) -> list[dict]:
    """The provider's models as last fetched, best first (Claude: newest release; outside: its own
    order). Deprecated ones only with `everything`."""
    with db.session() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM model_catalog WHERE provider = ?", (provider,))]
    if not everything:
        rows = [r for r in rows if r["lifecycle"] == "active"]
    if provider == "claude":
        rows.sort(key=lambda r: (r["released"] or "", _version(r["id"])), reverse=True)
    else:
        rows.sort(key=lambda r: r["rank"])
    return rows


def fetched(provider: str) -> dict | None:
    with db.session() as con:
        row = con.execute("SELECT * FROM model_catalog_fetches WHERE provider = ?",
                          (provider,)).fetchone()
    return dict(row) if row else None


def save_catalog(provider: str, rows: list[dict] | None, note: str | None = None) -> None:
    """Replace a provider's list; None (failed fetch) keeps the last and records the attempt."""
    with db.session() as con, con:
        if rows is not None:
            con.execute("DELETE FROM model_catalog WHERE provider = ?", (provider,))
            con.executemany(
                "INSERT OR REPLACE INTO model_catalog (provider, id, family, label, released, rank,"
                " lifecycle) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(provider, r["id"], r.get("family") or family_of(r["id"], provider),
                  r.get("label"), r.get("released"), int(r.get("rank") or 0),
                  r.get("lifecycle") or "active") for r in rows])
        con.execute("INSERT INTO model_catalog_fetches (provider, at, ok, note) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(provider) DO UPDATE SET at = excluded.at, ok = excluded.ok,"
                    " note = excluded.note", (provider, time.time(), int(rows is not None), note))


def _http(token: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    """(status, JSON body) as the CLI asks; status 0 for no answer at all."""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": claude_quota.USAGE_API_BETA,
        "anthropic-version": API_VERSION, "content-type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except Exception:   # offline, DNS, timeout, garbage body
        return 0, {}


def claude_rows(payload: dict) -> list[dict] | None:
    """/v1/models' answer as catalog rows."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    return [{"id": m["id"], "family": m.get("line") or family_of(m["id"]),
             "label": re.sub(r"^Claude\s+", "", m.get("display_name") or m["id"]),
             "released": m.get("created_at"), "lifecycle": m.get("lifecycle") or "active"}
            for m in data if isinstance(m, dict) and m.get("id")]


def _active() -> str:
    """The account in the CLI's slot, as the accounts tick last saw it."""
    return claude_quota.active_key() or ""


def _stale(provider: str, ttl: float = CATALOG_TTL) -> bool:
    f = fetched(provider)
    return f is None or time.time() - f["at"] > ttl


def refresh_catalog(provider: str, force: bool = False) -> None:
    """Fetch one provider's list if it's due. Blocking."""
    if not force and not _stale(provider):
        return
    if provider == "claude":
        uuid = _active() or claude_accounts.slot_uuid(claude_accounts.read_slot()) or ""
        token = claude_accounts.token_for(uuid) if uuid else claude_quota.oauth_token()
        if not token:
            save_catalog(provider, None, "no Claude login to ask with")
            return
        status, payload = _http(token, MODELS_API)
        rows = claude_rows(payload) if status == 200 else None
        save_catalog(provider, rows, None if rows else f"/v1/models answered {status or 'nothing'}")
        return
    p = registry.PROVIDERS[provider]
    if p.models is None:
        save_catalog(provider, None, p.no_list)
    elif not shutil.which(p.binary):
        save_catalog(provider, None, f"{p.binary} isn't installed")
    else:
        rows = p.models()
        save_catalog(provider, rows, None if rows else f"`{p.binary}` gave no list")


# --- what each account may run ------------------------------------------------------------

def access(uuid: str | None) -> dict[str, dict]:
    """{family: {ok, reason, source, checked_at}} for one account."""
    if not uuid:
        return {}
    with db.session() as con:
        rows = con.execute("SELECT * FROM claude_model_access WHERE account_uuid = ?", (uuid,))
        return {r["family"]: dict(r) for r in rows}


def record(uuid: str, family: str, ok: bool, reason: str | None, source: str) -> None:
    with db.session() as con, con:
        con.execute(
            "INSERT INTO claude_model_access (account_uuid, family, ok, reason, source, checked_at)"
            " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(account_uuid, family) DO UPDATE SET"
            " ok = excluded.ok, reason = excluded.reason, source = excluded.source,"
            " checked_at = excluded.checked_at",
            (uuid, family, int(ok), reason, source, time.time()))


def verdict(status: int, payload: dict) -> tuple[bool | None, str | None]:
    """A one-token probe's answer: (True, None) runs; (False, why) this account
    may not; (None, None) says nothing either way (a rate limit, an outage)."""
    if status == 200:
        return True, None
    err = payload.get("error") if isinstance(payload, dict) else None
    err = err if isinstance(err, dict) else {}
    details = err.get("details") if isinstance(err.get("details"), dict) else {}
    notice = details.get("notice") if isinstance(details.get("notice"), dict) else {}
    why = notice.get("title") or err.get("message") or f"refused ({status})"
    if details.get("error_code") == "credits_required" or details.get("disabled_reason"):
        return False, why
    if status in (403, 404) and err.get("type") in ("permission_error", "not_found_error"):
        return False, why
    return None, None


def probe(token: str, model_id: str) -> tuple[bool | None, str | None]:
    status, payload = _http(token, MESSAGES_API, {
        "model": model_id, "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
        # The subscription login only answers a Claude Code request.
        "system": [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}]})
    return verdict(status, payload)


def refresh_access(uuid: str, force: bool = False) -> None:
    """Probe each family this account has no fresh answer for. Blocking."""
    families: dict[str, str] = {}
    for r in catalog("claude"):
        families.setdefault(r["family"], r["id"])   # newest first
    known = access(uuid)
    due = [f for f in families if force or f not in known
           or time.time() - known[f]["checked_at"] > ACCESS_TTL]
    if not due:
        return
    token = claude_accounts.token_for(uuid)
    if not token:
        return
    for fam in due:
        ok, why = probe(token, families[fam])
        if ok is not None:
            was = known.get(fam)
            record(uuid, fam, ok, why, "probe")
            if was is None or bool(was["ok"]) != ok:
                log.info("model access: %s %s %s", uuid[:8], fam, "ok" if ok else why)


def learn_refusal(model: str | None, text: str | None, uuid: str | None = None) -> str | None:
    """A turn the API refused for this account: remember the family as off
    limits and return what to run instead (None when there's nothing)."""
    uuid = uuid or _active()
    fam = family_of(model)
    if not uuid or not fam:
        return None
    record(uuid, fam, False, (text or "refused").split(".")[0][:120], "turn")
    alt = usable(model or fam, uuid)
    return alt.model if alt.model and family_of(alt.model) != fam else None


# --- resolving a name ----------------------------------------------------------------------

@dataclass
class Pick:
    model: str | None          # what to run; None = the CLI's own default
    asked: str | None = None
    notes: list[str] = field(default_factory=list)


def newest(family: str, rows: list[dict] | None = None) -> dict | None:
    return next((r for r in (rows if rows is not None else catalog("claude"))
                 if r["family"] == family), None)


def _name(row: dict | None, fallback: str) -> str:
    return (row or {}).get("label") or fallback


def resolve(name: str | None) -> Pick:
    """A tier, an alias or an id → the model to run, before any account check.
    With no list fetched yet, a name goes to the CLI as given."""
    asked = (name or "").strip() or None
    n = asked or ""
    if n.lower() in ("", "default"):
        return Pick(None, asked)
    if n.lower() in TIERS:
        n = tier_value(n.lower())
    suffix = (m.group(1) if (m := _SUFFIX.search(n)) else "")
    base = n[:len(n) - len(suffix)]
    rows = catalog("claude")
    if not rows:
        return Pick(n, asked)
    fam = family_of(base)
    if base.lower() != fam:            # an id
        if any(r["id"] == base for r in rows):
            return Pick(n, asked)
        if (row := newest(fam, rows)):
            return Pick(row["id"] + suffix, asked,
                        [f"{base} is no longer offered; using {_name(row, row['id'])}"])
        if not _CLAUDE_ID.match(base):
            return Pick(n, asked)      # not a Claude id we can judge ("opusplan")
    elif (row := newest(fam, rows)):
        return Pick(row["id"] + suffix, asked)
    # A whole family gone: the next one down that's still listed.
    start = LADDER.index(fam) + 1 if fam in LADDER else 0
    for f in LADDER[start:]:
        if (row := newest(f, rows)):
            return Pick(row["id"], asked, [f"no {fam} model is offered any more; using "
                                           f"{_name(row, row['id'])}"])
    return Pick(n, asked)


def usable(model: str, uuid: str | None) -> Pick:
    """`model` if this account may run it, else the best one down the ladder
    it may, with a note saying why."""
    fam = family_of(model)
    acc = access(uuid)
    refused = acc.get(fam)
    if refused is None or refused["ok"]:
        return Pick(model)
    who = _account_label(uuid)
    start = LADDER.index(fam) + 1 if fam in LADDER else 0
    rows = catalog("claude")
    for f in LADDER[start:]:
        if (a := acc.get(f)) is not None and not a["ok"]:
            continue
        row = newest(f, rows)
        if row or not rows:
            to = row["id"] if row else f
            return Pick(to, model, [f"{fam.title()} isn't available on {who}: "
                                    f"{refused['reason'] or 'refused'}; using {_name(row, f)}"])
    return Pick(model, model, [f"{who} may not run {fam.title()}, and no other model is known to work"])


def _account_label(uuid: str | None) -> str:
    acct = next((a for a in claude_accounts.accounts() if a["uuid"] == uuid), None)
    if not acct:
        return "this account"
    plan = {"pro": "Pro", "max": "Max"}.get(acct.get("plan") or "", acct.get("plan") or "")
    return f"{acct['label']}" + (f" ({plan})" if plan else "")


def pick(name: str | None, uuid: str | None = None) -> Pick:
    """What to run for `name` on the active account (or `uuid`): resolved, then
    swapped for one the account may run. Non-blocking (sqlite reads only)."""
    first = resolve(name)
    if first.model is None:
        return first
    second = usable(first.model, uuid if uuid is not None else _active())
    return Pick(second.model, (name or "").strip() or None, first.notes + second.notes)


def blocked(uuid: str | None = None) -> dict[str, str]:
    """{family: why} the active account (or `uuid`) may not run."""
    return {f: a["reason"] or "not available" for f, a in
            access(uuid if uuid is not None else _active()).items() if not a["ok"]}


# --- defaults -------------------------------------------------------------------------------

def builtin(key: str) -> str | None:
    kind, _, what = key.partition(":")
    if kind == "mode":
        return DEV_MODEL if what == "dev" else DEFAULT_MODEL
    if kind == "tier":
        return TIERS.get(what)
    return None


def default_keys() -> list[str]:
    return ([f"mode:{m}" for m in MODES] + [f"tier:{t}" for t in TIERS]
            + [f"provider:{p}" for p in OUTSIDE])


def stored(key: str) -> str | None:
    with db.session() as con:
        row = con.execute("SELECT value FROM model_defaults WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def default_for(key: str) -> str | None:
    return stored(key) or builtin(key)


def tier_value(tier: str) -> str:
    value = default_for(f"tier:{tier}") or TIERS[tier]
    return TIERS[tier] if value.lower() in TIERS else value


def mode_default(profile: str) -> str:
    """What a console tab of this mode runs when it names no model: a name
    (alias, tier or id) that `pick` turns into a model."""
    return default_for(f"mode:{profile if profile in MODES else 'general'}") or DEFAULT_MODEL


class DefaultError(ValueError):
    pass


def claude_names() -> list[str]:
    """What a Claude model may be called right now: the tiers, every family
    listed, and every active id. Without a list, the ladder stands in."""
    rows = catalog("claude")
    return [*TIERS, *(families(rows) or LADDER), *(r["id"] for r in rows)]


def set_default(key: str, value: str | None) -> None:
    if key not in default_keys():
        raise DefaultError(f"no setting {key!r}")
    value = (value or "").strip()
    if value:
        kind, _, what = key.partition(":")
        if kind == "provider":
            ids = [r["id"] for r in catalog(what)]
            if ids and value not in ids:
                raise DefaultError(f"{registry.PROVIDERS[what].label} has no model {value!r}")
        else:
            names = claude_names()
            if kind == "tier":
                names = [n for n in names if n not in TIERS]
            base = _SUFFIX.sub("", value)
            if base not in names and base.lower() not in names:
                raise DefaultError(f"no Claude model {value!r}; it takes: "
                                   + ", ".join(n for n in names if not n.startswith("claude-")))
    with db.session() as con, con:
        if value:
            con.execute("INSERT INTO model_defaults (key, value) VALUES (?, ?) ON CONFLICT(key)"
                        " DO UPDATE SET value = excluded.value", (key, value))
        else:
            con.execute("DELETE FROM model_defaults WHERE key = ?", (key,))


# --- an outside provider ----------------------------------------------------------------------

def outside_pick(provider: str, name: str | None) -> tuple[Pick | None, str | None]:
    """(Pick, None), or (None, why) when neither the model nor its family is listed.
    Empty `name`: Settings default, else the CLI's own. A retired id runs as its family's newest, with a note."""
    asked = (name or "").strip()
    n = asked or default_for(f"provider:{provider}") or ""
    if not n:
        return Pick(None), None
    rows = catalog(provider)
    if not rows or any(r["id"] == n for r in rows):
        return Pick(n, asked or None), None
    fam = family_of(n, provider)
    same = [r for r in rows if r["family"] == fam]
    if same:
        row = max(same, key=lambda r: (_version(r["id"]), -r["rank"]))
        return Pick(row["id"], asked or None, [f"{n} isn't offered any more; using {row['id']}"]), None
    label = registry.PROVIDERS[provider].label
    if not asked:   # a stale default: the CLI's own rather than a failure
        return Pick(None, None, [f"the default {n} isn't offered by {label} any more; "
                                 "using its own default"]), None
    return None, f"{label} has no model '{n}'; it has: " + ", ".join(r["id"] for r in rows[:15])


# --- keeping it fresh -------------------------------------------------------------------------

def refresh_all(force: bool = False) -> None:
    """Every list that's due, then every saved account's access. Blocking."""
    with _lock:
        for p in ("claude", *OUTSIDE):
            try:
                refresh_catalog(p, force)
            except Exception:   # one provider's failure isn't the others'
                log.exception("model list refresh failed: %s", p)
        for a in claude_accounts.accounts():
            if a.get("enabled", 1) and not a.get("needs_login"):
                try:
                    refresh_access(a["uuid"], force)
                except Exception:
                    log.exception("model access probe failed: %s", a["uuid"][:8])


def _on_switch(uuid: str) -> None:
    """The account changed: the list again, and what this one may run."""
    def run():
        with _lock:
            try:
                refresh_catalog("claude", force=True)
                refresh_access(uuid, force=True)
            except Exception:
                log.exception("model recheck after a switch failed")
    threading.Thread(target=run, name="model-recheck", daemon=True).start()


claude_accounts.on_switch(_on_switch)


async def loop() -> None:
    await asyncio.sleep(FIRST_TICK_DELAY)
    while True:
        try:
            await asyncio.to_thread(refresh_all)
        except Exception:   # the loop must outlive any one bad tick
            log.exception("model catalog tick failed")
        await asyncio.sleep(TICK_SECONDS)


# --- routes ---------------------------------------------------------------------------------------

def families(rows: list[dict] | None = None) -> list[str]:
    """Every family listed, most capable first: one the ladder doesn't know
    (new) ahead of it, then the ladder's order."""
    fams = dict.fromkeys(r["family"] for r in (rows if rows is not None else catalog("claude")))
    return sorted(fams, key=lambda f: LADDER.index(f) + 1 if f in LADDER else 0)


def choices() -> list[dict]:
    """The Claude pickers' rows, in console.session_models' shape: one alias
    per family (its newest), then every active id pinned."""
    rows = catalog("claude")
    out = [{"value": f, "resolved": r["id"], "label": r["label"], "family": f, "efforts": []}
           for f in families(rows) if (r := newest(f, rows))]
    out += [{"value": r["id"], "resolved": r["id"], "label": r["label"],
             "family": r["family"], "efforts": []} for r in rows]
    return out


def _default_row(key: str) -> dict:
    kind, _, what = key.partition(":")
    value, base = stored(key), builtin(key)
    row = {"key": key, "kind": kind, "name": what, "value": value, "builtin": base,
           "hint": TIER_HINTS.get(what, "")}
    if kind == "provider":
        p, why = outside_pick(what, None)
        row.update(runs=p.model if p else None, notes=(p.notes if p else [why]),
                   label=registry.PROVIDERS[what].label)
    else:
        p = pick(value or base)
        row.update(runs=p.model, notes=p.notes, label=what)
    return row


@router.get("/api/models")
def get_models() -> dict:
    active = _active()
    accts = claude_accounts.accounts()
    fams = families()
    return {
        "claude": {"models": catalog("claude"), "choices": choices(), "fetched": fetched("claude"),
                   "families": fams, "tiers": list(TIERS), "active": active,
                   "blocked": blocked(active),
                   "accounts": [{"uuid": a["uuid"], "label": a["label"], "plan": a.get("plan"),
                                 "access": access(a["uuid"])} for a in accts]},
        "providers": {p: {"label": registry.PROVIDERS[p].label, "models": catalog(p),
                          "fetched": fetched(p)} for p in OUTSIDE},
        "defaults": [_default_row(k) for k in default_keys()],
    }


class DefaultIn(BaseModel):
    key: str
    value: str | None = None


@router.put("/api/models/defaults")
def put_default(body: DefaultIn) -> dict:
    try:
        set_default(body.key, body.value)
    except DefaultError as e:
        raise HTTPException(400, str(e)) from e
    return get_models()


@router.post("/api/models/refresh")
async def post_refresh() -> dict:
    await asyncio.to_thread(refresh_all, True)
    return get_models()
