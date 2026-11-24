"""List prices ($ per million tokens) for each model, kept current from public lists.

token_usage.py prices every call off this. Lookup order: Claude Code's cached list, LiteLLM,
OpenRouter, then the hand table (Claude only). Lists are cached in app.db (derived,
droppable) and refetched daily; a failed fetch keeps the last good rows. A model no list
has is unpriced, never $0. Design: DESIGN.md → Model usage.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from dataclasses import dataclass

from . import claude_quota, db

log = logging.getLogger(__name__)

CLI, LITELLM, OPENROUTER, BUILTIN = "claude-cli", "litellm", "openrouter", "builtin"
LABELS = {CLI: "Claude Code's price list", LITELLM: "LiteLLM's price list",
          OPENROUTER: "OpenRouter's model list", BUILTIN: "the built-in table"}
LITELLM_URL = ("https://raw.githubusercontent.com/BerriAI/litellm/main/"
               "model_prices_and_context_window.json")
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
WEB = {LITELLM: LITELLM_URL, OPENROUTER: OPENROUTER_URL}
FETCH_TIMEOUT = 30
FRESH_S = {CLI: 3600, LITELLM: 86400, OPENROUTER: 86400}
RETRY_S = 3600          # after a failed fetch
CLAUDE_ORDER = (CLI, LITELLM, OPENROUTER, BUILTIN)
OTHER_ORDER = (LITELLM, OPENROUTER)
# LiteLLM providers whose rows are the vendors' own prices.
LITELLM_KEEP = {"openai", "anthropic", "gemini", "vertex_ai-language-models"}

# $/M tokens: (input, output, cache read); writes 1.25x (5m) / 2x (1h) input. Prefix-matched; last resort.
BUILTIN_PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-2": (8.0, 40.0, 0.20),
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.00),
    "claude-mythos-5-2": (8.0, 40.0, 0.20),
    "claude-mythos-5-1": (10.0, 50.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-opus-4-5": (5.0, 25.0, 0.50),
    "claude-opus-4": (15.0, 75.0, 1.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
BUILTIN_LONG = {"claude-haiku-5-5": (100_000, 5.0)}


@dataclass(frozen=True, slots=True)
class Rates:
    """$ per million tokens of each kind."""
    input: float
    output: float
    cache_read: float
    cache_5m: float
    cache_1h: float


@dataclass(frozen=True, slots=True)
class Price:
    model: str                  # the id it was listed under
    source: str
    base: Rates
    long_above: int | None = None   # a prompt over this many tokens bills at `long`
    long: Rates | None = None

    def rates(self, prompt_tokens: int, tiered: bool = True) -> Rates:
        if tiered and self.long and self.long_above and prompt_tokens > self.long_above:
            return self.long
        return self.base


def _rates(inp: float, out: float, read=None, w5=None, w1=None) -> Rates:
    """A missing cache rate means no premium: it falls back to input (1h to the 5m write)."""
    w5 = inp if w5 is None else w5
    return Rates(inp, out, inp if read is None else read, w5, w5 if w1 is None else w1)


# --- parsing each list ---------------------------------------------------------------------

def _row(model: str, base: Rates, long_above=None, long: Rates | None = None) -> dict:
    lg = long or Rates(None, None, None, None, None)
    return {"model": model, "input": base.input, "output": base.output,
            "cache_read": base.cache_read, "cache_write_5m": base.cache_5m,
            "cache_write_1h": base.cache_1h, "long_above": long_above if long else None,
            "long_input": lg.input, "long_output": lg.output, "long_cache_read": lg.cache_read,
            "long_cache_write_5m": lg.cache_5m, "long_cache_write_1h": lg.cache_1h}


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f >= 0 else None


def parse_cli(costs: dict) -> list[dict]:
    """additionalModelCostsCache, already $/M. Fast-mode rates are left out: transcripts don't say."""
    def rates(p: dict) -> Rates | None:
        inp, out = _num(p.get("inputTokens")), _num(p.get("outputTokens"))
        if inp is None or out is None or not (inp or out):
            return None
        return _rates(inp, out, _num(p.get("promptCacheReadTokens")),
                      _num(p.get("promptCacheWriteTokens")), _num(p.get("promptCacheWrite1hTokens")))
    rows = []
    for model, p in (costs or {}).items():
        base = rates(p) if isinstance(p, dict) else None
        if base is None:
            continue
        lp = p.get("longPrompt") if isinstance(p.get("longPrompt"), dict) else None
        long = rates(lp) if lp else None
        rows.append(_row(model, base, int(lp.get("abovePromptTokens") or 0) or None if lp else None, long))
    return rows


_ABOVE = re.compile(r"^input_cost_per_token_above_(\d+)k_tokens$")


def parse_litellm(data: dict) -> list[dict]:
    """LiteLLM's JSON ($ per token): vendors' own chat rows only (LITELLM_KEEP), bare ids
    (`gemini/x` stands in for a missing `x`). Batch, flex and priority rates are left out."""
    rows: dict[str, dict] = {}
    for key in sorted(data, key=lambda k: "/" in k):
        v = data[key]
        if not isinstance(v, dict) or v.get("litellm_provider") not in LITELLM_KEEP:
            continue
        if v.get("mode") not in ("chat", "responses", None):
            continue
        model = key.removeprefix("gemini/")
        if "/" in model or model in rows:
            continue

        def per_m(field: str, src=v) -> float | None:
            n = _num(src.get(field))
            return None if n is None else n * 1e6
        inp, out = per_m("input_cost_per_token"), per_m("output_cost_per_token")
        if inp is None or out is None or not (inp or out):
            continue
        base = _rates(inp, out, per_m("cache_read_input_token_cost"),
                      per_m("cache_creation_input_token_cost"),
                      per_m("cache_creation_input_token_cost_above_1hr"))
        tiers = sorted(int(m.group(1)) for f in v if (m := _ABOVE.match(f)))
        long = None
        if tiers:
            k = tiers[0]
            linp, lout = per_m(f"input_cost_per_token_above_{k}k_tokens"), \
                per_m(f"output_cost_per_token_above_{k}k_tokens")
            if linp is not None and lout is not None:
                long = _rates(linp, lout, per_m(f"cache_read_input_token_cost_above_{k}k_tokens"),
                              per_m(f"cache_creation_input_token_cost_above_{k}k_tokens"),
                              per_m(f"cache_creation_input_token_cost_above_1hr_above_{k}k_tokens"))
        rows[model] = _row(model, base, tiers[0] * 1000 if long else None, long)
    return list(rows.values())


def parse_openrouter(data: dict) -> list[dict]:
    """OpenRouter's /models ($ per token as strings): variants (":batch", ":free") dropped,
    vendor prefix stripped, "claude-opus-5.5" renamed "claude-opus-5-5"."""
    rows: dict[str, dict] = {}
    for m in (data or {}).get("data") or []:
        mid = str((m or {}).get("id") or "")
        if ":" in mid or "/" not in mid:
            continue
        vendor, model = mid.split("/", 1)
        if vendor == "anthropic":
            model = re.sub(r"(\d)\.(\d)", r"\1-\2", model)
        if model in rows:
            continue
        p = m.get("pricing") or {}

        def per_m(field: str, src=p) -> float | None:
            n = _num(src.get(field))
            return None if n is None else n * 1e6
        inp, out = per_m("prompt"), per_m("completion")
        if inp is None or out is None or not (inp or out):
            continue
        base = _rates(inp, out, per_m("input_cache_read"), per_m("input_cache_write"))
        over = next((o for o in p.get("overrides") or []
                     if isinstance(o, dict) and o.get("min_prompt_tokens")), None)
        long = None
        if over and per_m("prompt", over) is not None and per_m("completion", over) is not None:
            long = _rates(per_m("prompt", over), per_m("completion", over),
                          per_m("input_cache_read", over), per_m("input_cache_write", over))
        rows[model] = _row(model, base, int(over["min_prompt_tokens"]) if long else None, long)
    return list(rows.values())


PARSERS = {CLI: parse_cli, LITELLM: parse_litellm, OPENROUTER: parse_openrouter}


# --- fetching and the cache ----------------------------------------------------------------

def _get(url: str) -> dict:
    """An anonymous GET: urllib's own User-Agent only, nothing about the user."""
    with urllib.request.urlopen(urllib.request.Request(url), timeout=FETCH_TIMEOUT) as r:
        return json.loads(r.read())


def _read_cli() -> dict:
    raw = json.loads(claude_quota.CLAUDE_CONFIG.read_text())
    costs = raw.get("additionalModelCostsCache") if isinstance(raw, dict) else None
    if not isinstance(costs, dict) or not costs:
        raise ValueError("Claude Code's config has no price list (additionalModelCostsCache)")
    return costs


def fetches() -> dict[str, dict]:
    with db.session() as con:
        return {r["source"]: dict(r) for r in con.execute("SELECT * FROM model_price_fetches")}


def save(source: str, rows: list[dict] | None, note: str | None = None) -> None:
    """A fetch's outcome. rows=None (failed) keeps the last good rows."""
    now = time.time()
    with db.session() as con, con:
        if rows is not None:
            con.execute("DELETE FROM model_prices WHERE source = ?", (source,))
            cols = list(_row("x", _rates(0, 0)))
            con.executemany(
                f"INSERT INTO model_prices (source, {', '.join(cols)}) VALUES"
                f" (?, {', '.join('?' * len(cols))})",
                [(source, *(r[c] for c in cols)) for r in rows])
        con.execute(
            "INSERT INTO model_price_fetches (source, at, ok, note, good_at, models)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT (source) DO UPDATE SET at = excluded.at,"
            " ok = excluded.ok, note = excluded.note,"
            " good_at = COALESCE(excluded.good_at, model_price_fetches.good_at),"
            " models = COALESCE(excluded.models, model_price_fetches.models)",
            (source, now, int(rows is not None), note,
             now if rows is not None else None, len(rows) if rows is not None else None))
    _book.clear()


def refresh(source: str) -> bool:
    """Fetch one list now and store it, or record why not. True on success."""
    try:
        data = _read_cli() if source == CLI else _get(WEB[source])
        rows = PARSERS[source](data)
        if not rows:
            raise ValueError("the list had no models in it")
    except Exception as e:  # any failure keeps the last good rows; the note says why
        save(source, None, f"{type(e).__name__}: {e}"[:300])
        log.warning("model prices: %s fetch failed: %r", source, e)
        return False
    save(source, rows)
    return True


def stale(source: str, seen: dict | None = None, now: float | None = None) -> bool:
    f = (seen if seen is not None else fetches()).get(source)
    if f is None:
        return True
    return (now or time.time()) - f["at"] > (FRESH_S[source] if f["ok"] else RETRY_S)


_refreshing = threading.Lock()


def ensure_fresh() -> None:
    """Per page load: the local CLI list in line; a web list in line only if never fetched, else in the background."""
    seen = fetches()
    if stale(CLI, seen):
        refresh(CLI)
    due = [s for s in WEB if stale(s, seen)]
    if not due:
        return
    first = [s for s in due if not (seen.get(s) or {}).get("good_at")]
    if first and _refreshing.acquire(blocking=False):
        try:
            for s in first:
                refresh(s)
        finally:
            _refreshing.release()
        due = [s for s in due if s not in first]
    if due and _refreshing.acquire(blocking=False):
        def run():
            try:
                for s in due:
                    refresh(s)
            finally:
                _refreshing.release()
        threading.Thread(target=run, name="model-prices", daemon=True).start()


# --- lookup --------------------------------------------------------------------------------

# What may follow a listed id and still be that model: date, variant word, "@version"; never a digit.
_TAIL = re.compile(r"^(?:-\d{8}|-\d{4}-\d{2}-\d{2}|-[a-z]|@)")


def normalize(model: str) -> str:
    """'claude-opus-5-5[1m]' → 'claude-opus-5-5', 'google/gemini-x' → 'gemini-x'."""
    m = (model or "").strip().lower()
    m = re.sub(r"\[[^\]]*\]$", "", m)
    return m.rsplit("/", 1)[-1]


def match(table: dict, model: str, loose: bool = False):
    """The table's entry for a model: its exact id, else the longest listed
    id it starts with (followed by what _TAIL allows, or anything when loose)."""
    if model in table:
        return table[model]
    best = max((k for k in table if model.startswith(k)
                and (loose or _TAIL.match(model[len(k):]))), key=len, default=None)
    return table[best] if best else None


def _from_row(r) -> Price:
    base = Rates(r["input"], r["output"], r["cache_read"], r["cache_write_5m"], r["cache_write_1h"])
    long = (Rates(r["long_input"], r["long_output"], r["long_cache_read"],
                  r["long_cache_write_5m"], r["long_cache_write_1h"])
            if r["long_above"] and r["long_input"] is not None else None)
    return Price(r["model"], r["source"], base, r["long_above"] if long else None, long)


def _builtin() -> dict[str, Price]:
    out = {}
    for k, (inp, outp, read) in BUILTIN_PRICES.items():
        base = Rates(inp, outp, read, inp * 1.25, inp * 2)
        long = None
        if k in BUILTIN_LONG:
            mult = BUILTIN_LONG[k][1]
            long = Rates(*(x * mult for x in (inp, outp, read, inp * 1.25, inp * 2)))
        out[k] = Price(k, BUILTIN, base, BUILTIN_LONG.get(k, (None,))[0], long)
    return out


class Book:
    """Every cached list, reloaded after each save, lookups memoized."""

    def __init__(self, tables: dict[str, dict[str, Price]], seen: dict[str, dict]):
        self.tables = tables
        self.fetches = seen
        self._memo: dict[str, Price | None] = {}

    def lookup(self, model: str | None) -> Price | None:
        if not model:
            return None
        if model in self._memo:
            return self._memo[model]
        m = normalize(model)
        hit = None
        for src in CLAUDE_ORDER if m.startswith("claude-") else OTHER_ORDER:
            hit = match(self.tables.get(src) or {}, m, loose=src == BUILTIN)
            if hit:
                break
        self._memo[model] = hit
        return hit

    def sources(self) -> list[dict]:
        out = []
        for s in (CLI, LITELLM, OPENROUTER, BUILTIN):
            f = self.fetches.get(s) or {}
            out.append({"id": s, "label": LABELS[s], "models": len(self.tables.get(s) or {}),
                        "fetched_at": f.get("good_at"), "tried_at": f.get("at"),
                        "ok": bool(f.get("ok", 1)) if s != BUILTIN else True,
                        "note": f.get("note"),
                        "url": WEB.get(s) or ("~/.claude.json" if s == CLI else None)})
        return out


_book: dict[str, Book] = {}


def book() -> Book:
    """The lists as cached now (no fetching; ensure_fresh does that)."""
    hit = _book.get("now")
    if hit is not None:
        return hit
    tables: dict[str, dict[str, Price]] = {BUILTIN: _builtin()}
    with db.session() as con:
        for r in con.execute("SELECT * FROM model_prices"):
            tables.setdefault(r["source"], {})[r["model"]] = _from_row(r)
    _book["now"] = b = Book(tables, fetches())
    return b
