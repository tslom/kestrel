"""tasks.md follows every task write, not just the ones the UI makes.

Design: DESIGN.md → `app/tasks.py`: tasks.md follows every write.
"""

import pytest

from app import db, vault_views


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    v.mkdir()
    monkeypatch.setattr(vault_views, "VAULT_PATH", v)
    return v / vault_views.TASKS_VIEW_FILE


def test_a_provider_opened_task_lands_in_the_view(dbfile, vault):
    """finance.py's call, with no route or console tool in the path."""
    db.create_task(title="Pay Visa — min $25.00 due 2026-09-30",
                   due="2026-09-30", source="finance", source_kind="finance",
                   source_ref="acct:2026-09-30")
    assert "Pay Visa" in vault.read_text()


def test_updates_and_status_changes_regenerate_too(dbfile, vault):
    t = db.create_task(title="Check in UA123", source="travel")
    db.update_task(t["id"], title="Check in UA 123")
    assert "Check in UA 123" in vault.read_text()
    db.set_task_status(t["id"], "done")
    assert "Check in UA" not in vault.read_text()   # open tasks only
    assert not list(vault.parent.glob(".*.tmp"))    # swapped in whole


def test_a_missing_task_does_not_regenerate(dbfile, vault):
    assert db.set_task_status(999, "done") is None
    assert not vault.exists()


def test_a_test_db_never_renders_into_the_real_vault(dbfile, monkeypatch):
    """dbfile leaves VAULT_PATH on the real vault; point it at tmp so tasks.md isn't overwritten."""
    calls = []
    monkeypatch.setattr(vault_views, "regenerate_tasks_view",
                        lambda: calls.append(1))
    db.create_task(title="fixture task")
    assert calls == []


def test_a_failing_view_never_fails_the_write(dbfile, vault, monkeypatch):
    def boom():
        raise OSError("vault unmounted")
    monkeypatch.setattr(vault_views, "regenerate_tasks_view", boom)
    t = db.create_task(title="still saved")
    assert [r["id"] for r in db.list_tasks()] == [t["id"]]
