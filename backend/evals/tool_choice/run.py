"""Does the console pick the right tool from what its descriptions say?

    cd backend && .venv/bin/python -m evals.tool_choice.run [--repeat 2] [--only event]
    ... --descriptions evals/tool_choice/candidate.json

`cases.json` holds a message and every tool that's a right first move. Each pass
batches them and asks Haiku which tool it would call first, given the real system
prompt and tool catalog; nothing runs. A deliberate proxy (~100k Haiku tokens a
pass against the shared window; weaker is the right way round for guarding
descriptions). Routing only, not argument correctness.

`--descriptions` overlays {tool: text} without touching console_tools.py.
test_tool_eval.py holds the free checks.
"""
import argparse
import asyncio
import json
import re
from pathlib import Path

from app import agent, console_prompt, console_tools

HERE = Path(__file__).parent
BATCH = 14
MODEL = "haiku"
CONCURRENCY = 3

ASK = (
    "You are choosing tools for the assistant described above. For each "
    "numbered user message below, on its own and with no earlier conversation, "
    "name the single tool the assistant should call FIRST, using the tool "
    "descriptions. Answer with only a JSON object from each message's number "
    "to its tool name, e.g. {\"1\": \"provider\", \"2\": \"none\"}. Use "
    "\"none\" if no tool applies.\n\n")


def catalog(overlay: dict[str, str]) -> str:
    # use_computer is in the general tab's server, outside TOOLS.
    tools = [*console_tools.TOOLS, console_tools.use_computer]
    return "\n\n".join(f"{t.name}: {' '.join(overlay.get(t.name, t.description).split())}"
                       for t in tools)


def tool_chars(overlay: dict[str, str]) -> int:
    return sum(len(overlay.get(t.name, t.description)) for t in console_tools.TOOLS)


async def judge(batch: list[dict], system: str, gate: asyncio.Semaphore) -> list[str]:
    numbered = "\n".join(f"{i + 1}. {c['p']}" for i, c in enumerate(batch))
    # Keyed by message number, not positional: Haiku drops a line now and then and
    # every later answer would score against the next message. A missing key is a re-ask.
    for _ in range(3):
        async with gate:
            out = await agent.ask(ASK + numbered, model=MODEL, system=system,
                                  thinking={"type": "disabled"})
        # First object only: some answers trail a second one ("Extra data").
        found = re.search(r"\{.*?\}", out, re.S)
        try:
            keyed = json.loads(found.group(0)) if found else {}
        except json.JSONDecodeError:
            keyed = {}
        picks = [keyed.get(str(i + 1)) for i in range(len(batch))]
        if isinstance(keyed, dict) and all(isinstance(p, str) for p in picks):
            # `mcp__vault__food_weigh` is food_weigh, not a wrong pick.
            return [str(p).removeprefix(f"mcp__{console_tools.SERVER_NAME}__") for p in picks]
    got = sum(isinstance(p, str) for p in picks)
    raise ValueError(f"{got} answers for {len(batch)} messages, three times")


async def run(cases: list[dict], overlay: dict[str, str], repeat: int) -> dict:
    system = (console_prompt.render_append(profile="general")
              + "\n\n# Tools\n\n" + catalog(overlay))
    gate = asyncio.Semaphore(CONCURRENCY)
    wrong: dict[str, list[str]] = {}
    passes = []
    for _ in range(repeat):
        batches = [cases[i:i + BATCH] for i in range(0, len(cases), BATCH)]
        answers = await asyncio.gather(*(judge(b, system, gate) for b in batches))
        right = 0
        for batch, picks in zip(batches, answers):
            for case, pick in zip(batch, picks):
                if pick in case["ok"]:
                    right += 1
                else:
                    wrong.setdefault(case["p"], []).append(pick)
        passes.append(right)
    return {"passes": passes, "wrong": wrong}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--only", help="cases whose accepted tools contain this")
    ap.add_argument("--descriptions", type=Path)
    args = ap.parse_args()
    cases = json.loads((HERE / "cases.json").read_text())
    if args.only:
        cases = [c for c in cases if any(args.only in t for t in c["ok"])]
    overlay = json.loads(args.descriptions.read_text()) if args.descriptions else {}
    if (why := agent.over_budget()):
        raise SystemExit(f"not running: {why}")
    result = asyncio.run(run(cases, overlay, args.repeat))
    print(f"{result['passes']} of {len(cases)}, tools {tool_chars(overlay)} chars")
    by_case = {c["p"]: c["ok"] for c in cases}
    for p, picks in result["wrong"].items():
        print(f"  {len(picks)}x {sorted(set(picks))} want {by_case[p]} :: {p}")


if __name__ == "__main__":
    main()
