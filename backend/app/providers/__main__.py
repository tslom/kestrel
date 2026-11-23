"""CLI face of the providers, for skills and the console — same code as REST,
zero exploration tokens:

    python -m app.providers weather --range this-week --metric tmax
    python -m app.providers calendar --range next-week --query lawton
    python -m app.providers calendar --range this-week --format brief
    python -m app.providers --list
    python -m app.providers weather --refresh       # force a cache pull first

Prints one JSON object to stdout (`--format brief`: header + one line per
item — same data, far fewer tokens for console/skill use). OAuth management
is its own CLI: python -m app.providers.oauth --help.
"""

import argparse
import json
import sys

from . import REGISTRY, describe, get
# Registers db's task-write hook: a `--refresh` here can create tasks (finance,
# email_extract, messages), and tasks.md must follow them as it does in-app.
from .. import vault_views  # noqa: F401 — imported for its side effect (above)


def _brief(out: dict, mod) -> str:
    """Header + one line per item. Providers may define brief_line(item);
    fallback is single-line JSON."""
    line = getattr(mod, "brief_line",
                   lambda it: json.dumps(it, separators=(",", ":"),
                                         ensure_ascii=False))
    head = f"{out['source']} {out['range']} {out['start']}..{out['end']}"
    if out.get("query"):
        head += f" query={out['query']!r}"
    data = out.get("data", [])
    head += f" ({len(data)} items"
    if out.get("refreshed_at"):
        head += f", refreshed {out['refreshed_at']}"
    head += ")"
    return "\n".join([head] + [line(it) for it in data])


def _search_brief(hits: dict, mod) -> str:
    """Same one-line-per-item face as _brief, for a payload that has a query
    where a range would be — a search answers over the source, not over days."""
    line = getattr(mod, "brief_line",
                   lambda it: json.dumps(it, separators=(",", ":"),
                                         ensure_ascii=False))
    scope = f" in {hits['subreddit']}" if hits.get("subreddit") else ""
    scan = hits.get("scan")
    # reddit's fallback when the archive's search is down: only the newest
    # posts were read, so the window in the head would overstate it.
    span = (f" newest {scan['posts']} posts only, back to {scan['back_to']}"
            f" (search down)" if scan and not scan["complete"]
            else f" last {hits['days']}d")
    head = (f"{hits['source']} search={hits['search']!r}{scope}"
            f"{span} ({hits['count']} items)")
    return "\n".join([head] + [line(it) for it in hits.get("data", [])])


def _body_brief(msg: dict) -> str:
    """Headers, then the body as text — the readable face of --body."""
    head = [f"{msg['received'][:16]} {msg['from']}: {msg['subject']}"]
    notes = (["quoted reply chain trimmed"] if msg.get("quoted_trimmed") else
             []) + ([f"truncated at {msg['chars']} chars"]
                    if msg.get("truncated") else [])
    if notes:
        head.append(f"({'; '.join(notes)})")
    return "\n".join(head + ["", msg["body"]])


def _main() -> None:
    p = argparse.ArgumentParser(
        prog="python -m app.providers",
        description="Query a context provider (reads the app.db cache).")
    p.add_argument("source", nargs="?", choices=sorted(REGISTRY))
    p.add_argument("--range", default="today", dest="range_")
    p.add_argument("--metric", default=None)
    p.add_argument("--query", default=None,
                   help="case-insensitive substring match over the items'"
                        " string fields (summary, location, from, subject, …)")
    p.add_argument("--format", default="json", choices=["json", "brief"],
                   dest="format_",
                   help="brief = header + one line per item (token-cheap)")
    p.add_argument("--refresh", action="store_true",
                   help="pull from the source into the cache before reading")
    p.add_argument("--extract", type=int, metavar="N", default=None,
                   help="sources that hold files (attachments): extract text"
                        " from up to N not-yet-tried items before reading."
                        " Never implicit — it costs model tokens per image and"
                        " parses untrusted bytes per document")
    p.add_argument("--body", metavar="ID", default=None,
                   help="sources that hold messages (email): print one whole"
                        " message by id instead of a range. Fetched live and"
                        " never stored")
    p.add_argument("--chars", type=int, default=None,
                   help="with --body: cap the body at N characters"
                        " (default EMAIL_BODY_FULL_CHARS)")
    p.add_argument("--search", metavar="TEXT", default=None,
                   help="sources that can query the whole outside world"
                        " (reddit): live full-text search instead of a range"
                        " read. Uncached — nothing it returns is written")
    p.add_argument("--sub", metavar="NAME", default=None,
                   help="with --search: which subreddit to search. Required —"
                        " the archive refuses an unscoped query")
    p.add_argument("--days", type=int, default=365,
                   help="with --search: how far back to look (default 365)")
    p.add_argument("--limit", type=int, default=25,
                   help="with --search: max results (capped at 100)")
    p.add_argument("--list", action="store_true",
                   help="show sources, metrics and ranges, then exit")
    args = p.parse_args()

    if args.list or args.source is None:
        print(json.dumps(describe(), indent=2))
        return
    try:
        mod = REGISTRY[args.source]
        if args.refresh and hasattr(mod, "refresh"):
            mod.refresh()
        if args.extract:
            if not hasattr(mod, "extract_pending"):
                sys.exit(f"error: {args.source!r} has nothing to extract")
            mod.extract_pending(args.extract)
        if args.body:
            if not hasattr(mod, "body"):
                sys.exit(f"error: {args.source!r} has no message bodies")
            msg = mod.body(args.body, args.chars)
            print(_body_brief(msg) if args.format_ == "brief"
                  else json.dumps(msg, indent=2))
            return
        if args.search:
            if not hasattr(mod, "search"):
                sys.exit(f"error: {args.source!r} has no live search")
            hits = mod.search(args.search, args.sub, args.days, args.limit)
            print(_search_brief(hits, mod) if args.format_ == "brief"
                  else json.dumps(hits, indent=2))
            return
        out = get(args.source, args.range_, args.metric, args.query)
        if args.format_ == "brief":
            print(_brief(out, mod))
        else:
            print(json.dumps(out, indent=2))
    except ValueError as e:
        sys.exit(f"error: {e}")
    except RuntimeError as e:
        # A source refusing or failing — a live search the archive timed out
        # on, a 4xx from an upstream API. It is an answer about the world, not
        # a bug in the caller, so it reads as one line rather than a traceback.
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    from ..agent import interactive
    with interactive():   # someone typed this (or asked the console to)
        _main()
