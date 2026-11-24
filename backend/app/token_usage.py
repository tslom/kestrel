"""Token usage: tokens and API-equivalent cost per tab, model, mode, project, day.

Source: the session transcripts (`~/.claude/projects/*/*.jsonl`, plus
`subagents/`) and the delegates log for outside providers. Nothing is stored.
Traps: usage is logged once per streamed chunk, so dedupe by `message.id`
(max of each field); cost is list price, never a bill.
Design: DESIGN.md → Model usage.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException

from . import claude_quota, config, db, model_prices

router = APIRouter(prefix="/api/token-usage")

CLAUDE_PROJECTS = Path(os.path.expanduser("~/.claude/projects"))

# Provider ids and names are the delegates registry's (app/delegates/registry.py).
CLAUDE = "claude"


def _provider_labels() -> dict[str, str]:
    from .delegates import registry
    return {p.id: p.label for p in registry.PROVIDERS.values()}


PROVIDER_LABEL = _provider_labels()

# "If one model did it all" targets: newest of these families in the live list, else COUNTERFACTUALS.
COUNTER_FAMILIES = ("opus", "fable", "sonnet")
COUNTERFACTUALS = ("claude-opus-5-5", "claude-fable-5-2", "claude-sonnet-5-5")


def counterfactuals(providers: set[str]) -> dict[str, str]:
    """label → model id to reprice on; an unreadable list falls back to COUNTERFACTUALS."""
    out: dict[str, str] = {}
    try:
        from . import model_catalog
        for fam in COUNTER_FAMILIES:
            row = model_catalog.newest(fam)
            if row:
                out[family(row["id"])] = row["id"]
        for p in sorted(providers - {CLAUDE}):
            rows = sorted(model_catalog.catalog(p), key=lambda r: r["rank"])
            if rows:
                out[rows[0].get("label") or rows[0]["id"]] = rows[0]["id"]
    except Exception as e:  # the comparison falls back to the fixed ids
        print(f"[token-usage] model list unreadable: {e!r}")
    if not any(m.startswith("claude-") for m in out.values()):
        out = {**{family(m): m for m in COUNTERFACTUALS}, **out}
    return out


def price_for(model: str, book: model_prices.Book | None = None) -> model_prices.Price | None:
    return (book or model_prices.book()).lookup(model)


def family(model: str) -> str:
    """'claude-opus-5-5' → 'opus 5.5', for labels."""
    m = model.removeprefix("claude-")
    parts = m.split("-")
    if len(parts) >= 2 and parts[0] in {"opus", "sonnet", "haiku", "fable", "mythos"}:
        ver = [p for p in parts[1:] if p.isdigit() and len(p) <= 2]
        return f"{parts[0]} {'.'.join(ver)}".strip()
    return m


@dataclass(frozen=True, slots=True)
class Call:
    """One model response: its tokens, and enough to say whose it was."""
    ts: float              # epoch seconds
    model: str
    provider: str
    session: str
    inp: int
    out: int
    cache_read: int
    cache_5m: int
    cache_1h: int
    account: str = ""      # an outside provider's account (delegate_accounts.id)
    summed: bool = False   # a delegate run: many model calls' tokens added up


@dataclass(slots=True)
class FileInfo:
    session: str
    cwd: str
    entrypoint: str
    subagent: bool
    first_prompt: str


def _prompt(c: Call) -> int:
    return c.inp + c.cache_read + c.cache_5m + c.cache_1h


def cost(c: Call, model: str | None = None,
         book: model_prices.Book | None = None) -> float | None:
    """API-equivalent $ for a call, at its own model's prices or `model`'s.
    A prompt past the model's long-context threshold bills at its long tier,
    except in a delegate run, whose tokens are summed over many calls."""
    p = price_for(model or c.model, book)
    if p is None:
        return None
    r = p.rates(_prompt(c), tiered=not c.summed)
    return (c.inp * r.input + c.out * r.output + c.cache_read * r.cache_read
            + c.cache_5m * r.cache_5m + c.cache_1h * r.cache_1h) / 1e6


def uncached_cost(c: Call, book: model_prices.Book | None = None) -> float | None:
    """The same call with no prompt caching: every prompt token at input price."""
    p = price_for(c.model, book)
    if p is None:
        return None
    r = p.rates(_prompt(c), tiered=not c.summed)
    return (_prompt(c) * r.input + c.out * r.output) / 1e6


# --- the Claude transcript scanner ---------------------------------------

def _ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _prompt_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                return str(b.get("text") or "")
    return ""


def parse_claude(path: Path) -> tuple[FileInfo, list[Call]]:
    """One transcript file → who it was and its deduped calls."""
    sub = path.parent.name == "subagents"
    session = path.parent.parent.name if sub else path.stem
    info = FileInfo(session=session, cwd="", entrypoint="", subagent=sub, first_prompt="")
    by_id: dict[str, dict] = {}
    with open(path, "rb") as f:
        for line in f:
            has_usage = b'"usage"' in line
            if info.cwd and info.first_prompt and not has_usage:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            if not info.cwd and d.get("cwd"):
                info.cwd = str(d["cwd"])
            if not info.entrypoint and d.get("entrypoint"):
                info.entrypoint = str(d["entrypoint"])
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            if (not info.first_prompt and d.get("type") == "user"
                    and not d.get("isMeta") and msg.get("role") == "user"):
                text = _prompt_text(msg).strip()
                if text and not text.startswith("<"):
                    info.first_prompt = " ".join(text.split())[:140]
            usage = msg.get("usage")
            model = msg.get("model")
            if not has_usage or not isinstance(usage, dict) or not model or model == "<synthetic>":
                continue
            ts = _ts(d.get("timestamp"))
            if ts is None:
                continue
            mid = str(msg.get("id") or d.get("requestId") or d.get("uuid"))
            cc = usage.get("cache_creation") or {}
            row = {
                "ts": ts, "model": str(model),
                "inp": int(usage.get("input_tokens") or 0),
                "out": int(usage.get("output_tokens") or 0),
                "cache_read": int(usage.get("cache_read_input_tokens") or 0),
                "cache_5m": int(cc.get("ephemeral_5m_input_tokens") or 0),
                "cache_1h": int(cc.get("ephemeral_1h_input_tokens") or 0),
            }
            if not cc and usage.get("cache_creation_input_tokens"):
                # Older records carry only the total, at the then-default 5m TTL.
                row["cache_5m"] = int(usage["cache_creation_input_tokens"])
            prev = by_id.get(mid)
            if prev is None:
                by_id[mid] = row
            else:
                for k in ("inp", "out", "cache_read", "cache_5m", "cache_1h"):
                    prev[k] = max(prev[k], row[k])
                prev["ts"] = min(prev["ts"], ts)
    calls = [Call(ts=r["ts"], model=r["model"], provider=CLAUDE, session=session,
                  inp=r["inp"], out=r["out"], cache_read=r["cache_read"],
                  cache_5m=r["cache_5m"], cache_1h=r["cache_1h"]) for r in by_id.values()]
    return info, calls


def claude_files(root: Path | None = None) -> list[Path]:
    root = root or CLAUDE_PROJECTS
    if not root.is_dir():
        return []
    return [*root.glob("*/*.jsonl"), *root.glob("*/*/subagents/*.jsonl")]


# Each source: (provider, list-files, parse-file).
SOURCES = [(CLAUDE, claude_files, parse_claude)]


def delegate_calls(start: datetime | None) -> list[Call]:
    """Outside providers' finished runs as Calls, charged to the Claude session that sent
    them; Claude fan-outs are skipped (already in transcripts). Never raises."""
    try:
        from .delegates import registry, store
        outside = tuple(p.id for p in registry.PROVIDERS.values() if p.outside)
        since = start.astimezone().replace(tzinfo=None).isoformat(timespec="seconds") if start else None
        rows = store.finished_runs(outside, since)
    except Exception as e:  # a broken delegates table costs these rows, not the page
        print(f"[token-usage] delegate runs unreadable: {e!r}")
        return []
    out = []
    for r in rows:
        # Refused before any model call (bad --model, signed out): spent nothing.
        if not (r["tokens_in"] or r["tokens_out"]):
            continue
        try:
            ts = datetime.fromisoformat(r["started_at"]).timestamp()
        except (TypeError, ValueError):
            continue
        # No model = the CLI's unnamed default. tokens_in includes the cached part.
        tin, cached = int(r["tokens_in"] or 0), int(r.get("tokens_cached") or 0)
        out.append(Call(ts=ts, model=r["model"] or DEFAULT_MODEL, provider=r["provider"],
                        session=r["session_id"] or f"{r['provider']}-errand",
                        inp=max(0, tin - cached), out=int(r["tokens_out"] or 0),
                        cache_read=min(cached, tin), cache_5m=0, cache_1h=0,
                        account=r.get("account") or "", summed=True))
    return out


DEFAULT_MODEL = "default model"


def _account_labels() -> dict[str, str]:
    """delegate_accounts id → name; a broken table costs the names, not the page."""
    try:
        from .delegates import store
        return {a["id"]: a["label"] for a in store.accounts()}
    except Exception:  # the page still groups, by id
        return {}


class _Cache:
    def __init__(self):
        self.lock = threading.Lock()
        self.files: dict[str, tuple[tuple[int, float], FileInfo, list[Call]]] = {}


_cache = _Cache()


def scan(sources=None) -> tuple[dict[str, FileInfo], list[Call]]:
    """Every call from every source, re-reading only files that changed.
    Returns (session → info, calls). A subagent file lends its calls to its
    parent session and its info only when the parent's own file is gone."""
    sources = sources if sources is not None else SOURCES
    with _cache.lock:
        seen: set[str] = set()
        for _provider, lister, parser in sources:
            for p in lister():
                key = str(p)
                try:
                    st = p.stat()
                except OSError:
                    continue
                seen.add(key)
                stamp = (st.st_size, st.st_mtime)
                hit = _cache.files.get(key)
                if hit and hit[0] == stamp:
                    continue
                try:
                    info, calls = parser(p)
                except OSError:
                    continue
                _cache.files[key] = (stamp, info, calls)
        for gone in set(_cache.files) - seen:
            del _cache.files[gone]
        infos: dict[str, FileInfo] = {}
        calls: list[Call] = []
        for _stamp, info, cs in _cache.files.values():
            calls.extend(cs)
            have = infos.get(info.session)
            if have is None or (have.subagent and not info.subagent):
                infos[info.session] = info
        return infos, calls


# --- who a session was ----------------------------------------------------

def _known_sessions() -> dict[str, dict]:
    try:
        with db.session() as con:
            return {r["session_id"]: dict(r)
                    for r in con.execute("SELECT * FROM console_sessions")}
    except Exception:
        return {}


def classify(info: FileInfo | None, known: dict | None) -> dict:
    """{mode, project, tab} for a session. mode is one of general, study,
    dev, terminal, background, night, other."""
    cwd = (info.cwd if info else "") or ""
    entry = (info.entrypoint if info else "") or ""
    vault = str(config.VAULT_PATH)
    project = worktree = ""
    if "/.claude/worktrees/" in cwd:
        base, _, rest = cwd.partition("/.claude/worktrees/")
        project, worktree = Path(base).name, rest.split("/")[0]
    elif cwd:
        project = Path(cwd).name
    in_vault = bool(cwd) and (cwd == vault or cwd.startswith(vault + "/"))
    if known and known.get("profile"):
        mode = known["profile"]
    elif in_vault:
        mode = "general/study" if entry.startswith("sdk") else "terminal"
    elif worktree.startswith("night"):
        mode = "night"
    elif entry == "cli":
        mode = "terminal"
    elif entry.startswith("sdk"):
        # Kestrel's own one-shots (agent.ask) run with the backend as cwd.
        mode = "background" if Path(cwd).name == "backend" else "dev"
    else:
        mode = "other"
    if in_vault:
        project = "vault"
    return {"mode": mode, "project": project or "?", "worktree": worktree}


# --- ranges and grouping --------------------------------------------------

RANGES = ("5h", "week", "today", "7d", "30d", "90d", "all")
GROUPS = ("model", "day", "tab", "mode", "project", "provider", "account")


def _window_start(key: str, span: timedelta, now: datetime) -> tuple[datetime, bool]:
    """The live quota window's start, from its resets_at; else a rolling
    span. (start, True) when it's the real window."""
    try:
        snap = claude_quota.usage_snapshot(fetch=False)
    except Exception:
        snap = {"windows": []}
    for w in snap.get("windows") or []:
        reset = w.get("resets_at")
        if w.get("key") == key and reset and reset > now.timestamp():
            return datetime.fromtimestamp(reset, timezone.utc) - span, True
    return now - span, False


def range_start(rng: str, now: datetime | None = None) -> tuple[datetime | None, str]:
    """(start, a sentence saying what the start is)."""
    now = now or datetime.now(timezone.utc)
    if rng == "5h":
        start, real = _window_start("five_hour", timedelta(hours=5), now)
        return start, "the current 5h window" if real else "the last 5 hours"
    if rng == "week":
        start, real = _window_start("seven_day", timedelta(days=7), now)
        return start, "the current weekly window" if real else "the last 7 days"
    if rng == "today":
        local = datetime.now().astimezone()
        day = db.day_key(local.replace(tzinfo=None))
        start = db.day_instant(day, "04:00").astimezone()
        return start, f"today ({day}, from 4am)"
    if rng in {"7d", "30d", "90d"}:
        return now - timedelta(days=int(rng[:-1])), f"the last {rng[:-1]} days"
    if rng == "all":
        return None, "everything on disk"
    raise HTTPException(400, f"range must be one of {', '.join(RANGES)}")


def _blank() -> dict:
    return {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
            "cost": 0.0, "uncached": 0.0, "unpriced": 0, "sessions": set()}


def _add(acc: dict, c: Call, v: float | None, u: float | None) -> None:
    acc["calls"] += 1
    acc["input"] += c.inp
    acc["output"] += c.out
    acc["cache_read"] += c.cache_read
    acc["cache_write"] += c.cache_5m + c.cache_1h
    acc["sessions"].add(c.session)
    if v is None or u is None:
        acc["unpriced"] += 1
    else:
        acc["cost"] += v
        acc["uncached"] += u


def _finish(acc: dict) -> dict:
    out = {k: v for k, v in acc.items() if k != "sessions"}
    out["sessions"] = len(acc["sessions"])
    out["cost"] = round(acc["cost"], 4)
    out["uncached"] = round(acc["uncached"], 4)
    out["cache_saved"] = round(acc["uncached"] - acc["cost"], 4)
    out["tokens"] = acc["input"] + acc["output"] + acc["cache_read"] + acc["cache_write"]
    return out


def summary(rng: str = "week", group: str = "tab", limit: int = 40,
            sources=None) -> dict:
    if group not in GROUPS:
        raise HTTPException(400, f"group must be one of {', '.join(GROUPS)}")
    start, what = range_start(rng)
    infos, calls = scan(sources)
    cut = start.timestamp() if start else float("-inf")
    calls = [c for c in calls if c.ts >= cut]
    if sources is None:
        calls += delegate_calls(start)
    try:
        model_prices.ensure_fresh()
    except Exception as e:  # stale prices beat no page
        print(f"[token-usage] price refresh failed: {e!r}")
    book = model_prices.book()
    known = _known_sessions()
    accts = _account_labels() if group == "account" else {}
    who: dict[str, dict] = {}

    def whom(s: str) -> dict:
        if s not in who:
            who[s] = classify(infos.get(s), known.get(s))
        return who[s]

    total = _blank()
    groups: dict[str, dict] = {}
    labels: dict[str, dict] = {}
    providers: dict[str, dict] = {}
    # Unpriced calls are left out of both the mix and the counterfactuals.
    targets = counterfactuals({c.provider for c in calls})
    targets = {k: m for k, m in targets.items() if book.lookup(m)}
    counter = dict.fromkeys(targets, 0.0)
    mix = 0.0
    used: dict[tuple[str, str], int] = {}
    for c in calls:
        v, u = cost(c, book=book), uncached_cost(c, book=book)
        _add(total, c, v, u)
        _add(providers.setdefault(c.provider, _blank()), c, v, u)
        used[(c.provider, c.model)] = used.get((c.provider, c.model), 0) + c.inp + c.out \
            + c.cache_read + c.cache_5m + c.cache_1h
        if v is not None:
            mix += v
            for k, m in targets.items():
                counter[k] += cost(c, m, book) or 0.0
        w = whom(c.session)
        if group == "model":
            key = f"{c.provider}:{c.model}"
            labels.setdefault(key, {"label": family(c.model), "provider": c.provider})
        elif group == "provider":
            key = c.provider
            labels.setdefault(key, {"label": PROVIDER_LABEL.get(key, key)})
        elif group == "account":
            # Transcripts don't say which login wrote them, so Claude is one row.
            key = f"{c.provider}:{c.account}" if c.account else c.provider
            if key not in labels:
                name = PROVIDER_LABEL.get(c.provider, c.provider)
                labels[key] = {"label": f"{name} · {accts.get(c.account, c.account)}" if c.account
                               else name + (" (all accounts)" if c.provider == CLAUDE
                                            else " (API key, or before accounts)"),
                               "provider": c.provider}
        elif group == "day":
            key = db.day_key(datetime.fromtimestamp(c.ts))
            labels.setdefault(key, {"label": key})
        elif group == "mode":
            key = w["mode"]
            labels.setdefault(key, {"label": key})
        elif group == "project":
            key = w["project"]
            labels.setdefault(key, {"label": key})
        else:
            key = c.session
            if key not in labels:
                info = infos.get(key)
                k = known.get(key) or {}
                labels[key] = {
                    "label": k.get("label") or (info.first_prompt if info else "") or key[:8],
                    "mode": w["mode"], "project": w["project"],
                    "worktree": w["worktree"], "session": key,
                }
        acc = groups.setdefault(key, _blank())
        _add(acc, c, v, u)
        if group == "model" and "price" not in labels[key]:
            labels[key]["price"] = _price_meta(book.lookup(c.model), book)
        if group == "tab":
            meta = labels[key]
            meta["models"] = meta.get("models") or set()
            meta["models"].add(family(c.model) if c.provider == CLAUDE
                               else f"{PROVIDER_LABEL.get(c.provider, c.provider)} {c.model}")
            meta["last"] = max(meta.get("last", 0.0), c.ts)
    rows = []
    for key, acc in groups.items():
        meta = dict(labels.get(key) or {"label": key})
        if "models" in meta:
            meta["models"] = sorted(meta["models"])
        if "last" in meta:
            meta["last"] = datetime.fromtimestamp(meta["last"], timezone.utc).isoformat()
        rows.append({"key": key, **meta, **_finish(acc)})
    if group == "day":
        rows.sort(key=lambda r: r["key"], reverse=True)
    else:
        rows.sort(key=lambda r: (r["cost"], r["tokens"]), reverse=True)
    more = max(0, len(rows) - limit)
    tot = _finish(total)
    return {
        "range": rng, "group": group, "what": what,
        "since": start.isoformat() if start else None,
        "total": tot,
        "providers": sorted(
            ({"key": p, "label": PROVIDER_LABEL.get(p, p), **_finish(acc)}
             for p, acc in providers.items()),
            key=lambda r: (r["key"] != CLAUDE, -r["tokens"])),
        "mix_cost": round(mix, 4),
        "if_all_on": {k: round(v, 4) for k, v in counter.items()},
        "rows": rows[:limit], "more": more,
        "prices": _prices_used(used, book),
        "price_sources": book.sources(),
        "note": ("API-equivalent list prices for the same tokens — on a "
                 "subscription nothing is billed per token. Repricing ignores "
                 "tokenizer differences between models. An errand's tokens are "
                 "summed over its whole run, so its long-context tier isn't "
                 "applied."),
    }


def _price_meta(p: model_prices.Price | None, book: model_prices.Book) -> dict | None:
    """Where a model's price came from, for the page: None is unpriced."""
    if p is None:
        return None
    f = book.fetches.get(p.source) or {}
    return {"source": p.source, "source_label": model_prices.LABELS[p.source],
            "listed_as": p.model, "fetched_at": f.get("good_at"),
            "input": p.base.input, "output": p.base.output, "cache_read": p.base.cache_read,
            "long_above": p.long_above}


# Why an outside provider's run can be missing what pricing needs.
GAPS = {
    "antigravity": "agy reports no cached tokens, so all its input is priced as fresh",
}


def _prices_used(used: dict[tuple[str, str], int], book: model_prices.Book) -> list[dict]:
    """Every model in the range with its price and source, the most tokens
    first; an unpriced one says why."""
    out = []
    for (prov, model), tokens in sorted(used.items(), key=lambda kv: -kv[1]):
        meta = _price_meta(book.lookup(model), book)
        why = None
        if meta is None:
            why = ("the CLI didn't say which model ran" if model == DEFAULT_MODEL
                   else "no price list has this model")
        out.append({"provider": prov, "model": model,
                    "label": family(model) if prov == CLAUDE else model,
                    "tokens": tokens, "price": meta, "unpriced_why": why,
                    "gap": GAPS.get(prov)})
    return out


@router.get("/summary")
def get_summary(range: str = "week", group: str = "tab", limit: int = 40) -> dict:
    return summary(range, group, max(1, min(int(limit), 200)))
