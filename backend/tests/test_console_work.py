"""console_work: what a tab had in flight, so a quit's damage can be resumed.

A row exists only while a tab has work in flight, under this server's BOOT;
one left by another boot is work a quit cut off. The page's resume text is
lib/interrupted.js, run here under node so pytest holds it too.
"""

import shutil
import subprocess
from pathlib import Path

from app import console_link, console_work, db

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
AGENT = {"id": "a1", "description": "Analyze reference sites", "kind": "local_agent",
         "status": "running"}


def _mark(tab, work, con):
    console_work.mark(tab, session_id="s1", profile="dev", project="/p", label=None,
                      work=work, con=con)


def test_work_in_flight_is_recorded_and_cleared_when_it_ends(dbfile):
    con = db.connect()
    _mark("t1", console_work.snapshot(True, [AGENT]), con)
    row = con.execute("SELECT * FROM console_work").fetchone()
    assert row["tab"] == "t1" and row["turn"] == 1 and row["boot"] == console_work.BOOT
    assert console_work.interrupted(con) == []          # this boot's: not cut off
    _mark("t1", console_work.snapshot(False, [{**AGENT, "status": "completed"}]), con)
    assert con.execute("SELECT COUNT(*) FROM console_work").fetchone()[0] == 0
    con.close()


def test_a_previous_boots_rows_are_the_interrupted_tabs(dbfile, monkeypatch):
    con = db.connect()
    _mark("t1", console_work.snapshot(True, [AGENT]), con)
    _mark("t2", console_work.snapshot(False, [AGENT]), con)
    monkeypatch.setattr(console_work, "BOOT", "next-launch")     # Kestrel relaunched
    rows = {r["tab"]: r for r in console_work.interrupted(con)}
    assert set(rows) == {"t1", "t2"}
    assert rows["t1"]["turn"] is True and rows["t1"]["agents"][0]["description"] == AGENT["description"]
    assert rows["t2"]["turn"] is False
    # This launch's work (the CLI wakes a turn on resume) is kept apart from what was cut off.
    _mark("t1", console_work.snapshot(True, []), con)
    _mark("t1", None, con)
    assert {r["tab"] for r in console_work.interrupted(con)} == {"t1", "t2"}
    # Resumed or dismissed: gone.
    assert console_work.dismiss("t1", con) is True
    assert console_work.dismiss("t2", con) is True
    assert console_work.interrupted(con) == []
    con.close()


def test_the_link_records_turns_and_agents_as_they_happen():
    calls = []
    link = console_link.Link("t1")
    link.on_work = lambda: calls.append(link.turn is not None)
    link.begin_turn({"type": "replay_user", "text": "go"})
    link._record({"type": "text_delta", "text": "x"})           # not a work event
    link._record({"type": "agents", "items": []})
    link._record({"type": "result"})
    assert calls == [True, True, False]


def test_resume_text():
    node = shutil.which("node")
    assert node, "node isn't on PATH; the resume-text tests can't run"
    r = subprocess.run([node, "--test", "tests/interrupted.test.mjs"], cwd=FRONTEND,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
