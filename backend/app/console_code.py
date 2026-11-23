"""run_script: Kestrel's Code Mode, reads only (New Features #73, phase 1).

Claude Code's own Code Mode refuses the Agent SDK every Kestrel tab runs on,
so this is the Cloudflare version of the same idea: one tool that takes a
short script, runs it in a sandbox where the console's read tools are typed
async functions, and hands back only what the script returns. A chain of N
reads costs one round-trip and one compact result instead of N results
pasted through the context.

**The sandbox is two layers, either of which holds on its own.**

1. *Deno with every permission denied* (`--no-prompt`, no `--allow-*`): no
   fs, net, env, run, ffi or sys. Remote and npm imports are off and the
   module cache is cached-only, so an `import()` can't fetch either.
2. *A macOS Seatbelt profile* (`sandbox-exec`, the same mechanism Claude
   Code's own sandbox uses) that the kernel enforces whatever Deno does:
   deny by default; read only the system libraries, the Homebrew kegs Deno
   links against, and the run's own temp dir; no network, no writes, no exec
   of anything but the Deno binary. Checked with Deno's permissions fully
   open (`-A`) inside it: /etc/passwd, ~, app.db (even a stat), fetch,
   127.0.0.1:8788, spawning /bin/ls and writing /tmp all fail.

The process also starts with an empty environment, a V8 heap cap, a wall
clock, a cap on what it may print, and a cap on tool calls (overall and for
the ones that cost money or quota per call). Node was the other candidate,
and is already installed, but its permission model is documented as "not a
security boundary against malicious code" and leaves `process.env` and
`process.binding` reachable; Deno is deny-all by design. No sandbox-exec
(not macOS) or no Deno means the tool refuses: it fails closed.

**The bridge.** The script's only way out is stdout/stdin, newline-delimited
JSON. A call is `{"t":"call","id","name","args"}`; the host validates args
against the tool's own JSON Schema, runs the same handler the direct tool
call runs (so the tool's own rules, caching and context var all still
apply), and answers `{"id","value"}` or `{"id","error"}`. Results are what
the tool would have said, parsed when that's JSON (db_query's rows,
`provider` with brief=false), else the text.

**Writes are not in the bridge.** The harness defines each write tool's name
as a function that throws "writes go through the normal tools", and the host
refuses any name that isn't a bridged read, whatever the sandbox sends.

**Phase 2 seam** (writes behind the same approval rules): `Bridge.writes` and
`Bridge.approve`. Today `writes` holds names only, for the rejection message;
phase 2 makes it name -> handler and has `_dispatch` await `approve(name,
args)` (the console's can_use_tool, so a card or the auto-mode verdict)
before running one. The harness's `_reject` stubs become `__call` for those.
Nothing else here has to change.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import jsonschema
from claude_agent_sdk import tool

TIMEOUT_S = 60.0
MAX_CALLS = 40
# Per-tool caps for calls that cost money or third-party quota each time
# (Google Maps, Reddit, a Gmail full-text fallback). A loop over these is
# exactly what a script makes easy.
TOOL_CAPS = {"venue_search": 5, "venue_details": 10, "reddit_search": 5,
             "email_search": 15}
CONCURRENCY = 4
HEAP_MB = 128
RESULT_CHARS = 12_000        # what goes back to the model
LOG_CHARS = 2_000            # console.log output kept
STREAM_BYTES = 8_000_000     # everything the process may print, in total
SCRIPT_CHARS = 20_000

# Tools whose bridged default differs from the direct call. `provider`'s
# brief line is for a model to read; a script wants the rows.
DEFAULTS = {"provider": {"brief": False}}
RETURNS = {"db_query": "row[]", "provider": "{count, data: item[]}"}

_DENO_CANDIDATES = ("/opt/homebrew/bin/deno", "/usr/local/bin/deno",
                    str(Path.home() / ".deno" / "bin" / "deno"))
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# No --allow-* anywhere: every Deno permission denied, and --no-prompt makes
# a denial an exception rather than a question on a tty nobody reads.
DENO_FLAGS = ("--no-prompt", "--no-config", "--no-lock", "--no-remote",
              "--no-npm", "--cached-only")


def deno_path() -> str | None:
    """The real Deno binary (symlinks resolved: Seatbelt matches the path the
    kernel execs, not the one on PATH)."""
    for c in (shutil.which("deno"), *_DENO_CANDIDATES):
        if c and os.access(c, os.X_OK):
            return os.path.realpath(c)
    return None


def _profile(deno: str, workdir: str) -> str:
    keg = str(Path(deno).parents[1])     # .../Cellar/deno/<ver>
    q = lambda p: json.dumps(p)          # SBPL string literal
    meta_dirs = ["/", "/private", "/private/tmp", "/tmp", "/var", "/private/var",
                 "/etc", "/opt", "/opt/homebrew"]
    read_trees = ["/usr/lib", "/usr/share", "/System", "/opt/homebrew/Cellar",
                  "/opt/homebrew/opt", keg, workdir]
    lits = " ".join(f"(literal {q(p)})" for p in meta_dirs)
    trees = " ".join(f"(subpath {q(p)})" for p in read_trees)
    # The mach services Deno touches at startup (logging, directory lookups,
    # trust evaluation). Narrower than mach-lookup for all: tested 11-17.
    mach = " ".join(q(s) for s in (
        "com.apple.system.opendirectoryd.libinfo", "com.apple.logd",
        "com.apple.system.notification_center", "com.apple.trustd.agent",
        "com.apple.SecurityServer", "com.apple.system.logger",
        "com.apple.coreservices.launchservicesd",
        "com.apple.CoreServices.coreservicesd", "com.apple.diagnosticd"))
    return f"""(version 1)
(deny default)
(allow process-exec (literal {q(deno)}))
(allow sysctl-read)
(allow mach-lookup (global-name {mach}))
(allow file-read-metadata {lits} (subpath "/dev") {trees})
(allow file-read* (literal "/") (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random") {trees})
"""


# ---------- the harness (runs inside the sandbox) ----------

_PRELUDE = r"""
const __enc = new TextEncoder();
const __stdout = Deno.stdout;
function __send(m) {
  let b = __enc.encode(JSON.stringify(m) + "\n");
  while (b.length) b = b.subarray(__stdout.writeSync(b));
}
const __pending = new Map();
let __next = 0;
function __call(name, args) {
  const id = ++__next;
  __send({ t: "call", id, name, args: args ?? {} });
  return new Promise((res, rej) => __pending.set(id, { res, rej }));
}
function _reject(name) {
  return () => { throw new Error(`${name} is a write (or asks before it runs). run_script is read-only: writes go through the normal tools, so call ${name} directly.`); };
}
(async () => {
  let buf = "";
  for await (const chunk of Deno.stdin.readable.pipeThrough(new TextDecoderStream())) {
    buf += chunk;
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const m = JSON.parse(buf.slice(0, i)); buf = buf.slice(i + 1);
      const p = __pending.get(m.id); if (!p) continue;
      __pending.delete(m.id);
      "error" in m ? p.rej(new Error(m.error)) : p.res(m.value);
    }
  }
})();
const __fmt = (a) => typeof a === "string" ? a : (() => { try { return JSON.stringify(a); } catch { return String(a); } })();
console.log = console.info = console.warn = console.error = console.debug =
  (...a) => __send({ t: "log", text: a.map(__fmt).join(" ") });
"""

_EPILOGUE = r"""
__main().then(
  (v) => { try { __send({ t: "done", value: v === undefined ? null : v }); }
           catch (e) { __send({ t: "fail", error: "the return value isn't JSON: " + e }); }
           Deno.exit(0); },
  (e) => { __send({ t: "fail", error: String((e && e.stack) || e) }); Deno.exit(0); });
"""


def harness(script: str, reads: list[str], writes: list[str]) -> tuple[str, int]:
    """main.ts for one run, and the line the script's own first line lands
    on (to turn `main.ts:57` back into `line 3` in an error)."""
    defs = [f"const {n} = (a) => __call({json.dumps(n)}, a);" for n in reads]
    defs += [f"const {n} = _reject({json.dumps(n)});" for n in writes]
    head = _PRELUDE + "\n".join(defs) + "\nasync function __main() {\n"
    return head + script + "\n}\n" + _EPILOGUE, head.count("\n") + 1


# ---------- signatures (the tool description's API list) ----------

_TS = {"string": "string", "integer": "number", "number": "number",
       "boolean": "boolean", "array": "any[]", "object": "object"}


def _ts_type(spec: dict) -> str:
    t = spec.get("type")
    if isinstance(t, list):
        return "|".join(_TS.get(x, "any") for x in t)
    if t == "array" and isinstance(spec.get("items"), dict):
        return f"{_ts_type(spec['items'])}[]"
    # A description that is nothing but "One of: a, b, c" is the vocabulary:
    # render it as the union. (provider's `range` also takes explicit dates,
    # so a description with more after the list stays a plain string.)
    m = re.fullmatch(r"One of: ([\w\-, ]+)\.?", spec.get("description") or "")
    if t == "string" and m:
        return "|".join(json.dumps(v.strip()) for v in m.group(1).split(","))
    return _TS.get(t, "any")


def signature(t) -> str:
    """`email_search({query, label?, days?: number}) → string`, in the
    shorthand the description's header defines: an untyped parameter is a
    string. No per-function prose — the names carry it, and each tool's
    full description is in the tool list already. Every session carries
    this text, so it is held to ~1.2k chars (test_tool_eval's budget)."""
    props = t.input_schema.get("properties", {})
    req = set(t.input_schema.get("required", []))
    args = []
    for k, v in props.items():
        ts = _ts_type(v)
        args.append(f"{k}{'' if k in req else '?'}{'' if ts == 'string' else ': ' + ts}")
    arg = f"{{{', '.join(args)}}}" if props else ""
    ret = RETURNS.get(t.name)
    return f"{t.name}({arg})" + (f" → {ret}" if ret else "")


# ---------- the host side ----------

def _shape(result: dict) -> Any:
    """A tool's MCP result as a script value: an error raises, JSON text
    parses (whole, or after a one-line header like db_query's "3 rows"),
    anything else is the text."""
    text = "\n".join(b.get("text", "") for b in result.get("content", [])
                     if b.get("type") == "text")
    if result.get("is_error"):
        raise ToolError(text.removeprefix("error: "))
    for body in (text, text.partition("\n")[2]):
        if body[:1] in "[{":
            try:
                return json.loads(body)
            except ValueError:
                pass
    return text


class ToolError(Exception):
    pass


@dataclass
class Run:
    ok: bool
    value: Any = None
    error: str | None = None
    logs: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    seconds: float = 0.0


@dataclass
class Bridge:
    reads: dict[str, Any]                 # name -> SdkMcpTool
    writes: list[str] = field(default_factory=list)
    # Phase 2: approve(name, args) -> bool, awaited before a write runs.
    approve: Callable[[str, dict], Awaitable[bool]] | None = None
    max_calls: int = MAX_CALLS
    timeout: float = TIMEOUT_S

    async def _dispatch(self, name: str, args: Any, run: Run) -> Any:
        if name in self.writes:
            raise ToolError(f"{name} is a write (or asks before it runs). "
                            f"run_script is read-only: writes go through the "
                            f"normal tools, so call {name} directly.")
        t = self.reads.get(name)
        if t is None:
            raise ToolError(f"no such function {name!r} in run_script")
        if len(run.calls) >= self.max_calls:
            raise ToolError(f"call cap reached ({self.max_calls} per script): "
                            f"do fewer reads, or use one db_query with a JOIN")
        cap = TOOL_CAPS.get(name)
        if cap is not None and run.calls.count(name) >= cap:
            raise ToolError(f"{name} is capped at {cap} calls per script "
                            f"(each one costs quota)")
        if not isinstance(args, dict):
            raise ToolError(f"{name} takes one object argument, e.g. "
                            f"{name}({{...}})")
        args = {**DEFAULTS.get(name, {}), **args}
        try:
            jsonschema.validate(args, t.input_schema)
        except jsonschema.ValidationError as e:
            raise ToolError(f"{name}: {e.message}") from None
        run.calls.append(name)
        return _shape(await t.handler(args))

    async def run(self, script: str) -> Run:
        run = Run(ok=False)
        deno = deno_path()
        if deno is None:
            run.error = "Deno isn't installed (brew install deno), so run_script can't run"
            return run
        if not os.access(SANDBOX_EXEC, os.X_OK):
            run.error = "no sandbox-exec on this machine, so run_script refuses to run"
            return run
        start = time.monotonic()
        workdir = os.path.realpath(tempfile.mkdtemp(prefix="kestrel-script-", dir="/tmp"))
        src, first_line = harness(script, list(self.reads), self.writes)
        Path(workdir, "main.ts").write_text(src)
        Path(workdir, "profile.sb").write_text(_profile(deno, workdir))
        proc = await asyncio.create_subprocess_exec(
            SANDBOX_EXEC, "-f", f"{workdir}/profile.sb", deno, "run", *DENO_FLAGS,
            f"--v8-flags=--max-old-space-size={HEAP_MB}", f"{workdir}/main.ts",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env={}, cwd=workdir,
            start_new_session=True, limit=1 << 20)
        sem = asyncio.Semaphore(CONCURRENCY)
        inflight: set[asyncio.Task] = set()
        printed = 0

        async def answer(msg: dict) -> None:
            async with sem:
                try:
                    reply = {"id": msg.get("id"),
                             "value": await self._dispatch(str(msg.get("name")),
                                                           msg.get("args"), run)}
                except ToolError as e:
                    reply = {"id": msg.get("id"), "error": str(e)}
                except Exception as e:           # a handler blew up
                    reply = {"id": msg.get("id"), "error": f"{msg.get('name')} failed: {e}"}
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.write((json.dumps(reply, default=str) + "\n").encode())
                try:
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        async def pump() -> None:
            nonlocal printed
            logged = 0
            while True:
                try:
                    line = await proc.stdout.readline()
                except ValueError:               # one line over `limit`
                    run.error = "the script printed a line over 1MB"
                    return
                if not line:
                    return
                printed += len(line)
                if printed > STREAM_BYTES:
                    run.error = f"the script printed over {STREAM_BYTES // 1_000_000}MB"
                    return
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue                     # not the protocol: ignore
                kind = msg.get("t") if isinstance(msg, dict) else None
                if kind == "call":
                    task = asyncio.create_task(answer(msg))
                    inflight.add(task)
                    task.add_done_callback(inflight.discard)
                elif kind == "log" and logged < LOG_CHARS:
                    text = str(msg.get("text", ""))[:LOG_CHARS - logged]
                    logged += len(text)
                    run.logs.append(text)
                elif kind == "done":
                    run.ok, run.value = True, msg.get("value")
                    return
                elif kind == "fail":
                    run.error = _relines(str(msg.get("error")), first_line)
                    return

        try:
            await asyncio.wait_for(pump(), self.timeout)
        except asyncio.TimeoutError:
            run.ok, run.error = False, f"timed out after {self.timeout:.0f}s"
        finally:
            for task in list(inflight):
                task.cancel()
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                _, err = await asyncio.wait_for(proc.communicate(), 5)
            except (asyncio.TimeoutError, ValueError):
                err = b""
            shutil.rmtree(workdir, ignore_errors=True)
        if not run.ok and run.error is None:
            full = err.decode(errors="replace").strip()
            tail = full[-600:]
            # V8 says so at the top, then prints a native backtrace.
            if re.search(r"heap out of memory|out of memory|heap limit", full[:4000], re.I):
                run.error = f"out of memory (heap capped at {HEAP_MB}MB)"
            else:
                run.error = (_relines(tail, first_line)
                             or f"the sandbox exited ({proc.returncode}) without a result")
        run.seconds = time.monotonic() - start
        return run


def _relines(text: str, first: int) -> str:
    """`file:///tmp/kestrel-script-x/main.ts:57:9` -> `line 3:9`, keeping
    only the stack frames inside the script itself."""
    def sub(m: re.Match) -> str:
        return f"line {int(m.group(1)) - first + 1}:{m.group(2)}"
    text = re.sub(r"file://\S*?main\.ts:(\d+):(\d+)", sub, text)
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("at ") or "__main" in ln)


def render(run: Run) -> str:
    """The text the model gets back: the value, capped, then one footer."""
    if run.ok:
        v = run.value
        body = v if isinstance(v, str) else json.dumps(v, default=str,
                                                       separators=(",", ":"))
    else:
        body = f"error: {run.error}"
    if len(body) > RESULT_CHARS:
        body = (body[:RESULT_CHARS] + f"\n…truncated ({len(body):,} chars): "
                "return less — filter and aggregate inside the script")
    tally: dict[str, int] = {}
    for c in run.calls:
        tally[c] = tally.get(c, 0) + 1
    calls = ", ".join(f"{k}×{n}" if n > 1 else k for k, n in tally.items())
    foot = f"[{len(run.calls)} calls{': ' + calls if calls else ''} · {run.seconds:.1f}s]"
    logs = ("\nlog:\n" + "\n".join(run.logs)) if run.logs else ""
    return f"{body}{logs}\n{foot}"


# The app.db tables questions join, for db_query inside a script. Without it
# the 11-17 eval spent 2-3 db_query calls on sqlite_master and
# pragma_table_info before every script, which is what ate the token savings.
# Columns are the ones answers use, not every one; checked against app.db
# 11-18 (tests/test_console_code.py holds them to the live schema).
SCHEMA_TABLES = {
    "fin_transactions": "transaction_id, day, merchant, name, amount, category, pending",
    "expense_reviewed": "transaction_id",
    "emails": "msg_id, day, from_name, from_addr, subject, snippet, is_sent",
    "messages": "ts, day, chat_guid, handle, contact_name, is_from_me, text, is_reaction",
    "chats": "chat_guid, display_name, is_group",
    "events": "day, start_at, end_at, summary, calendar_name, location",
    "coursework_items": "day, start, kind, title, course_code",
    "study_attempts": "day, course, kind, correct",
    "tasks": "id, title, status, due, project",
    "people": "person_key, name, birthday",
}
SCHEMA = (
    "db_query tables: " + "; ".join(f"{t}({c})" for t, c in SCHEMA_TABLES.items())
    + ". amount > 0 is money out; merchant is often null, so coalesce(merchant, "
    "name). A charge with no expense_reviewed row is unreviewed. day is "
    "YYYY-MM-DD. course_code is 'CSCI-170', study_attempts.course 'CSCI 170'.")


def make_tool(reads: list, writes: list):
    """The run_script tool over these read tools (SdkMcpTool objects).
    Called from console_tools, which owns the registry."""
    sigs = "\n".join(signature(t) for t in reads)
    bridge = Bridge(reads={t.name: t for t in reads},
                    writes=[t.name for t in writes])

    @tool(
        "run_script",
        "Answer a question that needs several reads, or rows from more than "
        "one place, in ONE call: a short async JS script over the read tools "
        "(loop, join, group, count), and only its return comes back. Decide "
        "before the first call: a read per item (each course, each charge "
        "plus its email, each friend plus the calendar) means the whole "
        "question is one script, the list it loops over included. Don't "
        "fetch the list directly and script over it, and don't probe the "
        "schema: the tables are below. One lookup, or one SQL query's total, "
        "stays a direct call. "
        "Return the answer, not the material: parse inside the script (amounts "
        "out of email_body text by regex, counts, sums) and return a few short "
        "fields per item, never bodies, whole rows or tool text. Each function "
        "below is that tool, awaited, taking its params as one object; it "
        "resolves to the tool's text unless marked, and throws on error. No "
        f"fs/net/imports; {TIMEOUT_S:.0f}s, {MAX_CALLS} calls. Read-only: "
        "writes stay direct calls. Unmarked params are strings; a range is "
        "today, this-week, last-month… or YYYY-MM-DD..YYYY-MM-DD.\n"
        + SCHEMA + "\n" + sigs,
        {"type": "object",
         "properties": {
             "description": {"type": "string",
                             "description": "What the script finds, in a few words."},
             "script": {"type": "string",
                        "description": "The body of an async function."}},
         "required": ["script"], "additionalProperties": False},
    )
    async def run_script(args: dict[str, Any]) -> dict[str, Any]:
        script = args.get("script") or ""
        # `is_error`, the key the SDK server reads (see console_tools._err).
        if not script.strip():
            return {"content": [{"type": "text", "text": "error: script is empty"}],
                    "is_error": True}
        if len(script) > SCRIPT_CHARS:
            return {"content": [{"type": "text", "text":
                    f"error: script over {SCRIPT_CHARS:,} chars — keep it short"}],
                    "is_error": True}
        run = await bridge.run(script)
        return {"content": [{"type": "text", "text": render(run)}],
                **({} if run.ok else {"is_error": True})}

    run_script.bridge = bridge
    return run_script
