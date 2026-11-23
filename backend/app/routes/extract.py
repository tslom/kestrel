"""Deliveries + travel routes — the typed face of
providers/email_extract.py. Extraction/refresh happens implicitly inside
email.refresh()'s cadence, so the GETs are read-only like every provider GET.

The two package writers are the exception, and deliberate: `packages` is not
a derived cache but the only record of order state this app keeps, and until
now the only hands on it were the extractor and the console. That left the
card read-only for the two corrections it most often needs — this arrived,
and this was never a real parcel — with the console the only way to make
either, which is a lot of ceremony for one button.
"""

from fastapi import APIRouter, HTTPException

from .. import db
from ..providers import email_extract

router = APIRouter(prefix="/api")

# A year out. The brief asks "am I flying tomorrow?" and 48h answers that;
# a card asks "what's booked?", and at 48h a flight bought four months ahead
# is invisible until the night before — which is exactly when a card telling
# you about it has stopped being useful.
WIDGET_TRIP_HOURS = 24 * 365
MAX_TRIP_HOURS = 24 * 730


@router.get("/packages")
def get_packages():
    """Active packages: not delivered, or delivered within the last 3 days.

    `day` rides along so the caller can ask "is this arriving today?" without
    recomputing the 4am rollover in the browser — `eta_day` is a day_key, and
    a frontend `new Date()` would disagree with it for four hours a night.
    """
    data = email_extract.active_packages()
    return {"data": data, "count": len(data), "day": db.day_key()}


@router.post("/packages/{order_key:path}/delivered")
def post_package_delivered(order_key: str):
    """Mark one package delivered by hand.

    No `delivered: false` counterpart, unlike the lists' settled/bought flips:
    those store a boolean, `packages.status` is a four-step ladder and the row
    doesn't remember which rung it was on, so there is nothing to flip back
    *to*. Undoing is `package_update` with the real status.
    """
    row = email_extract.update_package(order_key, status="delivered")
    if row is None:
        raise HTTPException(404, "no such package")
    return row


@router.delete("/packages/{order_key:path}")
def delete_package(order_key: str):
    """Drop a package row that shouldn't be on the card at all — see
    `email_extract.delete_package` for why this isn't the same as delivered,
    and for what can bring the row back."""
    try:
        deleted = email_extract.delete_package(order_key)
    except ValueError as e:      # empty key — `:path` matches /packages/
        raise HTTPException(400, str(e))
    if not deleted:
        raise HTTPException(404, "no such package")
    return {"ok": True}


@router.get("/trips")
def get_trips(hours: int = WIDGET_TRIP_HOURS):
    """Trips departing within the next `hours`. The brief calls
    `upcoming_trips()` directly and keeps its own 48h AeroAPI-poll horizon."""
    data = email_extract.upcoming_trips(max(1, min(hours, MAX_TRIP_HOURS)))
    return {"data": data, "count": len(data)}
