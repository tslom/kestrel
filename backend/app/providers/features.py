"""Features provider — the keystone cross-provider day
matrix. One row per (day, feature): a pure *denormalization* of the other
providers' cache tables into a single tall table the brief/console can reason
over. **No external calls, no LLM, no sibling-provider get()** (those trigger
on-demand syncs) — only SQL/python over rows already on disk.

A source with no data for a day leaves that (day, feature) **absent, never 0**:
a gap and a real zero must stay distinguishable for correlation. Within a
present source a 0 is real and kept (a day with texts but none sent → msgs_sent
0), but an entirely-missing source (Plaid down, no location fix) is a gap.

Day is whatever each *source* stores — behavioral sources (spotify,
messages, location) and tasks use day_key (4am rollover); health/weather/
calendar/finance use civil date. We passthrough each source's own `day` rather
than re-bucketing, so a feature value always matches what that provider's own
endpoint reports. The cost is that one `day` column holds two different notions
of a day: a pair drawn from both is joined across a boundary that can be off by
up to four hours, which matters most for late nights. FEATURE_DAY_BASIS records
which notion each feature uses, and correlations() marks such pairs
`cross_basis` so a reader can discount them rather than having to know.

`features(day, feature, value REAL, refreshed_at, PK(day,feature))` is
derived/droppable — refresh() recomputes today+yesterday, get() backfills any
never-computed day in the requested range on demand (like location.py). Because
computation is pure and cheap, an empty day (no source had data) stays absent
and is simply recomputed on the next read — same tradeoff location.py makes.
get() also recomputes days whose sources have been written since (_stale_days),
because "has any feature row" is not the same question as "has every source
that now has data for it".
"""

import json
import logging
import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from functools import partial

from .. import db
from . import calendar, finance, location, messages, plaid, ranges, spotify
from .ranges import Range

log = logging.getLogger(__name__)

NAME = "features"
METRICS = None       # free-form feature axis — any feature name is a valid --metric filter
DEFAULT_METRIC = None
ALLOW_FUTURE = False

BRIEF_SPAN_DAYS = 14  # window the daily brief reasons over

# correlations() defaults: a year of overlap, a floor under the sample size so a
# handful of coincident days can't manufacture an r, and a two-tailed p cutoff.
CORR_WINDOW_DAYS = 365
CORR_MIN_N = 20
CORR_MAX_P = 0.05

# Every feature and where it comes from. The four `_days` cache tables are pure
# passthroughs (rename only); the raw-event tables are aggregated to match each
# provider's own get() exactly (no drift). See the extractor callables below.
_PASSTHROUGH = (
    # (table, {source_metric: feature_name})
    ("health_days", {"sleep_hours": "sleep_hours", "hrv": "hrv",
                     "resting_hr": "resting_hr", "steps": "steps",
                     "active_zone_minutes": "active_zone_minutes"}),
    ("location_days", {"time_at_home_min": "time_at_home_min",
                       "places": "places", "entropy": "entropy"}),
    ("weather_days", {"tmax": "weather_tmax", "precip_prob": "precip_prob"}),
)

# Which source each feature belongs to — so correlations() can drop the
# uninteresting within-source pairs (msgs_sent ~ contacts, steps ~ zone_min)
# and keep only cross-source relationships.
_PASSTHROUGH_DOMAIN = {"health_days": "health",
                       "location_days": "location", "weather_days": "weather"}
FEATURE_DOMAIN = {feat: _PASSTHROUGH_DOMAIN[table]
                  for table, mapping in _PASSTHROUGH for feat in mapping.values()}
FEATURE_DOMAIN.update({
    "listen_min": "spotify", "novelty": "spotify",
    # "spotify", not "lastfm", even though the tags come from Last.fm: it's
    # derived from the same `plays` rows, so pairing it with listen_min or
    # novelty would surface the play count twice and read as a finding. Only
    # the genuinely cross-source pairs (sleep, HRV, time at home) survive.
    "genre_variety": "spotify",
    "repeat_ratio": "spotify", "listen_sessions": "spotify",
    "longest_listen_min": "spotify",
    "msgs_sent": "messages", "msgs_received": "messages", "contacts": "messages",
    "reply_latency": "messages",
    "spend": "finance", "calendar_events": "calendar", "busy_min": "calendar",
    "tasks_done": "tasks", "hr_var_day": "health",
})

# Which notion of "day" each domain's rows are keyed on (module docstring).
# Keyed by domain, not by feature, so there is still exactly one registry to
# update per feature — a new feature picks up its basis from FEATURE_DOMAIN,
# and test_every_domain_has_a_day_basis catches a new *domain* that forgets to
# declare one.
DOMAIN_DAY_BASIS = {
    "spotify": "day_key", "messages": "day_key",
    "location": "day_key", "tasks": "day_key",
    "health": "civil", "weather": "civil", "calendar": "civil",
    "finance": "civil",
}
FEATURE_DAY_BASIS = {feat: DOMAIN_DAY_BASIS[dom]
                     for feat, dom in FEATURE_DOMAIN.items()
                     if dom in DOMAIN_DAY_BASIS}

# hr_var_day needs enough of the day covered for its spread to mean anything;
# below this many hourly buckets the day stays a gap (not a misleading 0).
HR_MIN_HOURS = 12

# genre_variety gates: below this share of a day's plays tagged, the mix
# describes a minority of the day and its spread says more about which artists
# Last.fm knows than about what you listened to; below this many plays there
# isn't enough of a day to have a shape at all.
GENRE_MIN_COVERAGE = 0.6
GENRE_MIN_PLAYS = 5


def _marks(seq) -> str:
    return ",".join("?" * len(seq))


def _location_covered_days(con, days):
    covered = location.coverage_minutes(con, days)
    # Absent coverage is refused: this number is derived from the raw points,
    # so absent means no fixes at all. is_thin owns that rule for both callers.
    return {d for d in days if not location.is_thin(covered.get(d))}



# Coverage gates for passthrough sources whose silence isn't a zero. Keyed by
# table; each returns the subset of `days` whose numbers rest on enough data to
# mean anything, and the rest stay gaps (the HR_MIN_HOURS / GENRE_MIN_PLAYS
# rule, one source over).
#
# The sensors that ride on hardware being awake need one; the pollers don't.
# health/weather run server-side whether or not anything happens, so a 0 from
# them is an observation. Only location is in this class.
#
# OwnTracks reports *movement*: a day with a single fix computes 0 places and 0
# minutes at home, which is byte-for-byte a day spent indoors and the exact
# opposite of the truth. location.summary() already refuses such a day, so
# without this the card and the matrix disagree about the same date — a
# one-fix day is a gap on the card and a never-left-the-house observation in
# the correlations.
#
# The rule this gate exists for: a sensor's silence is only a zero if the
# sensor was watching, and "is it on?" is not answerable from the numbers it
# wrote. A sensor that looks always-on can be a hand-launched app that died
# weeks ago.
_COVERAGE_GATES = {"location_days": _location_covered_days}


def _calendar_covered_days(con, days):
    """Days the calendar cache can still answer for. `events` is not
    append-only: calendar.get() calls `_sync(missing[0], missing[-1])`, which
    DELETEs that whole span and re-inserts only what the API returned for it, so
    a past day's rows can go away entirely. v3's schema comment already named
    the marker that tells "no events that day" apart from "never fetched":
    calendar_days."""
    rows = con.execute(
        f"SELECT day FROM calendar_days WHERE day IN ({_marks(days)})", days)
    return {r["day"] for r in rows}


# Sources that can stop being able to speak for a past day at all — a different
# question from _COVERAGE_GATES above, which asks whether a day the source
# *does* cover rests on enough data to mean anything.
#
# It matters because _compute_days rebuilds a day from scratch: recomputing a
# day the calendar no longer holds would delete its calendar features and, by
# the gap-not-zero rule, republish the day as *having had no events* — the same
# confidently-wrong reading _stale_days exists to prevent, arrived at from the
# opposite direction. `features` is the only surviving record of that day's
# load, so those rows are carried forward instead (see _carried_forward).
#
# Only calendar has one. Every other source's cache table is append-only or
# rewritten per-day in place, so a past day stays answerable: plays and messages
# keep full history, health/location/weather rewrite the day they refresh,
# and Plaid's window is far wider than anything the matrix reads.
_RETENTION_GATES = {"calendar": _calendar_covered_days}

# Features per source, derived from the one FEATURE_DOMAIN registry so a new
# feature can't be forgotten here.
_DOMAIN_FEATURES: dict[str, list[str]] = defaultdict(list)
for _feat, _dom in FEATURE_DOMAIN.items():
    _DOMAIN_FEATURES[_dom].append(_feat)


def _passthrough_rows(con, days, *, table, mapping):
    """A `*_days(day, metric, value)` cache table, renamed straight through —
    less any day the table's coverage gate rejects (_COVERAGE_GATES)."""
    gate = _COVERAGE_GATES.get(table)
    if gate is not None:
        days = sorted(gate(con, days))
        if not days:
            return
    rows = con.execute(
        f"SELECT day, metric, value FROM {table}"
        f" WHERE day IN ({_marks(days)}) AND metric IN ({_marks(mapping)})",
        (*days, *mapping)).fetchall()
    for r in rows:
        yield r["day"], mapping[r["metric"]], r["value"]


def _plays_rows(con, days):
    """spotify: listen_min (sum ms) and novelty (share of plays whose artist
    was first ever heard within NOVELTY_WINDOW_DAYS) — same math as spotify.get."""
    for r in con.execute(
            f"SELECT day, ROUND(SUM(ms) / 60000.0, 1) AS v FROM plays"
            f" WHERE day IN ({_marks(days)}) GROUP BY day", days).fetchall():
        yield r["day"], "listen_min", r["v"]

    # artist_key, not artist_id — the importer's synthetic ids never match the
    # poller's real ones, so grouping on artist_id makes every artist look new
    # again at the import seam (spotify.artist_key, v35).
    spotify.ensure_artist_keys(con)
    rows = con.execute(
        f"SELECT day, artist_key, played_at FROM plays WHERE day IN ({_marks(days)})",
        days).fetchall()
    if not rows:
        return
    first_seen = {r["artist_key"]: r["played_at"] for r in con.execute(
        "SELECT artist_key, MIN(played_at) AS played_at FROM plays GROUP BY artist_key")}
    total, novel = defaultdict(int), defaultdict(int)
    for r in rows:
        total[r["day"]] += 1
        first = first_seen.get(r["artist_key"])
        if first and (date.fromisoformat(r["day"])
                      - date.fromisoformat(spotify.day_key_of(first))
                      ).days <= spotify.NOVELTY_WINDOW_DAYS:
            novel[r["day"]] += 1
    for d in total:
        yield d, "novelty", round(novel[d] / total[d], 2)


def _listen_shape_rows(con, days):
    """spotify: how the day's listening was shaped rather than how much of it
    there was — repeat_ratio (one song on loop vs shuffle), listen_sessions
    (scattered vs sustained), longest_listen_min. Its own extractor, not part
    of _plays_rows, so a failure here can't cost the day its listen_min.

    Delegated to the provider for the same reason `reply_latency` is:
    re-expressing the math here is what let `novelty` drift for twelve days
    (v35), so there is one implementation and a test pinning them together."""
    dayset = set(days)
    for day, shape in spotify.listen_shape_by_day(
            con, min(days), max(days)).items():
        if day in dayset:   # `days` needn't be contiguous
            for feature, value in shape.items():
                yield day, feature, value


def _genre_rows(con, days):
    """lastfm: `genre_variety` — normalized Shannon entropy of a day's genre
    mix (0 = everything you played was one sound, 1 = evenly spread across
    every genre that showed up).

    This is what's left of "genre as mood" once the tags are inspected: the
    vocabulary is genre words, not mood words (bare `mellow`/`upbeat`/
    `romantic` appear once each across ~2k artists, `sad` and `happy` never),
    so any tag->mood lexicon would be firing on one or two artists and
    reporting it as a mood. The *shape* of the mix survives that: locking onto
    one sound all day is a different day from wandering across eight.

    Two gates, both gap-not-zero. Coverage below GENRE_MIN_COVERAGE means the
    mix describes a minority of the day and its spread is an artifact of which
    artists happen to be tagged; fewer than GENRE_MIN_PLAYS plays means there
    isn't enough of a day to have a shape (the HR_MIN_HOURS precedent).
    """
    spotify.ensure_artist_keys(con)
    rows = con.execute(
        f"SELECT p.day, p.artist_key, COUNT(*) AS plays, t.tags"
        f" FROM plays p LEFT JOIN artist_tags t ON t.artist_key = p.artist_key"
        f" WHERE p.day IN ({_marks(days)}) AND p.artist_key IS NOT NULL"
        f" GROUP BY p.day, p.artist_key", days).fetchall()

    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    total, known = defaultdict(int), defaultdict(int)
    for r in rows:
        total[r["day"]] += r["plays"]
        tags = json.loads(r["tags"]) if r["tags"] else []
        if tags:
            known[r["day"]] += r["plays"]
        for g in tags:
            counts[r["day"]][g] += r["plays"]

    for d, n in total.items():
        if n < GENRE_MIN_PLAYS or known[d] / n < GENRE_MIN_COVERAGE:
            continue
        votes = counts[d]
        k, denom = len(votes), sum(votes.values())
        if k < 2 or not denom:
            # One sound all day is a real zero, not a gap — as long as it
            # cleared the gates above, that's a genuinely single-minded day.
            yield d, "genre_variety", 0.0
            continue
        h = -sum((c / denom) * math.log(c / denom) for c in votes.values())
        yield d, "genre_variety", round(h / math.log(k), 3)


def _messages_rows(con, days):
    """messages: sent/received counts and distinct-contacts — same as messages.get."""
    rows = con.execute(
        f"SELECT day, SUM(is_from_me) AS sent,"
        f" SUM(CASE WHEN is_from_me = 0 THEN 1 ELSE 0 END) AS received,"
        f" COUNT(DISTINCT NULLIF(handle, '')) AS contacts"
        f" FROM messages WHERE day IN ({_marks(days)}) GROUP BY day", days).fetchall()
    for r in rows:
        yield r["day"], "msgs_sent", r["sent"]
        yield r["day"], "msgs_received", r["received"]
        yield r["day"], "contacts", r["contacts"]

    # How fast you answered, not how much you said — the volume features above
    # can't tell a chatty day from a responsive one. Delegated to the provider
    # rather than re-expressed in SQL here: the same duplication is what let
    # `novelty` drift for twelve days (v35).
    dayset = set(days)
    for day, minutes in messages.reply_latency_by_day(
            con, min(days), max(days)).items():
        if day in dayset:  # `days` needn't be contiguous; the DELETE only
            yield day, "reply_latency", minutes  # covers what was asked for


def _finance_rows(con, days):
    """finance: daily outflow, transfers/loan payments excluded — same filter as
    finance.get('spend'). No zero-fill here: a $0 day is indistinguishable from
    Plaid being down, so it stays a gap (the spec's rule)."""
    env = plaid.creds()[3]
    excl = finance.SPEND_EXCLUDED_CATEGORIES
    rows = con.execute(
        f"SELECT t.day, ROUND(SUM(t.amount), 2) AS v"
        f" FROM fin_transactions t JOIN plaid_items i"
        f"  ON i.item_id = t.item_id AND i.env = ?"
        f" WHERE t.day IN ({_marks(days)}) AND t.amount > 0"
        f"  AND (t.category IS NULL OR t.category NOT IN ({_marks(excl)}))"
        f" GROUP BY t.day", (env, *days, *excl)).fetchall()
    for r in rows:
        yield r["day"], "spend", r["v"]


def _events_rows(con, days):
    """calendar: count of timed events and busy minutes (each clamped to its
    civil-day window, so an overnight event doesn't over-count). All-day events
    (birthdays/holidays — not load) are excluded from both, and so are timed
    events over a day long (a retreat, a mis-dated hold): calendar.SPAN_SQL,
    the rule the provider uses to hand those out as all-day."""
    tz = datetime.now().astimezone().tzinfo
    counts, busy = defaultdict(int), defaultdict(float)
    rows = con.execute(
        f"SELECT day, start_at, end_at FROM events"
        f" WHERE day IN ({_marks(days)}) AND all_day = 0"
        f" AND NOT {calendar.SPAN_SQL}", days).fetchall()
    for r in rows:
        counts[r["day"]] += 1
        try:
            s = datetime.fromisoformat(r["start_at"]).astimezone(tz)
            e = datetime.fromisoformat(r["end_at"]).astimezone(tz)
        except ValueError:
            continue
        d0 = datetime.combine(date.fromisoformat(r["day"]), time.min, tz)
        overlap = (min(e, d0 + timedelta(days=1)) - max(s, d0)).total_seconds() / 60
        if overlap > 0:
            busy[r["day"]] += overlap
    for d, c in counts.items():
        yield d, "calendar_events", float(c)
    for d, m in busy.items():
        yield d, "busy_min", round(m, 1)


def _tasks_rows(con, days):
    """tasks: completions per day_key. completed_at is local wall-clock, so it's
    day_key'd here (4am rollover) rather than sliced on its civil date."""
    dayset = set(days)
    lo, hi = min(days), (date.fromisoformat(max(days)) + timedelta(days=1)).isoformat()
    counts: dict[str, int] = defaultdict(int)
    rows = con.execute(
        "SELECT completed_at FROM tasks WHERE status = 'done'"
        " AND completed_at IS NOT NULL AND substr(completed_at, 1, 10) BETWEEN ? AND ?",
        (lo, hi)).fetchall()
    for r in rows:
        try:
            dk = db.day_key(datetime.fromisoformat(r["completed_at"]))
        except ValueError:
            continue
        if dk in dayset:
            counts[dk] += 1
    for d, c in counts.items():
        yield d, "tasks_done", float(c)


def _hr_var_rows(con, days):
    """intraday HR: spread (population stdev, bpm) of a day's hourly mean heart
    rate — a same-day physiological-load proxy from health_hours. A day
    with < HR_MIN_HOURS covered stays a gap: partial wear makes the spread
    unreliable and the gap-not-zero rule keeps it out of correlations."""
    by_day: dict[str, list[float]] = defaultdict(list)
    rows = con.execute(
        f"SELECT day, value FROM health_hours"
        f" WHERE metric = 'hr_hourly' AND day IN ({_marks(days)})", days).fetchall()
    for r in rows:
        by_day[r["day"]].append(r["value"])
    for d, vals in by_day.items():
        if len(vals) >= HR_MIN_HOURS:
            mean = sum(vals) / len(vals)
            sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
            yield d, "hr_var_day", round(sd, 2)


# Order is cosmetic — features are partitioned by source, so no (day,feature)
# collides across extractors. Each is guarded independently in _compute_days.
_EXTRACTORS = [
    *(partial(_passthrough_rows, table=t, mapping=m) for t, m in _PASSTHROUGH),
    _plays_rows, _listen_shape_rows, _genre_rows, _messages_rows,
    _finance_rows, _events_rows, _tasks_rows, _hr_var_rows,
]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# Source tables that stamp `refreshed_at` on every row, keyed by the same day
# the features are — watched by _stale_days so a late arrival can reach a day
# that has already been computed.
_ROW_STAMPED_SOURCES = (
    "health_days", "location_days", "weather_days",
    "plays", "messages", "fin_transactions", "health_hours",
)

# Every source _stale_days watches, as (required tables, SELECT yielding
# (day, refreshed_at)). One registry rather than two, so adding a source is one
# edit whether or not its timestamp happens to sit on the row.
#
# The three below are the ones this file's docstring used to hand back to the
# reader as "needs an explicit recompute". None of them stamps a usable
# `refreshed_at` next to the day, and each is reachable a different way.
_STALE_SOURCES = (
    *(((t,), f"SELECT day, refreshed_at FROM {t}") for t in _ROW_STAMPED_SOURCES),
    # calendar: `events` rows carry no timestamp, but _sync stamps every day it
    # rewrote into calendar_days, on the same civil date the features key on.
    # That marker re-stamps the whole trailing window each poll whether or not
    # an event changed, so the window goes stale once per poll and converges on
    # the next sweep — the health_days behaviour, not a new one.
    (("calendar_days",), "SELECT day, refreshed_at FROM calendar_days"),
    # tasks: `updated_at` comes from the tasks_touch trigger, which writes
    # datetime('now','localtime') — a *space* separator, so it sorts before
    # every ISO-T timestamp of the same second and has to be normalized before
    # it can be compared against features.refreshed_at as a string. A row
    # inserted already-done never fired the trigger, hence the created_at
    # fallback. The day is completed_at's day_key, the same 4am fold
    # _tasks_rows does in python — DAY_ROLLOVER_HOUR is interpolated so the
    # rollover stays defined in exactly one place, and
    # test_late_task_completion_folds_on_day_key_not_civil_date pins the SQL
    # against db.day_key so the two can't drift.
    (("tasks",),
     f"SELECT date(completed_at, '-{db.DAY_ROLLOVER_HOUR} hours') AS day,"
     f" replace(COALESCE(updated_at, created_at), ' ', 'T') AS refreshed_at"
     f" FROM tasks WHERE status = 'done' AND completed_at IS NOT NULL"),
    # lastfm tags: artist_tags is keyed by *artist*, so the day has to come
    # from the plays that reference it. This is the arm that was actually wrong
    # at scale — tags are fetched lazily, one artist at a time, so on a first
    # read of a long history nearly every day's genre_variety was computed
    # before the tags it rests on existed. Both halves of _genre_rows move
    # underneath: the entropy, and the GENRE_MIN_COVERAGE gate that had kept
    # under-tagged days out as gaps.
    (("plays", "artist_tags"),
     "SELECT p.day AS day, t.refreshed_at AS refreshed_at FROM plays p"
     " JOIN artist_tags t ON t.artist_key = p.artist_key"),
)


def _stale_days(con, days: list[str]) -> set[str]:
    """Of `days`, the ones whose features predate their sources.

    get()'s backfill test is per *day*: a day is "computed" if it has any
    feature row at all. So a day that had health and spotify rows when it was
    first computed is never revisited, and a source that lands afterwards — a
    provider backfill, a history re-import, a newly built source — never
    reaches it. It doesn't read as missing, either: the gap-not-zero rule means
    the matrix reports it as *the source having no data that day*, which is
    exactly the reading the data was supposed to correct.

    Location hit this: `location._ensure_days()` recovered five days of
    location_days that the nightly refresh had already computed
    features for, and all five stayed location-less in the matrix.

    A day is stale if any watched source wrote a row for it after the day's
    features were computed — every source in _STALE_SOURCES, including the
    three whose timestamp doesn't sit next to their day (calendar via
    calendar_days, tasks via the touch trigger, lastfm tags via the plays that
    reference the artist). Nothing is left needing a hand-run recompute.
    """
    if not days:
        return set()
    computed = {r["day"]: r["computed"] for r in con.execute(
        f"SELECT day, MIN(refreshed_at) AS computed FROM features"
        f" WHERE day IN ({_marks(days)}) GROUP BY day", days)}
    if not computed:
        return set()
    # Skip tables this DB hasn't got, the per-extractor guard in _compute_days
    # one layer up.
    present = {r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    lo, hi = min(computed), max(computed)
    stale: set[str] = set()
    # One statement per source, unioned here. Joined inside a single UNION
    # against a features CTE, the planner walked every source's day index end
    # to end (78-166ms on the real DB); with `day BETWEEN` pushed into each arm
    # it's an index range search per source, a few ms for all of them.
    for tables, sql in _STALE_SOURCES:
        if not all(t in present for t in tables):
            continue
        for r in con.execute(
                f"SELECT s.day AS day, MAX(s.refreshed_at) AS latest FROM ({sql}) s"
                f" WHERE s.day BETWEEN ? AND ? GROUP BY s.day", (lo, hi)):
            c = computed.get(r["day"])
            if c is not None and r["latest"] > c:
                stale.add(r["day"])
    return stale


def _carried_forward(con, days: list[str]) -> dict[str, list[str]]:
    """`{day: [feature, ...]}` that a rebuild of `days` must not delete, because
    the source behind them can no longer answer for that day (_RETENTION_GATES)
    and these rows are the only record left. Only days that actually have such
    rows appear, so the common case is an empty dict."""
    out: dict[str, set[str]] = defaultdict(set)
    for domain, gate in _RETENTION_GATES.items():
        blind = sorted(set(days) - gate(con, days))
        feats = _DOMAIN_FEATURES.get(domain) or []
        if not blind or not feats:
            continue
        rows = con.execute(
            f"SELECT day, feature FROM features WHERE day IN ({_marks(blind)})"
            f" AND feature IN ({_marks(feats)})", (*blind, *feats)).fetchall()
        for r in rows:
            out[r["day"]].add(r["feature"])
    return {d: sorted(f) for d, f in out.items()}


def _compute_days(con, days: list[str], now: str) -> None:
    """Recompute every feature for `days` from the cache tables — pure local
    SQL/python. One extractor failing (e.g. Plaid not configured) drops only
    that source's features to gaps; the rest still land.

    Features whose source can no longer speak for the day are carried forward
    rather than rebuilt (_carried_forward). They're re-stamped with `now`, which
    is both what refreshed_at means here — when this row was last confirmed as
    the matrix's best knowledge — and what keeps the day from reading stale
    forever and recomputing on every single read."""
    if not days:
        return
    keep = _carried_forward(con, days)
    plain = [d for d in days if d not in keep]
    with con:
        if plain:
            con.execute(f"DELETE FROM features WHERE day IN ({_marks(plain)})", plain)
        for day, feats in keep.items():
            con.execute(
                f"DELETE FROM features WHERE day = ?"
                f" AND feature NOT IN ({_marks(feats)})", (day, *feats))
            con.execute(
                f"UPDATE features SET refreshed_at = ? WHERE day = ?"
                f" AND feature IN ({_marks(feats)})", (now, day, *feats))
        for extractor in _EXTRACTORS:
            try:
                for day, feature, value in extractor(con, days):
                    if value is None:
                        continue
                    con.execute(
                        "INSERT OR REPLACE INTO features (day, feature, value, refreshed_at)"
                        " VALUES (?,?,?,?)", (day, feature, float(value), now))
            except Exception as e:  # degrade a source to gaps, never fail
                name = getattr(extractor, "func", extractor).__name__
                log.warning("features: extractor %s failed: %s", name, e)


def refresh() -> dict:
    """Poller entrypoint: recompute today + yesterday. Cheap, pure-local — runs
    each cycle so the day matrix trails the sources by at most one cycle."""
    today = db.day_key()
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    with db.session() as con:
        _compute_days(con, [yesterday, today], _now())
    return {"days": [yesterday, today]}


def _read(con, rng: Range):
    return con.execute(
        "SELECT day, feature, value, refreshed_at FROM features"
        " WHERE day BETWEEN ? AND ?", (rng.start_key, rng.end_key)).fetchall()


def get(rng: Range, metric: str | None = None) -> dict:
    """Read the slice from cache, backfilling any never-computed past day first
    (local-only). With `metric` set, returns that one feature's series
    ({day, value}); without it, the full matrix ({day, feature, value})."""
    with db.session() as con:
        today = db.day_key()
        wanted = {d for d in rng.days() if d <= today}
        rows = _read(con, rng)
        computed = {r["day"] for r in rows}
        redo = sorted((wanted - computed)
                      | _stale_days(con, sorted(wanted & computed)))
        if redo:
            _compute_days(con, redo, _now())
            rows = _read(con, rng)
        if metric:
            data = [{"day": r["day"], "value": r["value"]}
                    for r in rows if r["feature"] == metric]
        else:
            data = [{"day": r["day"], "feature": r["feature"], "value": r["value"]}
                    for r in rows]
        refreshed = max((r["refreshed_at"] for r in rows), default=None)
    return {"metric": metric, "data": data, "refreshed_at": refreshed}


def matrix(end_day: str, span_days: int = BRIEF_SPAN_DAYS) -> dict:
    """`{feature: {day: value}}` over the trailing `span_days` ending at
    `end_day` — the compact form the daily brief reasons over. Materializes any
    missing day in the window via get()."""
    start = (date.fromisoformat(end_day) - timedelta(days=span_days - 1)).isoformat()
    payload = get(ranges.parse_range(f"{start}..{end_day}"))
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for it in payload["data"]:
        out[it["feature"]][it["day"]] = it["value"]
    return dict(out)


# ---------- cross-source correlations ----------
#
# Pearson r between feature pairs over the days where *both* are present (the
# matrix leaves a gap, never a 0, for an absent source — so n is the true
# overlap, not a window length). Significance is the textbook two-tailed t-test
# for r, whose p-value is the regularized incomplete beta I_x(df/2, 1/2) at
# x = df/(df+t^2); we compute I_x in pure stdlib (Numerical Recipes' continued
# fraction + lgamma) rather than pull in scipy for one function.


def _betacf(a: float, b: float, x: float) -> float:
    TINY, EPS, MAXIT = 1e-300, 3e-12, 300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (TINY if abs(d) < TINY else d)
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 / (TINY if abs(1.0 + aa * d) < TINY else 1.0 + aa * d)
        c = TINY if abs(1.0 + aa / c) < TINY else 1.0 + aa / c
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 / (TINY if abs(1.0 + aa * d) < TINY else 1.0 + aa * d)
        c = TINY if abs(1.0 + aa / c) < TINY else 1.0 + aa / c
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log(1.0 - x))
    front = math.exp(lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _corr_p(r: float, n: int) -> float:
    """Two-tailed p-value for Pearson r under H0: rho = 0."""
    df = n - 2
    if df <= 0:
        return 1.0
    if abs(r) >= 1.0:
        return 0.0
    t2 = r * r * df / (1.0 - r * r)
    return _betai(df / 2.0, 0.5, df / (df + t2))


def _pearson(pairs: list[tuple[float, float]]) -> float | None:
    n = len(pairs)
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    sxx = syy = sxy = 0.0
    for x, y in pairs:
        dx, dy = x - mx, y - my
        sxx += dx * dx
        syy += dy * dy
        sxy += dx * dy
    if sxx == 0.0 or syy == 0.0:  # a constant series has no correlation
        return None
    return sxy / math.sqrt(sxx * syy)


def correlations(end_day: str, window_days: int = CORR_WINDOW_DAYS, *,
                 min_n: int = CORR_MIN_N, max_p: float = CORR_MAX_P,
                 focus: str | set[str] | None = None,
                 cross_domain: bool = False) -> list[dict]:
    """Significant feature pairs over the trailing `window_days`, ranked by
    |r| (strongest first). Each: {a, b, r, n, p}. `focus` (a feature name or
    set) keeps only pairs that touch it — e.g. focus='listen_min' for how
    listening moves with everything else. `cross_domain` drops pairs whose two
    features come from the same source (the mechanically-obvious ones).

    A pair whose two features are keyed on different notions of a day carries
    `cross_basis: True` (absent otherwise — the common case stays as it was, and
    the brief pays no tokens for it). It is a caveat on the r, not a reason to
    drop the pair: the misalignment is bounded by the 4am rollover."""
    series = matrix(end_day, window_days)
    feats = sorted(series)
    if isinstance(focus, str):
        focus = {focus}
    out: list[dict] = []
    for i, a in enumerate(feats):
        for b in feats[i + 1:]:
            if focus and a not in focus and b not in focus:
                continue
            if cross_domain and (FEATURE_DOMAIN.get(a) is not None
                                 and FEATURE_DOMAIN.get(a) == FEATURE_DOMAIN.get(b)):
                continue
            common = series[a].keys() & series[b].keys()
            if len(common) < min_n:
                continue
            r = _pearson([(series[a][d], series[b][d]) for d in common])
            if r is None:
                continue
            p = _corr_p(r, len(common))
            if p > max_p:
                continue
            pair = {"a": a, "b": b, "r": round(r, 3),
                    "n": len(common), "p": round(p, 4)}
            basis_a, basis_b = (FEATURE_DAY_BASIS.get(a), FEATURE_DAY_BASIS.get(b))
            if basis_a is not None and basis_b is not None and basis_a != basis_b:
                pair["cross_basis"] = True
            out.append(pair)
    out.sort(key=lambda c: abs(c["r"]), reverse=True)
    return out


def brief_line(it: dict) -> str:
    if "feature" in it:  # full matrix
        return f"{it['day']} {it['feature']}={it['value']}"
    return f"{it['day']} {it['value']}"
