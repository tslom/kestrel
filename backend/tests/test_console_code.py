"""run_script (console_code): the sandbox holds, writes are refused, the caps
cap, and real multi-read scripts get one compact answer.

These spawn real Deno processes under real Seatbelt, ~0.2s each. A machine
without either skips them: the tool itself fails closed there."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from app import console_code as cc
from app import console_tools as ct
from app import db

pytestmark = pytest.mark.skipif(
    cc.deno_path() is None or not os.access(cc.SANDBOX_EXEC, os.X_OK),
    reason="needs deno and macOS sandbox-exec")


def run(script: str, **bridge) -> cc.Run:
    b = cc.Bridge(reads=ct.run_script.bridge.reads,
                  writes=ct.run_script.bridge.writes, **bridge)
    return asyncio.run(b.run(script))


def call(script: str) -> tuple[str, bool]:
    out = asyncio.run(ct.run_script.handler({"script": script}))
    return out["content"][0]["text"], bool(out.get("is_error"))


# ---------- escapes ----------

ESCAPES = r"""
const got = {};
const attempt = async (name, f) => {
  try { await f(); got[name] = "ALLOWED"; } catch (e) { got[name] = e.name; }
};
await attempt("read /etc/passwd", () => Deno.readTextFile("/etc/passwd"));
await attempt("read a secret", () => Deno.readTextFile(SECRET));
await attempt("stat home", () => Deno.stat(HOME));
await attempt("list /tmp", async () => { for await (const _ of Deno.readDir("/tmp")) break; });
await attempt("write /tmp", () => Deno.writeTextFile(OUT, "pwned"));
await attempt("env HOME", () => { if (Deno.env.get("HOME") === undefined) throw new Error("empty"); });
await attempt("fetch internet", () => fetch("https://example.com"));
await attempt("fetch backend", () => fetch("http://127.0.0.1:8788/api/health"));
await attempt("tcp connect", () => Deno.connect({ hostname: "127.0.0.1", port: 8788 }));
await attempt("listen", () => Deno.listen({ port: 0 }));
await attempt("spawn", () => new Deno.Command("/bin/sh", { args: ["-c", "id"] }).output());
await attempt("import remote", () => import("https://deno.land/std/path/mod.ts"));
await attempt("import file", () => import("file:///etc/hosts"));
return got;
"""


def _escapes(tmp_path) -> str:
    secret = tmp_path / "secret.txt"
    secret.write_text("hunter2")
    consts = (f"const SECRET = {json.dumps(str(secret))};\n"
              f"const HOME = {json.dumps(str(Path.home()))};\n"
              f"const OUT = {json.dumps(str(tmp_path / 'out.txt'))};\n")
    return consts + ESCAPES


def test_nothing_escapes_the_sandbox(tmp_path):
    r = run(_escapes(tmp_path))
    assert r.ok, r.error
    allowed = [k for k, v in r.value.items() if v == "ALLOWED"]
    assert allowed == [], f"got out: {allowed}"
    assert not (tmp_path / "out.txt").exists()


def test_seatbelt_holds_even_with_every_deno_permission_on(tmp_path, monkeypatch):
    """The second layer on its own: Deno told to allow everything, and the
    kernel still says no. If Deno's permission model ever has a hole, this
    is what's left."""
    monkeypatch.setattr(cc, "DENO_FLAGS", ("-A", "--no-config", "--no-lock"))
    r = run(_escapes(tmp_path).replace(
        'return got;',
        'got.env = Object.keys(Deno.env.toObject()).join(",");\nreturn got;'))
    assert r.ok, r.error
    env = r.value.pop("env")
    allowed = [k for k, v in r.value.items() if v == "ALLOWED"]
    assert allowed == [], f"got out past Seatbelt: {allowed}"
    assert not (tmp_path / "out.txt").exists()
    # the process starts with nothing in its environment (macOS adds one)
    assert set(filter(None, env.split(","))) <= {"__CF_USER_TEXT_ENCODING"}


def test_a_forged_call_still_meets_the_host(dbfile, monkeypatch):
    """The harness's write stubs throw, but a script can bypass them and
    write the protocol line itself. The host refuses the name anyway."""
    ran = []

    async def spy(args):
        ran.append(args)
        return {"content": [{"type": "text", "text": "made"}]}

    monkeypatch.setattr(ct.task_create, "handler", spy)
    r = run('return await __call("task_create", {title: "sneaky"});')
    assert not r.ok and "writes go through the normal tools" in r.error
    assert ran == []


def test_unknown_names_are_refused():
    r = run('return await __call("Bash", {command: "id"});')
    assert not r.ok and "no such function" in r.error


# ---------- writes ----------

@pytest.mark.parametrize("name", ["task_create", "list_add", "event_create",
                                  "email_draft", "file_read", "open_tab"])
def test_writes_throw_with_the_way_out(name):
    text, err = call(f"return await {name}({{}});")
    assert err
    assert f"{name} is a write" in text and "writes go through the normal tools" in text


def test_every_write_tool_is_stubbed_and_none_is_bridged():
    bridge = ct.run_script.bridge
    assert {t.name for t in ct.WRITE_TOOLS} <= set(bridge.writes)
    assert not set(bridge.reads) & set(bridge.writes)
    assert "run_script" not in bridge.reads


# ---------- caps ----------

def test_a_busy_loop_times_out():
    r = run("while (true) {}", timeout=2)
    assert not r.ok and "timed out after 2s" in r.error
    assert r.seconds < 6


def test_a_promise_that_never_settles_times_out():
    r = run("await new Promise(() => {});", timeout=2)
    assert not r.ok and "timed out" in r.error


def test_memory_is_capped():
    r = run("const a = []; while (true) a.push(new Array(1e6).fill(1.5));",
            timeout=30)
    assert not r.ok and "out of memory" in r.error


def test_the_call_cap(dbfile):
    r = run("for (let i = 0; i < 100; i++) await db_query({sql: 'SELECT 1'});",
            max_calls=5)
    assert not r.ok and "call cap reached (5" in r.error
    assert len(r.calls) == 5


def test_paid_tools_have_their_own_cap(monkeypatch):
    async def fake(args):
        return {"content": [{"type": "text", "text": "a place"}]}
    monkeypatch.setattr(ct.venue_search, "handler", fake)
    r = run("for (let i = 0; i < 9; i++) await venue_search({query: 'x' + i});")
    assert not r.ok and "venue_search is capped at 5" in r.error


def test_print_floods_are_cut_off(monkeypatch):
    monkeypatch.setattr(cc, "STREAM_BYTES", 200_000)
    r = run("for (;;) console.log('x'.repeat(10000));", timeout=20)
    assert not r.ok and "printed over" in r.error


def test_a_big_return_is_truncated_with_advice():
    text, err = call("return 'y'.repeat(50000);")
    assert not err and "truncated (50,000 chars)" in text
    assert len(text) < cc.RESULT_CHARS + 300


def test_bad_args_fail_on_the_tool_schema(dbfile):
    text, err = call("return await db_query({query: 'SELECT 1'});")
    assert err and "db_query" in text and "query" in text


def test_errors_point_at_the_scripts_own_lines():
    text, err = call("const a = 1;\nconst b = 2;\nnull.boom;")
    assert err and "line 3" in text and "main.ts" not in text


def test_typescript_annotations_run():
    text, err = call("const n: number = 2;\nreturn n * 21;")
    assert not err and text.startswith("42")


# ---------- real multi-read scripts ----------

def test_join_tasks_to_lists_and_aggregate(dbfile):
    for t in ("pay rent", "pay tuition", "call mom"):
        db.create_task(title=t)
    text, err = call("""
const open = await db_query({sql: "SELECT id, title FROM tasks WHERE status = 'open'"});
const pays = open.filter((t) => t.title.startsWith("pay"));
const counts = await db_query({sql: "SELECT status, count(*) AS n FROM tasks GROUP BY status"});
return {pay: pays.map((t) => t.title).sort(), total: counts.find((c) => c.status === "open").n};
""")
    assert not err, text
    body, _, foot = text.rpartition("\n")
    assert json.loads(body) == {"pay": ["pay rent", "pay tuition"], "total": 3}
    assert foot == f"[2 calls: db_query×2 · {foot.split('· ')[1]}"


def test_parallel_reads_and_text_results(dbfile, monkeypatch):
    """Promise.all over a text tool: N reads, one answer, and the tool's
    own handler is what ran (the bridge adds nothing in between)."""
    seen = []

    async def fake_search(args):
        seen.append(args["query"])
        return {"content": [{"type": "text",
                             "text": f"email search {args['query']!r} (1 messages)\n- receipt"}]}

    monkeypatch.setattr(ct.email_search, "handler", fake_search)
    text, err = call("""
const merchants = ["Lyft", "Amazon", "Trader Joe's"];
const hits = await Promise.all(merchants.map((m) => email_search({query: m, days: 31})));
return merchants.map((m, i) => `${m}: ${hits[i].split("\\n").length - 1}`).join("; ");
""")
    assert not err, text
    assert text.splitlines()[0] == "Lyft: 1; Amazon: 1; Trader Joe's: 1"
    assert sorted(seen) == ["Amazon", "Lyft", "Trader Joe's"]


def test_a_thrown_tool_error_can_be_caught(dbfile):
    text, err = call("""
try { await db_query({sql: "SELECT * FROM nope"}); }
catch (e) { return "caught: " + e.message; }
""")
    assert not err and "caught: no such table: nope" in text


def test_the_description_lists_every_bridged_read_and_stays_lean():
    d = ct.run_script.description
    for name in ct.run_script.bridge.reads:
        assert f"\n{name}(" in d
    assert "task_create" not in d
    # 1.9k until 11-18, then +0.8k for the table summary (SCHEMA) and +0.3k
    # for "decide first, return the answer": the code_mode eval's probing
    # and raw-body returns are what they replaced.
    assert len(d) < 3000
