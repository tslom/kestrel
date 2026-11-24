"""POST /api/console/upload — the composer's drop target.

The route is thin (read_upload's policy is in test_attachments); these pin the
frontend contract: every answer, refusals included, carries a label, kind and note.
"""

import asyncio
import base64
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import console
from app.providers import attachments


@pytest.fixture
def api():
    app = FastAPI()
    app.include_router(console.router)
    return TestClient(app)


def test_a_dropped_text_file_comes_back_as_text(api):
    r = api.post("/api/console/upload",
                 files={"file": ("notes.txt", b"buy milk", "text/plain")})
    assert r.status_code == 200
    body = r.json()
    assert body["label"] == "notes.txt"
    assert body["kind"] == "text" and "buy milk" in body["text"]


def _dropped(api, monkeypatch, png=b"\x89PNG\r\n\x1a\n" + b"\x00" * 32):
    monkeypatch.setattr(attachments, "_long_edge", lambda data: 800)
    return api.post("/api/console/upload",
                    files={"file": ("board.png", png, "image/png")}).json()


def test_a_dropped_image_reaches_the_session_as_an_image(api, monkeypatch):
    body = _dropped(api, monkeypatch)
    assert body["kind"] == "image" and isinstance(body["image"], str)
    payload = {"text": "what's this?", "context": [
        {"label": "board.png", "kind": "file", "data": "", "image": body["image"]}]}

    async def first_message():
        return [m async for m in console.turn_input(payload)][0]

    content = asyncio.run(first_message())["message"]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    assert base64.b64decode(content[0]["source"]["data"]).startswith(b"\x89PNG")
    assert "<attached_image name='board.png'>" in content[1]["text"]
    assert "what's this?" in content[1]["text"]


def test_a_turn_with_no_image_stays_a_plain_string():
    assert isinstance(console.turn_input({"text": "hi"}), str)


def test_an_image_gone_from_memory_says_so():
    """A restart between drop and send says the image is gone."""
    pill = {"label": "board.png", "kind": "file", "data": "", "image": "gone"}
    out = console.turn_input({"text": "hi", "context": [pill]})
    assert isinstance(out, str) and "the image is gone" in out


def test_held_images_are_bounded(api, monkeypatch):
    for _ in range(console.HELD_IMAGES_MAX + 3):
        _dropped(api, monkeypatch)
    assert len(console._held_images) == console.HELD_IMAGES_MAX


def test_a_video_is_refused_with_a_reason_not_a_500(api):
    body = api.post("/api/console/upload",
                    files={"file": ("clip.mov", b"\x00" * 32, "video/quicktime")}).json()
    assert body["kind"] == "unreadable"
    assert "cannot watch" in body["note"]


def test_every_answer_carries_the_three_fields_the_pill_renders(api):
    """Every answer has the label, note and text the pill renders (a missing key shows 'undefined')."""
    for name, data, mime in [("a.txt", b"hi", "text/plain"),
                             ("b.mov", b"\x00", "video/quicktime"),
                             ("c.txt", b"", "text/plain"),
                             ("d.bin", b"\x00\x01", "application/x-thing")]:
        body = api.post("/api/console/upload",
                        files={"file": (name, data, mime)}).json()
        assert {"label", "kind", "text", "note"} <= body.keys(), (name, body)
        assert body["label"] == name


def test_nothing_is_written_to_the_database(api, fresh_dbfile):
    """A dropped file is not cached: no row to cache against."""
    api.post("/api/console/upload",
             files={"file": ("notes.txt", b"transient", "text/plain")})
    assert not fresh_dbfile.exists(), "the upload path touched app.db"


# ---------- the desktop tray ----------

@pytest.fixture
def desk(tmp_path, monkeypatch):
    d = tmp_path / "Desktop"
    d.mkdir()
    monkeypatch.setattr(console, "_desk_dirs", lambda: [d])
    return d


def test_the_tray_lists_newest_first_and_skips_dotfiles_and_folders(api, desk):
    import os
    (desk / "old.pdf").write_bytes(b"x")
    (desk / "Screenshot new.png").write_bytes(b"x")
    (desk / ".DS_Store").write_bytes(b"x")
    (desk / "folder").mkdir()
    os.utime(desk / "old.pdf", (1, 1))
    files = api.get("/api/console/desktop").json()["files"]
    assert [f["name"] for f in files] == ["Screenshot new.png", "old.pdf"]
    assert files[0]["image"] and not files[1]["image"]


def test_attaching_from_the_tray_reads_like_a_drop(api, desk):
    (desk / "notes.txt").write_bytes(b"buy milk")
    body = api.post("/api/console/desktop/attach", json={"name": "notes.txt"}).json()
    assert body["label"] == "notes.txt"
    assert body["kind"] == "text" and "buy milk" in body["text"]


def test_the_tray_only_names_files_never_paths(api, desk, tmp_path):
    (tmp_path / "secret.txt").write_bytes(b"nope")
    for name in ["../secret.txt", "/etc/hosts", ".DS_Store", ""]:
        body = api.post("/api/console/desktop/attach", json={"name": name}).json()
        assert body["kind"] == "missing" and body["text"] == "", name
        assert api.get("/api/console/desktop/preview",
                       params={"name": name}).status_code == 404, name


def test_a_symlink_out_of_the_desktop_is_not_followed(api, desk, tmp_path):
    (tmp_path / "secret.txt").write_bytes(b"nope")
    (desk / "link.txt").symlink_to(tmp_path / "secret.txt")
    body = api.post("/api/console/desktop/attach", json={"name": "link.txt"}).json()
    assert body["kind"] == "missing"


def test_previews_are_images_only(api, desk):
    (desk / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (desk / "b.txt").write_bytes(b"hi")
    assert api.get("/api/console/desktop/preview", params={"name": "a.png"}).status_code == 200
    assert api.get("/api/console/desktop/preview", params={"name": "b.txt"}).status_code == 404


def test_an_oversized_file_is_refused_without_being_read(api, desk, monkeypatch):
    (desk / "rec.mov").write_bytes(b"\x00" * 64)
    monkeypatch.setattr(attachments, "ATTACHMENT_MAX_BYTES", 16)
    body = api.post("/api/console/desktop/attach", json={"name": "rec.mov"}).json()
    assert body["kind"] == "too_big" and body["label"] == "rec.mov"


async def _shell(command, tmp_path):
    state = console.SessionState()
    state.cwd = tmp_path
    sent = []

    async def send(frame):
        sent.append(frame)

    return *await console.run_shell(command, state, send), sent, state


def test_a_shell_command_streams_and_reports_its_code(tmp_path):
    (tmp_path / "f.txt").write_text("hi")
    output, code, sent, state = asyncio.run(_shell("cat f.txt; echo oops >&2; exit 3", tmp_path))
    assert "hi" in output and "oops" in output, "stderr has to reach Claude too"
    assert code == 3 and sent[-1] == {"type": "shell_done", "code": 3}
    assert state.shell is None and not state.shells


def test_a_shell_command_the_size_of_a_log_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "SHELL_CAP_CHARS", 100)
    output, code, _, _ = asyncio.run(_shell("seq 1 100000", tmp_path))
    assert len(output) <= 100 and code == 0


def test_a_shell_command_runs_on_a_terminal_with_plain_text_for_claude(tmp_path):
    output, code, sent, _ = asyncio.run(_shell(
        "[ -t 0 ] && echo on-a-tty; echo pager=$PAGER; printf '\\033[31mred\\033[0m\\n'", tmp_path))
    assert "on-a-tty" in output and "pager=cat" in output
    assert "red\n" in output and "\x1b" not in output and "\r\n" not in output
    assert sent[0]["type"] == "shell_tty" and code == 0


def test_a_prompt_can_be_answered_through_the_terminal(tmp_path):
    async def run():
        state = console.SessionState()
        state.cwd = tmp_path
        sent = []

        async def send(frame):
            sent.append(frame)

        task = asyncio.create_task(console.run_shell(
            "printf 'name? '; read -r who; echo \"hi $who\"", state, send))
        while not console._tty_shells:
            await asyncio.sleep(0.01)
        shell = next(iter(console._tty_shells.values()))
        await asyncio.sleep(0.2)
        os.write(shell.master, b"alex\n")
        return await task

    output, code = asyncio.run(run())
    assert "hi alex" in output and code == 0
    assert not console._tty_shells, "a finished command is no longer attachable"


def test_the_terminal_socket_replays_and_types_into_the_command(tmp_path):
    class FakeWS:
        headers = {"origin": "http://localhost:5173"}

        def __init__(self):
            self.got, self.closed, self.inbox = b"", False, asyncio.Queue()

        async def accept(self):
            pass

        async def send_bytes(self, data):
            self.got += data

        async def receive(self):
            return await self.inbox.get()

        async def close(self, code=1000):
            self.closed = True
            self.inbox.put_nowait({"type": "websocket.disconnect"})

    async def run():
        state = console.SessionState()
        state.cwd = tmp_path

        async def send(frame):
            pass

        task = asyncio.create_task(console.run_shell(
            "printf 'name? '; read -r who; echo \"hi $who\"", state, send))
        while not console._tty_shells:
            await asyncio.sleep(0.01)
        shell_id = next(iter(console._tty_shells))
        await asyncio.sleep(0.2)              # the prompt is out before we attach
        ws = FakeWS()
        viewer = asyncio.create_task(console.shell_ws(ws, shell_id))
        await asyncio.sleep(0.1)
        ws.inbox.put_nowait({"type": "websocket.receive",
                             "text": '{"type": "resize", "rows": 30, "cols": 90}'})
        ws.inbox.put_nowait({"type": "websocket.receive", "bytes": b"ada\r"})
        output, code = await task
        await asyncio.wait_for(viewer, 2)
        return ws, output, code

    ws, output, code = asyncio.run(run())
    assert b"name? " in ws.got, "a late terminal is replayed what it missed"
    assert b"hi ada" in ws.got and "hi ada" in output and code == 0
    assert ws.closed, "the terminal is closed when the command ends"


def test_the_terminal_socket_refuses_what_isnt_running():
    class FakeWS:
        headers = {}
        closed = None

        async def close(self, code=1000):
            self.closed = code

    ws = FakeWS()
    asyncio.run(console.shell_ws(ws, "nope"))
    assert ws.closed == 1008


def test_an_open_terminal_keeps_a_command_in_the_foreground(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "SHELL_TIMEOUT_S", 0.2)

    class Viewer:
        async def send_bytes(self, data):
            pass

        async def close(self):
            pass

    async def run():
        state = console.SessionState()
        state.cwd = tmp_path
        sent = []

        async def send(frame):
            sent.append(frame)

        task = asyncio.create_task(console.run_shell("sleep 0.6; echo done", state, send))
        while not console._tty_shells:
            await asyncio.sleep(0.01)
        next(iter(console._tty_shells.values())).viewers.add(Viewer())
        return await task

    output, code = asyncio.run(run())
    assert code == 0 and "done" in output, "it finished in the foreground, not moved"


def test_claude_is_told_who_ran_it():
    prompt = console.shell_prompt("ls", "a.txt", 0)
    assert "The user ran this themselves" in prompt
    assert "<bash-input>ls</bash-input>" in prompt and "<bash-stdout>a.txt</bash-stdout>" in prompt


def test_past_the_limit_a_command_moves_to_the_background_and_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "SHELL_TIMEOUT_S", 0.2)

    async def run():
        state = console.SessionState()
        state.cwd = tmp_path
        state.day = console.day_key()
        sent = []

        async def send(frame):
            sent.append(frame)

        output, code = await console.run_shell("echo start; sleep 0.5; echo end", state, send)
        assert code is None and "start" in output
        assert {"type": "shell_done", "code": None, "background": True} in sent
        await asyncio.sleep(0.8)
        return state, sent

    state, sent = asyncio.run(run())
    finished = [f for f in sent if f["type"] == "background"][-1]["items"][0]
    assert finished["running"] is False and finished["code"] == 0 and "end" in finished["output"]
    prompt = console.build_prompt({"text": "next"}, state)
    assert "<background_command" in prompt and "end" in prompt
    assert "<background_command" not in console.build_prompt({"text": "after"}, state)


@pytest.mark.parametrize("url,on", [
    ("http://localhost:5173", True), ("http://127.0.0.1:3000/", True),
    ("https://evil.test", False), ("http://localhost:5173/<x>", False), ("", False),
])
def test_auto_verify_only_ever_names_a_local_url(url, on):
    assert ("<auto_verify>" in console.build_prompt({"text": "x", "verify_url": url})) is on


def test_btw_never_touches_the_session(api, monkeypatch):
    asked = []

    async def ask(prompt, *, model):
        asked.append(prompt)
        return " sure "

    monkeypatch.setattr(console.agent, "ask", ask)
    body = api.post("/api/console/btw", json={"question": "why?", "context": "User: hi"}).json()
    assert body == {"answer": "sure"} and "User: hi" in asked[0] and "why?" in asked[0]
    assert api.post("/api/console/btw", json={"question": " "}).status_code == 400


def _file_api(api, monkeypatch, root):
    monkeypatch.setattr(console, "VAULT_PATH", root)
    return api


def test_files_read_and_save_and_refuse_a_stale_save(api, monkeypatch, tmp_path):
    (tmp_path / "a.md").write_text("one")
    _file_api(api, monkeypatch, tmp_path)
    got = api.get("/api/console/file", params={"path": "a.md"}).json()
    assert got["content"] == "one" and got["read_only"] is False
    saved = api.put("/api/console/file", json={"path": "a.md", "content": "two", "mtime": got["mtime"]})
    assert saved.status_code == 200 and (tmp_path / "a.md").read_text() == "two"
    stale = api.put("/api/console/file", json={"path": "a.md", "content": "three", "mtime": got["mtime"]})
    assert stale.status_code == 409 and (tmp_path / "a.md").read_text() == "two"


def test_files_stay_under_the_tab_and_tasks_md_is_read_only(api, monkeypatch, tmp_path):
    (tmp_path / "tasks.md").write_text("generated")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    _file_api(api, monkeypatch, tmp_path)
    assert api.get("/api/console/file", params={"path": "../outside.txt"}).status_code == 400
    assert api.get("/api/console/file", params={"path": str(outside)}).status_code == 400
    got = api.get("/api/console/file", params={"path": "tasks.md"}).json()
    assert got["read_only"] is True
    assert api.put("/api/console/file", json={"path": "tasks.md", "content": "x",
                                              "mtime": got["mtime"]}).status_code == 400
    outside.unlink()


def test_a_finished_tab_queues_one_notification(api, monkeypatch):
    """A finished tab queues one notification (the page swallows this route's errors)."""
    sent, flushed = [], []
    real_key = console.notify._notify_key
    monkeypatch.setattr(console.notify, "send", lambda kind, *parts, **kw: sent.append(
        (real_key(kind, *parts), kw["title"], kw["body"])))
    monkeypatch.setattr(console.notify, "flush", lambda: flushed.append(1))
    r = api.post("/api/console/notify", json={"id": "tab1", "at": "1", "title": "Kestrel finished", "body": "x"})
    assert r.status_code == 200 and flushed and sent[0][1:] == ("Kestrel finished", "x")
