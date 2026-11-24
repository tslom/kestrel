"""Deliveries + travel routes, the typed face of providers/email_extract.py.
GETs are read-only (extraction runs inside email.refresh()). The two package
writers are deliberate: `packages` is the only record of order state, and the
card needs "this arrived" / "never a real parcel" without a console round trip.
"""

from fastapi import APIRouter, HTTPException

from .. import db
from ..providers import email_extract

router = APIRouter(prefix="/api")

# A year out for the card ("what's booked?"); the brief keeps its own 48h horizon.
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
