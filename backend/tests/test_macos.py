"""app/macos.py — the one osascript/open wrapper. Nothing here runs either
binary: subprocess.run is stubbed in every test."""

import subprocess

import pytest

from app import contacts, macos, reminders


def _timeout(cmd, **kw):
    raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))


def test_a_timeout_raises_the_callers_error_type(monkeypatch):
    """reminders._run used to let TimeoutExpired escape as itself — past the
    ReminderError its callers catch — whenever Reminders hung on launch."""
    monkeypatch.setattr(macos.subprocess, "run", _timeout)
    with pytest.raises(reminders.ReminderError, match="could not run osascript"):
        reminders._run("script")
    with pytest.raises(contacts.ContactError):
        contacts._run("script")
    assert reminders.available() is False


def test_values_ride_after_the_double_dash(monkeypatch):
    seen = {}

    def run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return subprocess.CompletedProcess(cmd, 0, stdout=" id-1\n", stderr="")

    monkeypatch.setattr(macos.subprocess, "run", run)
    assert macos.osascript("S", 'a "quoted"', "b", timeout=5) == "id-1"
    assert seen["cmd"] == ["osascript", "-e", "S", "--", 'a "quoted"', "b"]
    assert seen["kw"]["timeout"] == 5


def test_open_url_reports_what_open_said(monkeypatch):
    def run(cmd, **kw):
        assert kw["check"] and kw["timeout"]
        raise subprocess.CalledProcessError(1, cmd, stderr="no application\n")

    monkeypatch.setattr(macos.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="no application"):
        macos.open_url("obsidian://open", background=True)


def test_open_url_has_a_timeout(monkeypatch):
    """vault_views ran `open -g` with none; a wedged LaunchServices hung the
    request thread forever."""
    monkeypatch.setattr(macos.subprocess, "run", _timeout)
    with pytest.raises(RuntimeError, match="could not run"):
        macos.open_url("imessage://x")
