"""Texts to yourself, routed by the model into a task, event, food, watch, list_item, note or keep.

Applies without asking; every write is an `activity` row with an Undo ("Redo as..." re-files).
Texts group into bursts (BURST_GAP / QUIET); only texts after SINCE_KEY are routed (older
history was already captured as tasks). Attachment text is untrusted: fenced, no tools, closed kinds.
Design: DESIGN.md → Unattended writes.
"""

import asyncio
import json
import re
from datetime import datetime, timedelta

from . import activity, agent, db
from .config import (GENERATED_DIR, MESSAGES_SELF_HANDLES, SELFTEXT_MODEL,
                     VAULT_PATH)

BURST_GAP = timedelta(minutes=10)
QUIET = timedelta(minutes=3)
CONTEXT_GAP = timedelta(minutes=30)   # an earlier burst this close is context
MAX_ATTEMPTS = 3
RETRY_STATUSES = ("error", "timeout")   # attachment_text states worth re-reading
PER_RUN = 6
MAX_ACTIONS = 4
SINCE_KEY = "selftext_since"
NO_THINKING = {"type": "disabled"}

KINDS = ("task", "event", "food", "watch", "list_item", "note", "keep")

SYSTEM = (
    "You file texts a person sent to their own phone number. They are notes "
    "to self: a reminder, a photo of a flyer, a photo of a meal, a movie "
    "someone recommended, a pasted announcement. Decide what each was for "
    "and answer with the action(s) that file it.\n"
    "Kinds:\n"
    "- task: something they need to do. {\"kind\":\"task\",\"title\":short "
    "imperative,\"due\":\"YYYY-MM-DD\"|null,\"due_time\":\"HH:MM\"|null}\n"
    "- event: something happening at a specific date they might attend (a "
    "party, a talk, a meeting). {\"kind\":\"event\",\"summary\":...,"
    "\"start\":\"YYYY-MM-DDTHH:MM\" or \"YYYY-MM-DD\" if no time,\"end\":same "
    "form|null,\"location\":...|null}\n"
    "- food: a meal they ate (a photo of food, or 'had a burrito'). "
    "{\"kind\":\"food\",\"meal\":\"breakfast|lunch|dinner|snack\",\"items\":"
    "[{\"item\":dish name,\"where\":restaurant or dining hall|null,"
    "\"amount\":how much was eaten if the text or photo shows it ('2 scoops', "
    "'half the bowl', '24 oz cup', '3 tenders')|null,\"amount_seen\":true if "
    "the amount is stated or plainly countable/visible, false if you guessed,"
    "\"label\":{\"serving\":...,\"kcal\":n,\"protein\":n,\"carbs\":n,\"fat\":n,"
    "\"servings_eaten\":n} only when a Nutrition Facts panel is readable|null,"
    "\"barcode\":digits only when a barcode number is readable|null}]}\n"
    "- watch: a film or show to watch. {\"kind\":\"watch\",\"title\":...,"
    "\"year\":int|null,\"type\":\"movie|show\"}\n"
    "- list_item: something to buy or pack. {\"kind\":\"list_item\",\"list\":"
    "one of their list names,\"name\":...}\n"
    "- note: information worth keeping that is not an action (an idea, a place "
    "to visit, a recommendation, an announcement with no date). "
    "{\"kind\":\"note\",\"title\":...,\"body\":markdown}\n"
    "- keep: you can't tell what it's for. {\"kind\":\"keep\",\"why\":...}\n"
    "Rules: usually one action for the whole burst — texts sent together are "
    "about one thing, and a photo plus a caption is one thing. Use several "
    "only for clearly separate things. Never invent a date, time or place "
    "that isn't in the texts; resolve 'friday' or 'tomorrow' against the "
    "sending time given. A photo of food is food unless the text says "
    "otherwise; one dish or drink is one item, so a product label's lines "
    "(brand, name, flavor) make one item, not several. When unsure between "
    "kinds, choose keep. An earlier burst may "
    "be given as context: a follow-up like 'and a white monster' continues it "
    "(same list, same kind).\n"
    "Reply with only JSON: {\"actions\":[...]}")


# ---------- the queue ----------

def _handles() -> list[str]:
    from .providers.messages import normalize_handle
    return sorted({normalize_handle(h) for h in MESSAGES_SELF_HANDLES})


def _since(con) -> str:
    r = con.execute("SELECT value FROM sync_state WHERE key=?", (SINCE_KEY,)).fetchone()
    if r:
        return r["value"]
    since = datetime.now().astimezone().isoformat(timespec="seconds")
    con.execute("INSERT INTO sync_state (key, value) VALUES (?,?)", (SINCE_KEY, since))
    return since


def _ts(s: str) -> datetime:
    t = datetime.fromisoformat(s)
    return t if t.tzinfo else t.astimezone()


def collect(now: datetime | None = None) -> int:
    """Group not-yet-routed self-texts into bursts; returns bursts opened.

    The newest group waits until QUIET since its last text (caption after photo)."""
    handles = _handles()
    if not handles:
        return 0
    now = now or datetime.now().astimezone()
    marks = ",".join("?" * len(handles))
    with db.session() as con, con:
        since = _since(con)
        rows = con.execute(
            "SELECT guid, ts FROM messages WHERE is_from_me=1 AND is_reaction=0"
            f" AND handle IN ({marks}) AND ts >= ?"
            " AND guid NOT IN (SELECT guid FROM selftext_messages)"
            " ORDER BY ts", (*handles, since)).fetchall()
        groups: list[list] = []
        for r in rows:
            if groups and _ts(r["ts"]) - _ts(groups[-1][-1]["ts"]) <= BURST_GAP:
                groups[-1].append(r)
            else:
                groups.append([r])
        if groups and now - _ts(groups[-1][-1]["ts"]) < QUIET:
            groups.pop()
        stamp = now.isoformat(timespec="seconds")
        for g in groups:
            bid = con.execute(
                "INSERT INTO selftext_bursts (first_ts, last_ts, created_at)"
                " VALUES (?,?,?)", (g[0]["ts"], g[-1]["ts"], stamp)).lastrowid
            con.executemany("INSERT INTO selftext_messages (guid, burst_id) VALUES (?,?)",
                            [(r["guid"], bid) for r in g])
    return len(groups)


def _messages(con, burst_id: int, extract: bool = False) -> list[dict]:
    """The burst's texts in order, each with its files' extracted text.
    `extract` reads files not read yet — the router's job, not the card's."""
    from .providers import attachments
    msgs = [dict(r) for r in con.execute(
        "SELECT m.guid, m.ts, m.text FROM selftext_messages s"
        " JOIN messages m ON m.guid = s.guid WHERE s.burst_id=? ORDER BY m.ts",
        (burst_id,))]
    attachments.decorate(con, msgs)
    for m in msgs:
        for f in m["attachments"]:
            # extract() caches "error" forever (usually the vision call dropping), so retry it.
            if extract and f["status"] in ("not-extracted", *RETRY_STATUSES):
                try:
                    attachments.extract(f["id"], force=f["status"] != "not-extracted")
                except Exception:  # an unreadable file is just unread
                    pass
            t = con.execute("SELECT status, text FROM attachment_text WHERE attachment_id=?",
                            (f["id"],)).fetchone()
            f["status"] = t["status"] if t else f["status"]
            f["text"] = (t["text"] if t else "") or ""
    return msgs


def _unread(msgs: list[dict]) -> list[str]:
    """Files that failed in a way worth another try."""
    return [f["filename"] or "file" for m in msgs for f in m["attachments"]
            if f["status"] in RETRY_STATUSES]


def _burst_text(msgs: list[dict]) -> str:
    """What the card shows: the texts, one per line."""
    return "\n".join(m["text"] for m in msgs if m["text"])


# ---------- routing ----------

def _prompt(msgs: list[dict], earlier: dict | None, force: str | None) -> str:
    from .providers.attachments import fenced
    first = _ts(msgs[0]["ts"])
    lists = [r["name"] for r in db.all_lists()]
    lines = [f"Sent {first:%A %Y-%m-%d} (today is {db.day_key()}).",
             f"Their lists: {', '.join(lists) or 'none yet — use Shopping'}.", ""]
    if earlier:
        lines += ["Earlier burst (context only, already filed):",
                  earlier["text"], f"→ filed as: {earlier['filed']}", ""]
    lines.append("Texts:")
    for m in msgs:
        lines.append(f"[{_ts(m['ts']):%H:%M}] {m['text']}")
        for f in m["attachments"]:
            body = fenced(f["text"], f["filename"] or "file")
            lines.append(body or f"(file {f['filename'] or ''} — couldn't be read)")
    if force:
        lines += ["", f"The person says this should be filed as a {force}. "
                      f"Reply with exactly one action of kind {force}."]
    return "\n".join(lines)


def _earlier(con, burst: dict) -> dict | None:
    r = con.execute(
        "SELECT id, last_ts FROM selftext_bursts WHERE id<>? AND last_ts<=?"
        " ORDER BY last_ts DESC LIMIT 1", (burst["id"], burst["first_ts"])).fetchone()
    if r is None or _ts(burst["first_ts"]) - _ts(r["last_ts"]) > CONTEXT_GAP:
        return None
    filed = [a["summary"] for a in con.execute(
        "SELECT summary FROM activity WHERE source='selftext' AND origin=?"
        " AND undone_at IS NULL", (str(r["id"]),))]
    return {"text": _burst_text(_messages(con, r["id"])),
            "filed": "; ".join(filed) or "nothing yet"}


def _parse(text: str) -> list[dict]:
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        raise ValueError(f"no JSON in model reply: {text[:200]!r}")
    actions = json.loads(m.group(0)).get("actions")
    if not isinstance(actions, list):
        raise ValueError("model reply has no actions list")
    return [a for a in actions if isinstance(a, dict) and a.get("kind") in KINDS]


def route(msgs: list[dict], earlier: dict | None = None,
          force: str | None = None) -> list[dict]:
    reply = asyncio.run(agent.ask(_prompt(msgs, earlier, force), model=SELFTEXT_MODEL,
                                  system=SYSTEM, thinking=NO_THINKING))
    actions = _parse(reply)
    if force:
        actions = [a for a in actions if a["kind"] == force][:1]
        if not actions:
            raise ValueError(f"couldn't read these texts as a {force}")
    return actions[:MAX_ACTIONS] or [{"kind": "keep", "why": "nothing to file"}]


# ---------- applying ----------

def _when(start: str) -> str:
    """'Fri Oct 31, 7:00pm' / 'Fri Oct 31'."""
    try:
        t = datetime.fromisoformat(start)
    except ValueError:
        return start
    day = f"{t:%a %b} {t.day}"
    if "T" not in start:
        return day
    return f"{day}, {t:%I:%M}".replace(", 0", ", ") + t.strftime("%p").lower()


def _apply_task(a: dict, msgs: list[dict]) -> tuple[str, dict]:
    title = str(a.get("title") or "").strip()
    if not title:
        raise ValueError("no task title")
    text = _burst_text(msgs)
    t = db.create_task(title=title[:120], due=a.get("due") or None,
                       due_time=a.get("due_time") or None,
                       source="capture", source_kind="message",
                       source_ref=msgs[0]["guid"], source_excerpt=text[:500] or None)
    due = f" — due {_when(t['due'])}" if t.get("due") else ""
    return f"New task: {t['title']}{due}", {"task_id": t["id"]}


def _apply_event(a: dict, msgs: list[dict]) -> tuple[str, dict]:
    from fastapi import HTTPException

    from .routes import calendar as cal
    summary, start = str(a.get("summary") or "").strip(), a.get("start")
    if not summary or not start:
        raise ValueError("the event needs a title and a date")
    try:
        ev = cal.create_event(summary=summary, start=start, end=a.get("end") or None,
                              location=a.get("location") or None,
                              description="From a text to yourself:\n" + _burst_text(msgs))
    except HTTPException as e:
        raise ValueError(e.detail)
    where = f" at {ev['location']}" if ev.get("location") else ""
    return (f"Added “{ev['summary']}” to your calendar — {_when(start)}{where}",
            {"event_id": ev["event_id"], "calendar_id": ev["calendar_id"]})


def _apply_food(a: dict, msgs: list[dict]) -> tuple[str, dict]:
    from . import food, food_estimate
    items = [i for i in (a.get("items") or [])
             if isinstance(i, dict) and str(i.get("item") or "").strip()][:6]
    if not items:
        raise ValueError("no dish named")
    meal = a.get("meal") if a.get("meal") in food.MEALS else \
        food.default_meal(_ts(msgs[0]["ts"]).replace(tzinfo=None))
    day = db.day_key(_ts(msgs[0]["ts"]).replace(tzinfo=None))
    # Best source first: barcode/label (exact), then a food known by that name, then an estimate.
    entries, todo = [], []
    for n, i in enumerate(items):
        e = _food_exact(food, i, meal, day)
        if e is None:
            known = food.match_known(i["item"], _venue_slug(i.get("where")))
            if known is not None and not i.get("amount"):
                # No amount given: the usual amount (log() carries over a guess).
                e = food.log(known["id"], meal=meal, day=day, source="selftext")
        if e is not None:
            entries.append(e)
        else:
            todo.append((n, i))
    est = food_estimate.estimate_dishes(
        [{"key": str(n), "where": i.get("where"),
          "item": i["item"].strip() + (f" (amount eaten: {i['amount']})"
                                       if i.get("amount") else "")}
         for n, i in todo]) if todo else {}
    for n, i in todo:
        r = est.get(str(n)) or {}
        if not r.get("ok"):
            continue
        known = food.match_known(i["item"], _venue_slug(i.get("where")))
        guessed = not i.get("amount_seen")
        if known is not None:
            # The estimate only sized the portion; numbers are the known food's.
            sv = (r["serving_g"] / known["serving_g"]
                  if r.get("serving_g") and known.get("serving_g") else None)
            e = food.log(known["id"], servings=sv, meal=meal, day=day,
                         source="selftext", amount_guessed=guessed)
        else:
            f = food.create_custom(i["item"].strip(), r["nutrients"],
                                   venue=_venue_slug(i.get("where")),
                                   serving_desc=r["serving_desc"], serving_g=r["serving_g"],
                                   confidence=r["confidence"],
                                   note="estimated from a text to yourself")
            e = food.log(f["id"], servings=1, meal=meal, day=day, source="selftext",
                         amount_guessed=guessed)
        entries.append(e)
    if not entries:
        raise ValueError("couldn't estimate " + ", ".join(i["item"] for i in items))
    kcal = sum(e["nutrients"].get("kcal", 0) for e in entries)
    lo = sum((e.get("range") or {}).get("kcal", [e["nutrients"].get("kcal", 0)] * 2)[0]
             for e in entries)
    hi = sum((e.get("range") or {}).get("kcal", [e["nutrients"].get("kcal", 0)] * 2)[1]
             for e in entries)
    amount = f"{lo:,.0f}–{hi:,.0f} kcal" if hi - lo >= 20 else f"{kcal:,.0f} kcal"
    asks = [e for e in entries if e.get("ask")]
    tail = (f" · how much {asks[0]['name']}? answer on the Food card" if asks else "")
    return (f"Logged {meal}: {', '.join(e['name'] for e in entries)} ({amount}){tail}",
            {"log_ids": [e["id"] for e in entries]})


def _venue_slug(where) -> str | None:
    """'Panda Express' → 'panda-express' when it's a known venue; anything
    else is kept as the free text it was (a food's venue is just a label)."""
    w = str(where or "").strip()
    if not w:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", w.lower()).strip("-")
    with db.session() as con:
        r = con.execute("SELECT slug FROM food_venues WHERE slug=? OR lower(name)=?",
                        (slug, w.lower())).fetchone()
    return r["slug"] if r else w


def _food_exact(food, i: dict, meal: str, day: str):
    """A barcode (Open Food Facts) or a Nutrition Facts panel read off the
    photo → an exact entry, or None to fall through to an estimate. A label
    whose macros don't add up to its calories (a misread digit) is refused."""
    code = "".join(ch for ch in str(i.get("barcode") or "") if ch.isdigit())
    if len(code) >= 8:
        try:
            f = food.from_barcode(code)
            return food.log(f["id"], servings=1, meal=meal, day=day, source="selftext")
        except Exception:  # unknown code or offline: try the rest
            pass
    lab = i.get("label") if isinstance(i.get("label"), dict) else None
    if not lab:
        return None
    n = {k: float(lab[k]) for k in ("kcal", "protein", "carbs", "fat")
         if isinstance(lab.get(k), (int, float)) and lab[k] >= 0}
    if not n.get("kcal"):
        return None
    if all(k in n for k in ("protein", "carbs", "fat")):
        atwater = 4 * n["protein"] + 4 * n["carbs"] + 9 * n["fat"]
        if n["kcal"] > 20 and abs(atwater - n["kcal"]) / n["kcal"] > 0.35:
            return None
    f = food.create_custom(i["item"].strip(), n, serving_desc=str(lab.get("serving")
                                                                  or "1 serving"),
                           confidence=0.95, note="read off the label in a photo")
    sv = lab.get("servings_eaten")
    return food.log(f["id"], servings=float(sv) if isinstance(sv, (int, float)) and sv > 0
                    else 1, meal=meal, day=day, source="selftext")


def _apply_watch(a: dict, msgs: list[dict]) -> tuple[str, dict]:
    title = str(a.get("title") or "").strip()
    if not title:
        raise ValueError("no title")
    year = a.get("year") if isinstance(a.get("year"), int) else None
    row = db.create_watch_item(title, year=year,
                               kind="show" if a.get("type") == "show" else "movie")
    return (f"Added {title}{f' ({year})' if year else ''} to your watchlist",
            {"id": row["id"]})


def _apply_list_item(a: dict, msgs: list[dict]) -> tuple[str, dict]:
    name = str(a.get("name") or "").strip()
    if not name:
        raise ValueError("nothing to add")
    want = str(a.get("list") or "").strip() or "Shopping"
    lst = db.find_list(want) or db.find_list("Shopping") or db.create_list(want)
    row = db.create_list_item(lst["id"], name)
    return f"Added {name} to {lst['name']}", {"id": row["id"]}


def _slug(title: str) -> str:
    return re.sub(r"[^\w\- ]+", "", title).strip()[:60] or "Note"


def _apply_note(a: dict, msgs: list[dict]) -> tuple[str, dict]:
    import frontmatter
    title = str(a.get("title") or "").strip() or "Note to self"
    body = str(a.get("body") or "").strip() or _burst_text(msgs)
    folder = VAULT_PATH / GENERATED_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{db.day_key()} {_slug(title)}"
    path, n = folder / f"{stem}.md", 2
    while path.exists():
        path, n = folder / f"{stem} {n}.md", n + 1
    post = frontmatter.Post(f"# {title}\n\n{body}\n", **{
        "origin": "generated", "generated_by": "selftext",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "reviewed": False})
    path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    rel = str(path.relative_to(VAULT_PATH))
    return f"Saved a note: {title}", {"path": rel}


def _apply_keep(a: dict, msgs: list[dict]) -> tuple[str, None]:
    why = str(a.get("why") or "").strip()
    return f"Kept as is{f' — {why}' if why else ''}", None


_APPLY = {"task": _apply_task, "event": _apply_event, "food": _apply_food,
          "watch": _apply_watch, "list_item": _apply_list_item,
          "note": _apply_note, "keep": _apply_keep}


def _file(burst_id: int, action: dict, msgs: list[dict]) -> int:
    """Apply one action and log it; a failed write logs as a keep saying why (Redo stays possible)."""
    kind = action["kind"]
    try:
        summary, ref = _APPLY[kind](action, msgs)
    except Exception as e:  # one bad action must not lose the burst
        kind, ref = "keep", None
        summary = f"Kept — couldn't make it a {activity.KINDS[action['kind']]}: {e}"
    return activity.record("selftext", kind, summary, ref, origin=str(burst_id))


def process(burst: dict) -> list[int]:
    def retry_later(why: str) -> bool:
        """Count a failed attempt; True when that was the last one allowed."""
        n = burst["attempts"] + 1
        failed = n >= MAX_ATTEMPTS
        with db.session() as con, con:
            con.execute("UPDATE selftext_bursts SET attempts=?, error=?, status=?"
                        " WHERE id=?", (n, why[:300], "failed" if failed else "pending",
                                        burst["id"]))
        return failed

    with db.session() as con:
        msgs = _messages(con, burst["id"], extract=True)
        earlier = _earlier(con, burst)
    # An unread photo is usually the point of the text: wait for the next poll, route on the last attempt.
    if (unread := _unread(msgs)) and burst["attempts"] + 1 < MAX_ATTEMPTS:
        retry_later("couldn't read " + ", ".join(unread))
        return []
    if not msgs:
        actions = []
    else:
        try:
            actions = route(msgs, earlier)
        except agent.AgentError:
            raise                      # usage limit: leave it pending, untouched
        except Exception as e:
            if retry_later(str(e)):
                return [activity.record("selftext", "keep",
                                        f"Kept — couldn't sort this out: {e}",
                                        origin=str(burst["id"]))]
            return []
    # Marked done first: a crash mid-write must not route the burst again.
    with db.session() as con, con:
        con.execute("UPDATE selftext_bursts SET status='done', processed_at=?, error=NULL"
                    " WHERE id=?", (datetime.now().isoformat(timespec="seconds"),
                                    burst["id"]))
    return [_file(burst["id"], a, msgs) for a in actions]


def run(limit: int = PER_RUN) -> dict:
    """Poller step: open bursts for new self-texts, then route what's ready."""
    opened = collect()
    if (why := agent.blocked()):
        return {"opened": opened, "skipped": why}
    with db.session() as con:
        pending = [dict(r) for r in con.execute(
            "SELECT * FROM selftext_bursts WHERE status='pending'"
            " ORDER BY first_ts LIMIT ?", (limit,))]
    filed = 0
    for b in pending:
        try:
            filed += len(process(b))
        except agent.AgentError as e:
            return {"opened": opened, "filed": filed, "skipped": str(e)}
    return {"opened": opened, "routed": len(pending), "filed": filed}


# ---------- the card ----------

def redo(row_id: int, kind: str) -> dict:
    """Undo one filed action and file the same texts again as `kind`."""
    if kind not in KINDS:
        raise ValueError(f"can't file as {kind!r}")
    row = activity.get(row_id)
    if row is None:
        raise KeyError(row_id)
    if row["source"] != "selftext" or not row["origin"]:
        raise ValueError("only a text to yourself can be re-filed")
    burst_id = int(row["origin"])
    if row["undoable"]:
        activity.undo(row_id)
    elif row["kind"] != "keep" and not row["undone_at"]:
        raise ValueError("that one can't be undone")
    with db.session() as con:
        msgs = _messages(con, burst_id, extract=True)
    if not msgs:
        raise ValueError("those texts are no longer in the message cache")
    action = (route(msgs, force=kind)[0] if kind != "keep"
              else {"kind": "keep", "why": "you chose to keep it"})
    return activity.get(_file(burst_id, action, msgs))


def decorate(rows: list[dict]) -> list[dict]:
    """Hang the texts a self-text row came from off it, plus the kinds it
    can be re-filed as. One read for the whole card."""
    ids = sorted({int(r["origin"]) for r in rows
                  if r["source"] == "selftext" and r.get("origin")})
    texts: dict[str, dict] = {}
    if ids:
        with db.session() as con:
            for bid in ids:
                msgs = _messages(con, bid)
                texts[str(bid)] = {
                    "text": _burst_text(msgs),
                    "sent_at": msgs[0]["ts"] if msgs else None,
                    "files": [{"id": f["id"], "filename": f["filename"],
                               "mime": f["mime"]}
                              for m in msgs for f in m["attachments"]]}
    for r in rows:
        if r["source"] == "selftext" and r.get("origin") in texts:
            r["origin_text"] = texts[r["origin"]]
            r["redo_kinds"] = [k for k in KINDS if k != r["kind"]]
    return rows
