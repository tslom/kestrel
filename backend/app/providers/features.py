"""Features provider: the cross-provider (day, feature) matrix.

Derived/droppable denormalization of the other providers' cache tables. No external
calls, no LLM, no sibling-provider get() (those trigger syncs). A source with no data
for a day leaves the feature absent, never 0. `day` is each source's own key (day_key
or civil), see FEATURE_DAY_BASIS.
Design: DESIGN.md → §11 Feature matrix.
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

# correlations() defaults: window, minimum overlap n, two-tailed p cutoff.
CORR_WINDOW_DAYS = 365
CORR_MIN_N = 20
CORR_MAX_P = 0.05

_PASSTHROUGH = (
    # (table, {source_metric: feature_name})
    ("health_days", {"sleep_hours": "sleep_hours", "hrv": "hrv",
                     "resting_hr": "resting_hr", "steps": "steps",
                     "active_zone_minutes": "active_zone_minutes"}),
    ("location_days", {"time_at_home_min": "time_at_home_min",
                       "places": "places", "entropy": "entropy"}),
    ("weather_days", {"tmax": "weather_tmax", "precip_prob": "precip_prob"}),
)

# Source per feature; correlations(cross_domain=True) drops within-source pairs.
_PASSTHROUGH_DOMAIN = {"health_days": "health",
                       "location_days": "location", "weather_days": "weather"}
FEATURE_DOMAIN = {feat: _PASSTHROUGH_DOMAIN[table]
                  for table, mapping in _PASSTHROUGH for feat in mapping.values()}
FEATURE_DOMAIN.update({
    "listen_min": "spotify", "novelty": "spotify",
    # "spotify" not "lastfm": same `plays` rows, so a lastfm domain would pair it with
    # listen_min/novelty and double-count plays as a finding.
    "genre_variety": "spotify",
    "repeat_ratio": "spotify", "listen_sessions": "spotify",
    "longest_listen_min": "spotify",
    "msgs_sent": "messages", "msgs_received": "messages", "contacts": "messages",
    "reply_latency": "messages",
    "spend": "finance", "calendar_events": "calendar", "busy_min": "calendar",
    "tasks_done": "tasks", "hr_var_day": "health",
})

# Keyed by domain so a new feature takes its basis from FEATURE_DOMAIN;
# test_every_domain_has_a_day_basis catches a new domain with none.
DOMAIN_DAY_BASIS = {
    "spotify": "day_key", "messages": "day_key",
    "location": "day_key", "tasks": "day_key",
    "health": "civil", "weather": "civil", "calendar": "civil",
    "finance": "civil",
}
FEATURE_DAY_BASIS = {feat: DOMAIN_DAY_BASIS[dom]
                     for feat, dom in FEATURE_DOMAIN.items()
                     if dom in DOMAIN_DAY_BASIS}

# Below this many hourly buckets hr_var_day stays a gap.
HR_MIN_HOURS = 12

# genre_variety gap gates: min tagged share of a day's plays, min plays.
GENRE_MIN_COVERAGE = 0.6
GENRE_MIN_PLAYS = 5


def _marks(seq) -> str:
    return ",".join("?" * len(seq))


def _location_covered_days(con, days):
    covered = location.coverage_minutes(con, days)
    return {d for d in days if not location.is_thin(covered.get(d))}



# table -> fn(con, days) returning the days with enough data to mean anything; the
# rest stay gaps. Only location needs one (a one-fix day reads as "never left home").
_COVERAGE_GATES = {"location_days": _location_covered_days}


def _calendar_covered_days(con, days):
    """Days the calendar cache can still answer for (`calendar_days` marks synced days;
    `events` rows alone can't tell "no events" from "dropped")."""
    rows = con.execute(
        f"SELECT day FROM calendar_days WHERE day IN ({_marks(days)})", days)
    return {r["day"] for r in rows}


# Sources that can lose a past day entirely (only calendar): a rebuild would republish
# it as "no events", so _carried_forward keeps those features instead.
_RETENTION_GATES = {"calendar": _calendar_covered_days}

_DOMAIN_FEATURES: dict[str, list[str]] = defaultdict(list)
for _feat, _dom in FEATURE_DOMAIN.items():
    _DOMAIN_FEATURES[_dom].append(_feat)


def _passthrough_rows(con, days, *, table, mapping):
    """A `*_days(day, metric, value)` table renamed through, minus days its _COVERAGE_GATES rejects."""
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

    # artist_key, not artist_id: importer ids never match the poller's (v35).
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
    """spotify: repeat_ratio, listen_sessions, longest_listen_min, via the provider's own
    math. Separate from _plays_rows so a failure here can't cost the day its listen_min."""
    dayset = set(days)
    for day, shape in spotify.listen_shape_by_day(
            con, min(days), max(days)).items():
        if day in dayset:   # `days` needn't be contiguous
            for feature, value in shape.items():
                yield day, feature, value


def _genre_rows(con, days):
    """lastfm: `genre_variety`, normalized Shannon entropy of a day's genre mix (0 = one
    sound, 1 = evenly spread). Days under GENRE_MIN_COVERAGE / GENRE_MIN_PLAYS are gaps."""
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
            # One sound all day (past the gates) is a real zero, not a gap.
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

    # The provider's own math, not SQL here (a second copy let novelty drift, v35).
    dayset = set(days)
    for day, minutes in messages.reply_latency_by_day(
            con, min(days), max(days)).items():
        if day in dayset:  # `days` needn't be contiguous
            yield day, "reply_latency", minutes


def _finance_rows(con, days):
    """finance: daily outflow, same filter as finance.get('spend'). No zero-fill: a $0 day
    can't be told from Plaid being down."""
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
    """calendar: timed-event count and busy minutes, clamped to the civil day. All-day and
    over-a-day events (calendar.SPAN_SQL) are excluded."""
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
    """tasks: completions per day_key (completed_at is local wall-clock, not a civil slice)."""
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
    """intraday HR: population stdev (bpm) of hourly mean HR; < HR_MIN_HOURS stays a gap."""
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


_EXTRACTORS = [
    *(partial(_passthrough_rows, table=t, mapping=m) for t, m in _PASSTHROUGH),
    _plays_rows, _listen_shape_rows, _genre_rows, _messages_rows,
    _finance_rows, _events_rows, _tasks_rows, _hr_var_rows,
]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# Tables with `refreshed_at` on each row, keyed by the features' own day.
_ROW_STAMPED_SOURCES = (
    "health_days", "location_days", "weather_days",
    "plays", "messages", "fin_transactions", "health_hours",
)

# Every source _stale_days watches: (required tables, SELECT yielding (day, refreshed_at)).
_STALE_SOURCES = (
    *(((t,), f"SELECT day, refreshed_at FROM {t}") for t in _ROW_STAMPED_SOURCES),
    # calendar: `events` has no timestamp; calendar_days is stamped per synced day.
    (("calendar_days",), "SELECT day, refreshed_at FROM calendar_days"),
    # tasks: the trigger's space-separated `updated_at` is normalized to ISO-T to compare
    # as a string; created_at covers rows inserted already-done. The day is the 4am fold
    # (test_late_task_completion_folds_on_day_key_not_civil_date pins it to db.day_key).
    (("tasks",),
     f"SELECT date(completed_at, '-{db.DAY_ROLLOVER_HOUR} hours') AS day,"
     f" replace(COALESCE(updated_at, created_at), ' ', 'T') AS refreshed_at"
     f" FROM tasks WHERE status = 'done' AND completed_at IS NOT NULL"),
    # lastfm tags: keyed by artist, so the day comes from the plays that reference it.
    (("plays", "artist_tags"),
     "SELECT p.day AS day, t.refreshed_at AS refreshed_at FROM plays p"
     " JOIN artist_tags t ON t.artist_key = p.artist_key"),
)


def _stale_days(con, days: list[str]) -> set[str]:
    """Of `days`, the ones where any _STALE_SOURCES row was written after the day's
    features were computed (get()'s "has any feature row" test misses these)."""
    if not days:
        return set()
    computed = {r["day"]: r["computed"] for r in con.execute(
        f"SELECT day, MIN(refreshed_at) AS computed FROM features"
        f" WHERE day IN ({_marks(days)}) GROUP BY day", days)}
    if not computed:
        return set()
    present = {r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    lo, hi = min(computed), max(computed)
    stale: set[str] = set()
    # One statement per source with `day BETWEEN` in each arm: a single UNION against a
    # features CTE scanned every day index (78-166ms vs a few ms).
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
    """`{day: [feature, ...]}` a rebuild must not delete (_RETENTION_GATES); only days
    that have such rows appear."""
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
    """Recompute every feature for `days`. A failing extractor drops only its features to
    gaps. Carried-forward rows are re-stamped `now` so the day doesn't read stale forever."""
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
    """Poller entrypoint: recompute today + yesterday."""
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
    """Slice from cache, computing never-computed or stale days first. `metric` returns
    one feature's {day, value} series; else the full {day, feature, value} matrix."""
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
    """`{feature: {day: value}}` over the trailing `span_days` ending at `end_day`."""
    start = (date.fromisoformat(end_day) - timedelta(days=span_days - 1)).isoformat()
    payload = get(ranges.parse_range(f"{start}..{end_day}"))
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for it in payload["data"]:
        out[it["feature"]][it["day"]] = it["value"]
    return dict(out)


# ---------- cross-source correlations ----------
#
# Pearson r over days where both are present; p from I_x(df/2, 1/2), x = df/(df+t^2),
# in stdlib (continued fraction + lgamma), no scipy.


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
    """Significant feature pairs over the trailing `window_days`, by |r| descending:
    {a, b, r, n, p}. `focus` (name or set) keeps pairs touching it; `cross_domain` drops
    same-source pairs. `cross_basis: True` (key absent otherwise) marks a pair whose
    features use different day notions: a caveat on r, not a reason to drop it."""
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
