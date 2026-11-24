"""Debts (and beers), user lists and watchlist, app.db-truth (the old vault's
markdown tables are not read). Shared mechanics are db.list_rows / set_row_done /
delete_row; only create is per-domain. User lists are `lists` + `list_items`
rows the user makes, so they carry extra list-level routes.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db

router = APIRouter(prefix="/api")


def _flip(table: str, row_id: int, done: bool, missing: str):
    row = db.set_row_done(table, row_id, done)
    if row is None:
        raise HTTPException(404, missing)
    return row


def _drop(table: str, row_id: int, missing: str):
    if not db.delete_row(table, row_id):
        raise HTTPException(404, missing)
    return {"ok": True}


class DebtIn(BaseModel):
    person: str
    amount: float
    direction: str = "owed_to_me"   # owed_to_me | i_owe
    reason: str | None = None
    person_key: str | None = None   # people.person_key, when they're in the CRM
    incurred_on: str | None = None  # YYYY-MM-DD


class SettledIn(BaseModel):
    settled: bool = True


@router.get("/debts")
def get_debts():
    return db.debt_summary()


@router.post("/debts")
def post_debt(body: DebtIn):
    try:
        return db.create_debt(**body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/debts/{debt_id}/settled")
def post_debt_settled(debt_id: int, body: SettledIn):
    return _flip("debts", debt_id, body.settled, "no such debt")


@router.post("/debts/{debt_id}/reminded")
def post_debt_reminded(debt_id: int):
    """You've just chased this one — go quiet for DEBT_NUDGE_DAYS, then ask
    again. Settling is the only thing that makes a debt go away for good;
    this only buys it a week."""
    debt = db.remind_debt(debt_id)
    if debt is None:
        raise HTTPException(404, "no such debt")
    return debt


@router.delete("/debts/{debt_id}")
def delete_debt(debt_id: int):
    return _drop("debts", debt_id, "no such debt")


# Beers: own table (no dollar value), so outside the owed-to-you total and nudges.

class BeerIn(BaseModel):
    person: str
    count: int = 1
    direction: str = "owed_to_me"
    reason: str | None = None


class BeerSettleIn(BaseModel):
    person: str
    direction: str = "owed_to_me"


@router.get("/beers")
def get_beers():
    return db.beer_summary()


@router.post("/beers")
def post_beers(body: BeerIn):
    try:
        return {"beers": db.add_beers(**body.model_dump())}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/beers/settle")
def post_beer_settle(body: BeerSettleIn):
    """Settle the oldest open beer with this person: the card's check."""
    row = db.settle_beer(body.person, body.direction)
    if row is None:
        raise HTTPException(404, f"no open beer with {body.person}")
    return row


@router.delete("/beers/{beer_id}")
def delete_beer(beer_id: int):
    return _drop("beers", beer_id, "no such beer")


class ListIn(BaseModel):
    name: str


class ItemIn(BaseModel):
    name: str
    category: str | None = None
    note: str | None = None


class DoneIn(BaseModel):
    done: bool = True


@router.get("/lists")
def get_lists(include_done: bool = False):
    """Every list with its items nested. The card draws all the chips and
    switches between them without a second fetch, which is why this is one
    payload rather than a list index plus a per-list read."""
    return {"lists": db.all_lists(include_done=include_done)}


@router.post("/lists")
def post_list(body: ListIn):
    try:
        return db.create_list(body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.patch("/lists/{list_id}")
def patch_list(list_id: int, body: ListIn):
    try:
        row = db.rename_list(list_id, body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if row is None:
        raise HTTPException(404, "no such list")
    return row


@router.delete("/lists/{list_id}")
def delete_list(list_id: int):
    """Deletes the list and everything on it — the items go by FK cascade."""
    if not db.delete_list(list_id):
        raise HTTPException(404, "no such list")
    return {"ok": True}


@router.post("/lists/{list_id}/items")
def post_list_item(list_id: int, body: ItemIn):
    try:
        return db.create_list_item(list_id, **body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/list-items/{item_id}/done")
def post_item_done(item_id: int, body: DoneIn):
    return _flip("list_items", item_id, body.done, "no such item")


@router.delete("/list-items/{item_id}")
def delete_list_item(item_id: int):
    return _drop("list_items", item_id, "no such item")


class WatchIn(BaseModel):
    title: str
    year: int | None = None
    kind: str = "movie"             # movie | show
    services: str | None = None     # where it streams, comma-separated
    note: str | None = None


class WatchedIn(BaseModel):
    watched: bool = True


@router.get("/watchlist")
def get_watchlist(include_watched: bool = False):
    return {"items": db.list_rows("watchlist", include_done=include_watched)}


@router.post("/watchlist")
def post_watch(body: WatchIn):
    try:
        return db.create_watch_item(**body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/watchlist/{item_id}/watched")
def post_watched(item_id: int, body: WatchedIn):
    return _flip("watchlist", item_id, body.watched, "no such item")


@router.delete("/watchlist/{item_id}")
def delete_watch(item_id: int):
    return _drop("watchlist", item_id, "no such item")
