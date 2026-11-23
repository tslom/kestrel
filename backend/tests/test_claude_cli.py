"""The console bar's Claude Code version check and update button.

What's worth pinning: the installed version read off the native layout's
symlink, version ordering (numeric, not string), the release check failing
soft, and the update route reporting what `claude update` actually did.
"""

import stat

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import claude_cli


@pytest.fixture
def native(tmp_path, monkeypatch):
    """A native install: bin/claude → versions/<v>, which is a script."""
    versions = tmp_path / "versions"
    versions.mkdir()
    link = tmp_path / "claude"

    def install(v, script="#!/bin/sh\necho ok\n"):
        f = versions / v
        f.write_text(script)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
        if link.is_symlink():
            link.unlink()
        link.symlink_to(f)
        return f

    monkeypatch.setattr(claude_cli, "CONSOLE_CLI_PATH", link)
    monkeypatch.setattr(claude_cli, "SETTINGS", tmp_path / "settings.json")
    claude_cli._Latest.version, claude_cli._Latest.channel = None, None
    claude_cli._Latest.checked = claude_cli._Latest.next_try = 0.0
    return install, versions


def test_installed_version_reads_the_symlink(native):
    install, _ = native
    install("2.1.312")
    assert claude_cli.installed_version() == "2.1.312"


def test_missing_cli_is_none(native):
    assert claude_cli.installed_version() is None


def test_versions_compare_numerically():
    assert claude_cli._vkey("2.1.312") > claude_cli._vkey("2.1.99")
    assert claude_cli._vkey("2.10.0") > claude_cli._vkey("2.9.999")


def test_update_available_and_cached(native, monkeypatch):
    install, _ = native
    install("2.1.312")
    calls = []
    monkeypatch.setattr(claude_cli, "_fetch_latest",
                        lambda chan: calls.append(chan) or "2.1.313")
    s = claude_cli.status()
    assert s["update_available"] and s["latest"] == "2.1.313"
    assert s["channel"] == "latest"
    claude_cli.status()
    assert calls == ["latest"]          # held, not refetched per poll
    claude_cli.status(force=True)
    assert len(calls) == 2


def test_failed_release_check_is_not_an_update(native, monkeypatch):
    install, _ = native
    install("2.1.312")
    monkeypatch.setattr(claude_cli, "_fetch_latest", lambda chan: None)
    s = claude_cli.status()
    assert s["latest"] is None and s["update_available"] is False


def test_stable_channel_from_settings(native, monkeypatch):
    install, _ = native
    install("2.1.312")
    claude_cli.SETTINGS.write_text('{"autoUpdatesChannel": "stable"}')
    seen = []
    monkeypatch.setattr(claude_cli, "_fetch_latest", lambda chan: seen.append(chan) or "2.1.308")
    s = claude_cli.status()
    assert seen == ["stable"] and s["update_available"] is False


def _app():
    app = FastAPI()
    app.include_router(claude_cli.router)
    return TestClient(app)


def test_update_route_reports_the_new_version(native, monkeypatch):
    install, versions = native
    # `claude update` here repoints the symlink, as the real updater does.
    link = claude_cli.CONSOLE_CLI_PATH
    new = versions / "2.1.313"
    new.write_text("#!/bin/sh\n")
    old = install("2.1.312", f"#!/bin/sh\nrm '{link}'; ln -s '{new}' '{link}'; "
                             "echo 'Successfully updated'\n")
    monkeypatch.setattr(claude_cli, "_fetch_latest", lambda chan: "2.1.313")
    r = _app().post("/api/console/cli/update").json()
    assert r["ok"] and r["updated"] and r["before"] == "2.1.312"
    assert r["installed"] == "2.1.313" and r["update_available"] is False
    assert "Successfully updated" in r["output"]
    assert old.exists()


def test_update_route_already_current(native, monkeypatch):
    install, _ = native
    install("2.1.312", "#!/bin/sh\necho 'Claude Code is up to date'\n")
    monkeypatch.setattr(claude_cli, "_fetch_latest", lambda chan: "2.1.312")
    r = _app().post("/api/console/cli/update").json()
    assert r["ok"] and not r["updated"]


def test_update_route_failure_is_reported(native, monkeypatch):
    install, _ = native
    install("2.1.312", "#!/bin/sh\necho 'network down' >&2; exit 1\n")
    monkeypatch.setattr(claude_cli, "_fetch_latest", lambda chan: None)
    r = _app().post("/api/console/cli/update").json()
    assert r["ok"] is False and "network down" in r["output"]
