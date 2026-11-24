"""Settings → Claude account (app/claude_account.py).

Fake `claude` throughout. Pins response fields (named only), the argv the email
lands on, and one cancellable sign-in at a time.
"""

import json
import stat
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import claude_account


STATUS = {"loggedIn": True, "authMethod": "claude.ai", "email": "a@example.com",
          "subscriptionType": "pro", "loginExpiresAt": "2026-12-03T00:00:00Z",
          "orgId": "secret-org", "accessToken": "must-not-cross"}


@pytest.fixture
def cli(tmp_path, monkeypatch):
    log = tmp_path / "argv"

    def install(login='exit 0'):
        f = tmp_path / "claude"
        f.write_text("#!/bin/sh\n"
                     f'echo "$@" >> {log}\n'
                     f'if [ "$2" = status ]; then echo \'{json.dumps(STATUS)}\'; exit 0; fi\n'
                     f"{login}\n")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)

    install()
    monkeypatch.setattr(claude_account, "CONSOLE_CLI_PATH", tmp_path / "claude")
    monkeypatch.setattr(claude_account._Login, "proc", None)
    monkeypatch.setattr(claude_account._Login, "state", "idle")
    monkeypatch.setattr(claude_account._Login, "error", None)
    install.log = log
    return install


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(claude_account.router)
    return TestClient(app)


def settled(client, state):
    for _ in range(100):
        login = client.get("/api/console/account").json()["login"]
        if login["state"] == state:
            return login
        time.sleep(0.02)
    raise AssertionError(f"never reached {state}: {login}")


def test_only_named_fields_cross(cli, client):
    body = client.get("/api/console/account")
    assert body.json()["account"] == {
        "logged_in": True, "method": "claude.ai", "email": "a@example.com",
        "plan": "pro", "expires_at": "2026-12-03T00:00:00Z"}
    assert "must-not-cross" not in body.text
    assert "secret-org" not in body.text


def test_a_cli_that_says_nothing_useful_reads_as_signed_out(cli, client, tmp_path):
    (tmp_path / "claude").write_text("#!/bin/sh\necho not json\n")
    account = client.get("/api/console/account").json()["account"]
    assert account["logged_in"] is False
    assert "couldn't ask" in account["error"]


def test_login_runs_the_clis_own_command_with_the_email(cli, client):
    assert client.post("/api/console/account/login", json={"email": "b@example.com"}).status_code == 200
    settled(client, "done")
    assert "auth login --email b@example.com" in cli.log.read_text()


def test_login_with_no_email_passes_no_flag(cli, client):
    client.post("/api/console/account/login", json={})
    settled(client, "done")
    assert "auth login\n" in cli.log.read_text()


@pytest.mark.parametrize("bad", ["--help", "-x@example.com", "a b@example.com", "nope"])
def test_an_email_that_isnt_one_never_reaches_the_argv(cli, client, bad):
    assert client.post("/api/console/account/login", json={"email": bad}).status_code == 400
    assert not cli.log.exists()


def test_a_failed_login_says_why_without_the_url(cli, client):
    cli('echo "Opening browser to sign in…"; '
        'echo "visit: https://claude.com/oauth?state=abc"; echo "Login cancelled" ; exit 1')
    client.post("/api/console/account/login", json={})
    assert settled(client, "failed")["error"] == "Login cancelled"


def test_one_at_a_time_and_cancel_stops_it(cli, client):
    cli("exec sleep 30")
    assert client.post("/api/console/account/login", json={}).json()["state"] == "waiting"
    assert client.post("/api/console/account/login", json={}).status_code == 409
    proc = claude_account._Login.proc
    assert client.post("/api/console/account/login/cancel").json() == {"state": "idle", "error": None}
    assert proc.wait(5) != 0
    # The watcher sees its proc is no longer current, so a cancel stays a cancel.
    time.sleep(0.1)
    assert client.get("/api/console/account").json()["login"]["state"] == "idle"
