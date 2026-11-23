"""Token usage — what every model call cost, and who spent the limit.

Two questions this answers that nothing else here could:

  * **does the tiering save anything?** Fable plans, Opus decides, Haiku
    grinds — or so the open_tab descriptions say. The only way to know is to
    price what actually ran, then reprice the same tokens as if one model had
    done it all.
  * **which tabs used up the 5h / weekly window?** The usage chips say how
    much is gone; this says where it went.

**The source is the session transcripts**, `~/.claude/projects/*/*.jsonl` (and
each session's `subagents/*.jsonl`): every Claude Code session on this
machine logs there, Kestrel's tabs (`entrypoint: sdk-py`) and the terminal's
alike. Two traps, both from the memory notes:

  * each assistant message is logged once per streamed chunk with the same
    cumulative `usage`, so a naive sum is ~2x. Dedupe by `message.id` within
    a file, taking the max of each field.
  * there is no API cost in a transcript, and on a subscription there is no
    bill. "Cost" here is **API-equivalent**: list price for the same tokens
    (PRICES below). It's a yardstick for comparing, not money spent.

Nothing is stored: a full scan of ~3.6GB reads in about five seconds, and a
per-file cache keyed on (size, mtime) makes every scan after the first only
re-read the files that grew. So the numbers are derived by construction —
there is no table to drift.

Tab and mode: a transcript knows its cwd and entrypoint, so dev tabs (cwd in
a project or one of its worktrees), terminal sessions and background one-shots
sort themselves. A vault session could be a general or a study tab, which only
the console knew: `console_sessions` (console_work.remember_session) records
it as tabs run; vault sessions older than the table read "general/study".

Other providers: Gemini and Codex errands come from the delegates log
(`delegate_calls`, app/delegates/store.py), one row per run with the tokens
their CLI reported, charged to the Claude session that sent them. They have no
rows in PRICES, so they count in tokens and show unpriced, and the tiering
comparison reprices Claude's calls only.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException

from . import claude_quota, config, db

router = APIRouter(prefix="/api/token-usage")

CLAUDE_PROJECTS = Path(os.path.expanduser("~/.claude/projects"))

# Provider ids are the delegates registry's (app/delegates/registry.py).
CLAUDE = "claude"
PROVIDER_LABEL = {"claude": "Claude", "gemini": "Gemini", "codex": "ChatGPT (Codex)"}

# $ per million tokens: (input, output, cache read). Cache writes are priced
# off input (WRITE_5M / WRITE_1H). List prices, matched by longest prefix of
# the model id so dated ids ("claude-haiku-4-5-20251001") resolve. From the
# claude-api skill's price table; a model not here is counted in
# tokens and shown unpriced rather than guessed at.
PRICES: dict[str, tuple[float, float, float]] = {
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
WRITE_5M, WRITE_1H = 1.25, 2.0
# Haiku 5.5 is tiered: a prompt over 100K tokens bills at 5x.
LONG_PROMPT = {"claude-haiku-5-5": (100_000, 5.0)}

# What "if one model did it all" reprices against.
COUNTERFACTUALS = ("claude-opus-5-5", "claude-fable-5-2", "claude-sonnet-5-5")


def price_for(model: str) -> tuple[float, float, float] | None:
    best = max((k for k in PRICES if model.startswith(k)), key=len, default=None)
    return PRICES[best] if best else None


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


@dataclass(slots=True)
class FileInfo:
    session: str
    cwd: str
    entrypoint: str
    subagent: bool
    first_prompt: str


def cost(c: Call, model: str | None = None) -> float | None:
    """API-equivalent $ for a call, at its own model's prices or `model`'s."""
    m = model or c.model
    p = price_for(m)
    if p is None:
        return None
    pin, pout, pcr = p
    prompt = c.inp + c.cache_read + c.cache_5m + c.cache_1h
    tier = next((mult for k, (limit, mult) in LONG_PROMPT.items()
                 if m.startswith(k) and prompt > limit), 1.0)
    pin, pout, pcr = pin * tier, pout * tier, pcr * tier
    return (c.inp * pin + c.out * pout + c.cache_read * pcr
            + c.cache_5m * pin * WRITE_5M + c.cache_1h * pin * WRITE_1H) / 1e6


def uncached_cost(c: Call) -> float | None:
    """The same call with no prompt caching: every prompt token at input price."""
    p = price_for(c.model)
    if p is None:
        return None
    return ((c.inp + c.cache_read + c.cache_5m + c.cache_1h) * p[0] + c.out * p[1]) / 1e6


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
                # Older records carry only the total; the CLI's default TTL
                # then was 5 minutes.
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
    """Gemini and Codex runs from the delegates log (`delegate_runs`), one
    Call each. Their session is the Claude session that sent them, so a tab's
    row counts its errands too. Claude's own fan-out variants are left out:
    they are `claude` sessions with transcripts of their own, already scanned.
    Never raises; telemetry."""
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
        try:
            ts = datetime.fromisoformat(r["started_at"]).timestamp()
        except (TypeError, ValueError):
            continue
        out.append(Call(ts=ts, model=r["model"] or r["provider"], provider=r["provider"],
                        session=r["session_id"] or f"{r['provider']}-errand",
                        inp=int(r["tokens_in"] or 0), out=int(r["tokens_out"] or 0),
                        cache_read=0, cache_5m=0, cache_1h=0))
    return out


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
GROUPS = ("model", "day", "tab", "mode", "project", "provider")


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


def _add(acc: dict, c: Call) -> None:
    acc["calls"] += 1
    acc["input"] += c.inp
    acc["output"] += c.out
    acc["cache_read"] += c.cache_read
    acc["cache_write"] += c.cache_5m + c.cache_1h
    acc["sessions"].add(c.session)
    v, u = cost(c), uncached_cost(c)
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
    known = _known_sessions()
    who: dict[str, dict] = {}

    def whom(s: str) -> dict:
        if s not in who:
            who[s] = classify(infos.get(s), known.get(s))
        return who[s]

    total = _blank()
    groups: dict[str, dict] = {}
    labels: dict[str, dict] = {}
    providers: dict[str, dict] = {}
    # The tiering question is about Claude's tiers, so only Claude's calls
    # are repriced, and compared against Claude's own cost.
    counter = {m: 0.0 for m in COUNTERFACTUALS}
    claude = 0.0
    for c in calls:
        _add(total, c)
        _add(providers.setdefault(c.provider, _blank()), c)
        if c.provider == CLAUDE:
            claude += cost(c) or 0.0
            for m in COUNTERFACTUALS:
                counter[m] += cost(c, m) or 0.0
        w = whom(c.session)
        if group == "model":
            key = f"{c.provider}:{c.model}"
            labels.setdefault(key, {"label": family(c.model), "provider": c.provider})
        elif group == "provider":
            key = c.provider
            labels.setdefault(key, {"label": PROVIDER_LABEL.get(key, key)})
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
        _add(acc, c)
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
        "claude_cost": round(claude, 4),
        "if_all_on": {family(m): round(v, 4) for m, v in counter.items()},
        "rows": rows[:limit], "more": more,
        "note": ("API-equivalent list prices for the same tokens — on a "
                 "subscription nothing is billed per token. Repricing ignores "
                 "tokenizer differences between models. Gemini and Codex runs "
                 "are counted in tokens, unpriced."),
    }


@router.get("/summary")
def get_summary(range: str = "week", group: str = "tab", limit: int = 40) -> dict:
    return summary(range, group, max(1, min(int(limit), 200)))
