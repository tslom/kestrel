"""app/files.py: a path a reply names resolves only to a real file under home,
is served only as a viewer kind, and is never executed by `open`."""

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import files, macos
from app.localguard import LocalGuard


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "Desktop").mkdir(parents=True)
    (h / "Desktop" / "resume.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    (h / "Projects" / "site" / "out").mkdir(parents=True)
    (h / "Projects" / "site" / "out" / "SCHEDULE.md").write_text("# Plan\n")
    (h / "Projects" / "site" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (h / "Projects" / "site" / "page.html").write_text("<script>alert(1)</script>")
    (h / "vault" / "01-Projects").mkdir(parents=True)
    (h / "vault" / "01-Projects" / "cv.pdf").write_bytes(b"%PDF-1.4\n")
    (h / ".ssh").mkdir()
    (h / ".ssh" / "id_ed25519").write_text("secret")
    (h / "data").mkdir()
    (h / "data" / "app.db").write_text("tokens")
    (tmp_path / "outside.txt").write_text("not yours")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(files, "VAULT_PATH", h / "vault")
    monkeypatch.setattr(files, "DATA_DIR", h / "data")
    return h


@pytest.fixture
def opened(monkeypatch):
    calls = []
    monkeypatch.setattr(macos, "open_url", lambda p, **k: calls.append(("open", p)))
    monkeypatch.setattr(macos, "reveal", lambda p, **k: calls.append(("reveal", p)))
    return calls


@pytest.fixture
def client(home):
    app = FastAPI()
    app.add_middleware(LocalGuard)
    app.include_router(files.router)
    return TestClient(app, base_url="http://127.0.0.1:8788")


def test_the_forms_a_reply_writes(home):
    proj = str(home / "Projects" / "site")
    assert files.resolve("~/Desktop/resume.pdf")["kind"] == "pdf"
    assert files.resolve(str(home / "Projects/site/shot.png"))["kind"] == "image"
    hit = files.resolve("01-Projects/cv.pdf")                     # vault-relative
    assert hit["path"] == str(files._real(home / "vault/01-Projects/cv.pdf")) and hit["vault"]
    assert files.resolve("out/SCHEDULE.md", [proj])["kind"] == "markdown"
    assert files.resolve("out/SCHEDULE.md") is None               # no project, not in the vault
    assert files.resolve("~/Projects")["kind"] == "dir"
    assert files.resolve("~/Desktop/nope.pdf") is None


def test_outside_home_traversal_and_secrets_refused(home):
    assert files.resolve(str(home.parent / "outside.txt")) is None
    assert files.resolve("~/../outside.txt") is None
    assert files.resolve("../../outside.txt", [str(home / "Projects" / "site")]) is None
    assert files.resolve("/etc/hosts") is None
    assert files.resolve("~/.ssh/id_ed25519") is None
    assert files.resolve("~/data/app.db") is None
    assert files.resolve("x\0y") is None
    # a base outside home is ignored, not used as a root
    assert files.resolve("outside.txt", [str(home.parent)]) is None


def test_symlink_escape_refused(home):
    link = home / "Projects" / "site" / "escape.txt"
    os.symlink(home.parent / "outside.txt", link)
    assert files.resolve(str(link)) is None
    os.symlink(home.parent, home / "Projects" / "up")
    assert files.resolve("~/Projects/up/outside.txt") is None
    os.symlink(home / ".ssh", home / "Desktop" / "keys")
    assert files.resolve("~/Desktop/keys/id_ed25519") is None
    # a symlink that stays home is fine, and judged as its target
    os.symlink(home / "Desktop" / "resume.pdf", home / "cv-link.pdf")
    assert files.resolve("~/cv-link.pdf")["path"].endswith("Desktop/resume.pdf")


def test_resolve_route_batches(client, home):
    r = client.post("/api/files/resolve", json={
        "paths": ["~/Desktop/resume.pdf", "out/SCHEDULE.md", "/etc/passwd"],
        "bases": [str(home / "Projects" / "site")]})
    got = r.json()["files"]
    assert got["~/Desktop/resume.pdf"]["kind"] == "pdf"
    assert got["out/SCHEDULE.md"]["kind"] == "markdown"
    assert got["/etc/passwd"] is None


def test_raw_serves_viewer_kinds_with_a_declared_type(client, home):
    r = client.get("/api/files/raw", params={"path": "~/Desktop/resume.pdf"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.headers["x-content-type-options"] == "nosniff"
    r = client.get("/api/files/raw", params={"path": str(home / "Projects/site/page.html")})
    assert r.headers["content-type"].startswith("text/plain")      # never rendered as a page
    assert "sandbox" in r.headers["content-security-policy"]
    r = client.get("/api/files/raw", params={"path": str(home / "Projects/site/shot.png")})
    assert r.headers["content-type"] == "image/png"


def test_raw_refuses_relative_outside_and_non_viewable(client, home):
    (home / "Desktop" / "a.zip").write_bytes(b"PK")
    assert client.get("/api/files/raw", params={"path": "Desktop/resume.pdf"}).status_code == 400
    assert client.get("/api/files/raw", params={"path": "/etc/hosts"}).status_code == 404
    assert client.get("/api/files/raw", params={"path": "~/../outside.txt"}).status_code == 404
    assert client.get("/api/files/raw", params={"path": "~/.ssh/id_ed25519"}).status_code == 404
    assert client.get("/api/files/raw", params={"path": "~/Desktop/a.zip"}).status_code == 415
    assert client.get("/api/files/raw", params={"path": "~/Projects"}).status_code == 415


def test_open_and_reveal(client, home, opened):
    (home / "Desktop" / "a.zip").write_bytes(b"PK")
    assert client.post("/api/files/open", json={"path": "~/Desktop/a.zip"}).json()["revealed"] is False
    assert client.post("/api/files/open", json={"path": "~/Projects"}).status_code == 200
    client.post("/api/files/open", json={"path": "~/Desktop/a.zip", "reveal": True})
    zip_ = str(files._real(home / "Desktop/a.zip"))
    assert opened == [("open", zip_), ("open", str(files._real(home / "Projects"))), ("reveal", zip_)]


def test_executables_are_revealed_never_opened(client, home, opened):
    d = home / "Desktop"
    (d / "run.command").write_text("echo hi")
    (d / "go.sh").write_text("echo hi")
    (d / "noext").write_text("#!/bin/sh\necho hi")
    os.chmod(d / "noext", 0o755)
    (d / "notes.txt").write_text("hi")
    os.chmod(d / "notes.txt", 0o755)                 # an exec bit wins over the extension
    (d / "Tool.app" / "Contents").mkdir(parents=True)
    (d / "Tool.app" / "Contents" / "Info.plist").write_text("<plist/>")
    (d / "Bare" / "Contents").mkdir(parents=True)    # a bundle without the suffix
    (d / "Bare" / "Contents" / "Info.plist").write_text("<plist/>")
    for name in ("run.command", "go.sh", "noext", "notes.txt", "Tool.app", "Bare"):
        r = client.post("/api/files/open", json={"path": f"~/Desktop/{name}"})
        assert r.json()["revealed"] is True, name
    assert all(kind == "reveal" for kind, _ in opened)
    # its source is still viewable — reading a script doesn't run it
    assert files.resolve("~/Desktop/go.sh")["kind"] == "text"


def test_localguard_still_applies(client, home, opened):
    r = client.post("/api/files/open", json={"path": "~/Desktop/resume.pdf"},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.get("/api/files/raw", params={"path": "~/Desktop/resume.pdf"},
                   headers={"host": "evil.example:8788"})
    assert r.status_code == 403
    assert opened == []


def test_the_real_app_mounts_it():
    from app.main import app
    paths = {p for p in app.openapi()["paths"]}
    assert {"/api/files/resolve", "/api/files/raw", "/api/files/open"} <= paths
