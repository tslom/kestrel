"""run_script vs direct calls, on the same questions, in real console sessions.

    cd backend && VAULT_PATH=... .venv/bin/python -m evals.code_mode.run [--only 1] [--arm script]

Each question runs twice through console.build_options (general profile, the
default model, the real prompt, Brain hook and vault server): once as
shipped, once with run_script taken out of the server and its prompt line
cut. Reads are auto-approved as in the console; anything that would need a
card is denied, so nothing is written. Prints tokens, turns, calls and time
from each session's ResultMessage.

This spends the shared subscription window (Sonnet, ~50-300k tokens a
session, mostly cache reads), so it is run by hand, not by pytest.
"""
import argparse
import asyncio
import json
import time

from claude_agent_sdk import (AssistantMessage, ClaudeSDKClient, PermissionResultDeny,
                              ResultMessage, ToolUseBlock)

from app import console, console_tools

QUESTIONS = [
    "Which of my charges this month haven't been reviewed yet? Group them by "
    "merchant with a total each, and for each merchant tell me whether there's "
    "a receipt or order email for it.",
    "For each course I'm taking this term, what's coming up in the next week, "
    "and how much have I studied each one in the last two weeks?",
    "Who has texted me in the last week that I haven't replied to? For each of "
    "them, is there anything on my calendar with them this month?",
    # Big intermediate results: each body is thousands of characters and
    # the answer needs one number out of each.
    "Go through my last 8 receipt or order emails and tell me the total "
    "amount on each one, read from the email itself, and the sum.",
]


async def deny(name, inp, ctx):
    return PermissionResultDeny(message="measurement run: read-only", interrupt=False)


async def no_ask(input_data, tool_use_id, context):
    return {}


def _cut_prompt_line(append: str) -> str:
    start = append.find("- `run_script`")
    if start < 0:
        return append
    end = append.find("\n", start)
    return append[:start] + append[end + 1:]


async def one(question: str, arm: str) -> dict:
    opts = console.build_options(None, None, "auto", deny, no_ask)
    if arm == "direct":
        tools = [t for t in console_tools.TOOLS if t.name != "run_script"]
        opts.mcp_servers = {console_tools.SERVER_NAME: console_tools.build_server(tools)}
        opts.allowed_tools = [t for t in opts.allowed_tools if not t.endswith("__run_script")]
        opts.system_prompt = {**opts.system_prompt,
                              "append": _cut_prompt_line(opts.system_prompt["append"])}
    opts.include_partial_messages = False
    calls: list[str] = []
    inputs: list[str] = []      # each call's input, cut: what the probing was for
    start = time.monotonic()
    result = None
    async with ClaudeSDKClient(options=opts) as client:
        await client.query(question)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, ToolUseBlock):
                        calls.append(b.name.removeprefix("mcp__vault__"))
                        inputs.append(json.dumps(b.input)[:300])
            elif isinstance(msg, ResultMessage):
                result = msg
    u = (result.usage or {}) if result else {}
    return {
        "arm": arm, "turns": result.num_turns if result else None,
        "input": u.get("input_tokens", 0),
        "cache_write": u.get("cache_creation_input_tokens", 0),
        "cache_read": u.get("cache_read_input_tokens", 0),
        "output": u.get("output_tokens", 0),
        "calls": calls, "inputs": inputs, "seconds": round(time.monotonic() - start, 1),
        "answer": (result.result or "")[:600] if result else "",
    }


def explored(calls: list[str]) -> int:
    """Calls made before the first run_script: the schema-finding the
    script arm is supposed to do without (11-17: 2-3 db_query probes).
    ToolSearch is loading the tools, not exploring, so it doesn't count; a
    run that never scripts counts every call."""
    before = calls[:calls.index("run_script")] if "run_script" in calls else calls
    return sum(c != "ToolSearch" for c in before)


def table(rows: list[dict]) -> str:
    """Median per question and arm: total input tokens, output, turns, calls
    before the script, seconds. Medians, since one run that wanders off
    doubles a mean at n=3."""
    from statistics import median
    lines = ["| q | arm | n | total in | out | turns | pre-script calls | scripted | s |",
             "|---|---|---|---|---|---|---|---|---|"]
    keys = sorted({(r["q"], r["arm"]) for r in rows})
    for q, arm in keys:
        rs = [r for r in rows if r["q"] == q and r["arm"] == arm]
        med = lambda k: median(r[k] for r in rs)    # one-line local
        pre = median(explored(r["calls"]) for r in rs) if arm == "script" else 0
        used = sum("run_script" in r["calls"] for r in rs)
        lines.append(f"| {q} | {arm} | {len(rs)} | {med('total_in'):,.0f} | {med('output'):,.0f}"
                     f" | {med('turns'):.0f} | {pre:.1f} | {used}/{len(rs)} | {med('seconds'):.0f} |")
    return "\n".join(lines)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", type=int, help="question number, from 1")
    ap.add_argument("--arm", choices=["script", "direct"])
    ap.add_argument("--repeat", type=int, default=1, help="runs per question and arm")
    ap.add_argument("--out", help="append each run as a JSON line here")
    ap.add_argument("--table", help="print the table for an --out file and stop")
    args = ap.parse_args()
    if args.table:
        with open(args.table) as f:
            print(table([json.loads(ln) for ln in f if ln.strip()]))
        return
    picks = [args.only] if args.only else range(1, len(QUESTIONS) + 1)
    arms = [args.arm] if args.arm else ["direct", "script"]
    rows = []
    for _ in range(args.repeat):
        for n in picks:
            # The two arms side by side, so the window state is the same for both.
            for r in await asyncio.gather(*(one(QUESTIONS[n - 1], a) for a in arms)):
                r = {"q": n, **r, "total_in": r["input"] + r["cache_write"] + r["cache_read"],
                     "explored": explored(r["calls"]) if r["arm"] == "script" else 0}
                rows.append(r)
                print(json.dumps(r), flush=True)
                if args.out:
                    with open(args.out, "a") as f:
                        f.write(json.dumps(r) + "\n")
    print(table(rows))


if __name__ == "__main__":
    asyncio.run(main())
