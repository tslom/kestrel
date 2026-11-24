"""Searching past sessions (app/sessions.py).

The session store is a directory of Claude Code JSONL files shared with the
terminal, so these build real ones on disk rather than mocking the parse.
"""

import asyncio
import json

import pytest

from app import console_tools as ct
from app import sessions


def _rec(role, content):
    return json.dumps({"type": role, "message": {"role": role, "content": content}})


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A project dir with three sessions, written oldest-first so mtime order
    is known."""
    monkeypatch.setattr(sessions, "project_dir", lambda: tmp_path)

    def write(name, lines, mtime):
        p = tmp_path / f"{name}.jsonl"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        import os
        os.utime(p, (mtime, mtime))
        return p

    write("aaaaaaaa-1111", [
        _rec("user", "what timezone is the flight in?"),
        _rec("assistant", [{"type": "text", "text": "Eastern on that date."}]),
    ], 1000)
    write("bbbbbbbb-2222", [
        _rec("user", "<attached_context>\n{\"widget\": \"junk\"}\n</attached_context>\nadd milk to the shopping list"),
        _rec("assistant", [{"type": "tool_use", "name": "task_create",
                            "input": {"title": "buy milk"}}]),
    ], 2000)
    write("cccccccc-3333", [
        _rec("user", "unrelated question"),
        _rec("assistant", [{"type": "text", "text": "unrelated answer"}]),
    ], 3000)
    return tmp_path


def test_search_finds_a_session_by_what_was_said(store):
    hits = sessions.search_sessions("timezone")
    assert [h["id"] for h in hits] == ["aaaaaaaa-1111"]
    assert hits[0]["match_count"] == 1
    assert "timezone" in hits[0]["matches"][0]["snippet"]
    assert hits[0]["matches"][0]["kind"] == "user"


def test_search_reads_assistant_text_and_tool_calls_too(store):
    """Search covers what Claude said and did, not only the user's wording."""
    assert [h["id"] for h in sessions.search_sessions("Eastern")] == ["aaaaaaaa-1111"]
    # tool_use blocks are searched by name and by argument
    assert [h["id"] for h in sessions.search_sessions("task_create")] == ["bbbbbbbb-2222"]
    assert [h["id"] for h in sessions.search_sessions("buy milk")] == ["bbbbbbbb-2222"]


def test_search_is_case_insensitive_and_ignores_blank_queries(store):
    assert sessions.search_sessions("TIMEZONE") == sessions.search_sessions("timezone")
    assert sessions.search_sessions("   ") == []


def test_search_returns_newest_first_and_honours_limit(store):
    hits = sessions.search_sessions("e")          # matches all three
    assert [h["id"] for h in hits] == ["cccccccc-3333", "bbbbbbbb-2222", "aaaaaaaa-1111"]
    assert [h["id"] for h in sessions.search_sessions("e", limit=1)] == ["cccccccc-3333"]


def test_search_rows_can_be_resumed_like_listing_rows(store):
    """A hit carries everything a listing row does (the browser resumes from it)."""
    hit = sessions.search_sessions("timezone")[0]
    row = next(r for r in sessions.list_sessions() if r["id"] == hit["id"])
    assert {k: hit[k] for k in row} == row


def test_search_misses_stay_empty(store):
    assert sessions.search_sessions("nothing here says this") == []


def test_preview_skips_the_attached_context_dump(store):
    """The preview skips the pinned-widget JSON dump that precedes a console message."""
    row = next(r for r in sessions.list_sessions() if r["id"] == "bbbbbbbb-2222")
    assert row["preview"] == "add milk to the shopping list"
    assert "widget" not in row["preview"]


def test_listing_and_transcript_are_uncapped_by_default(tmp_path, monkeypatch):
    """Neither listing nor search truncates (no 20-session / 500-entry cap)."""
    monkeypatch.setattr(sessions, "project_dir", lambda: tmp_path)
    for i in range(25):
        (tmp_path / f"s{i:04d}.jsonl").write_text(_rec("user", f"msg {i}") + "\n",
                                                  encoding="utf-8")
    assert len(sessions.list_sessions()) == 25
    assert len(sessions.list_sessions(limit=5)) == 5

    long = tmp_path / "long.jsonl"
    long.write_text("\n".join(_rec("user", f"line {i}") for i in range(600)) + "\n",
                    encoding="utf-8")
    assert len(sessions.session_transcript("long")) == 600
    assert len(sessions.session_transcript("long", limit=10)) == 10


def test_transcript_refuses_to_escape_the_project_dir(store):
    assert sessions.session_transcript("../../etc/passwd") == []


def test_chat_search_tool_reports_the_hits(store):
    r = asyncio.run(ct.chat_search.handler({"query": "timezone"}))
    text = r["content"][0]["text"]
    assert not r.get("is_error")
    assert "aaaaaaaa" in text and "timezone" in text
    assert "user:" in text            # the speaker is labelled


def test_chat_search_tool_says_so_when_nothing_matches(store):
    r = asyncio.run(ct.chat_search.handler({"query": "zzz-no-such-thing"}))
    assert not r.get("is_error")
    assert "no past session mentions" in r["content"][0]["text"]


def test_chat_search_tool_rejects_an_empty_or_bad_argument(store):
    empty = asyncio.run(ct.chat_search.handler({"query": "  "}))
    assert empty.get("is_error")
    bad = asyncio.run(ct.chat_search.handler({"query": "x", "limit": "abc"}))
    assert bad.get("is_error") and "must be a number" in bad["content"][0]["text"]


def test_chat_search_is_auto_approved_as_a_read(store):
    """Session reads are on the read allowlist (no approval card)."""
    assert "mcp__vault__chat_search" in ct.READ_TOOL_NAMES


# One formatter serves the live stream and the transcript replay.

def test_tool_detail_picks_the_argument_worth_reading():
    assert sessions.tool_detail({"command": "git status --short",
                                 "description": "Show working tree status"}) \
        == "git status --short"


def test_tool_detail_shortens_a_path_to_its_tail(monkeypatch):
    monkeypatch.setattr(sessions, "VAULT_PATH", "/Users/x/Vault")
    assert sessions.tool_detail(
        {"file_path": "/Users/x/Vault/02-Areas/health/notes.md"}) == "health/notes.md"


def test_tool_detail_says_the_pattern_and_where():
    assert sessions.tool_detail({"pattern": "day_key", "path": "backend/app",
                                 "output_mode": "content"}) == "day_key backend/app"


def test_tool_detail_collapses_whitespace_and_caps_the_line():
    long = sessions.tool_detail({"query": "SELECT *\n  FROM tasks " + "x " * 200})
    assert "\n" not in long and len(long) == 111 and long.endswith("…")


def test_tool_detail_says_something_for_a_tool_it_does_not_know():
    assert sessions.tool_detail({"todos": [{"content": "a"}]}).startswith("todos=")
    assert sessions.tool_detail("not a dict") == ""


def test_transcript_skips_records_that_only_look_like_a_message(tmp_path, monkeypatch):
    """An api-request-blob has message.role "user"; it isn't something you said."""
    monkeypatch.setattr(sessions, "project_dir", lambda *a: tmp_path)
    rows = [
        {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "hello"}},
        {"type": "api-request-blob", "message": {"role": "user", "content": [
            {"type": "text", "text": "Tool loaded."}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "hi"}]}},
    ]
    (tmp_path / "s.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    assert [e["text"] for e in sessions.session_transcript("s")] == ["hello", "hi"]


def test_a_shell_turn_replays_as_what_you_ran(tmp_path, monkeypatch):
    """The shell_prompt wrapper is for the model: log, listing and search show the command and output."""
    from app.console import shell_prompt

    monkeypatch.setattr(sessions, "project_dir", lambda: tmp_path)
    (tmp_path / "s.jsonl").write_text("\n".join([
        _rec("user", shell_prompt("ls </tmp", "a\n<bash-stdout>b\n", 2)),
        _rec("assistant", [{"type": "text", "text": "Two files."}]),
    ]))

    entries = sessions.session_transcript("s")
    assert [e["kind"] for e in entries] == ["user", "shell", "text"]
    assert entries[0]["text"] == "!ls </tmp"
    assert entries[1]["output"] == "a\n<bash-stdout>b\n"
    assert entries[1]["code"] == 2
    assert sessions._first_user_text(tmp_path / "s.jsonl") == "!ls </tmp"
    assert sessions.search_sessions("<bash-stdout>b")[0]["match_count"] == 1


def test_a_message_quoting_the_wrapper_stays_a_message(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "project_dir", lambda: tmp_path)
    said = "why does it say <bash-input>ls</bash-input> here?"
    (tmp_path / "s.jsonl").write_text(_rec("user", said))
    assert [e["text"] for e in sessions.session_transcript("s")] == [said]
