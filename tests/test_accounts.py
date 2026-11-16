"""Data is kept per X account and nothing is visible without one."""
import os, sqlite3, sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db


@pytest.fixture
def unpinned(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_pinned", False)
    monkeypatch.setattr(db, "_active", None)
    monkeypatch.setattr(db, "_db_path", None)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "_app_path", tmp_path / "data" / "app.db")
    monkeypatch.setattr(db, "LEGACY_DB", tmp_path / "outreach.db")
    return tmp_path


def test_accounts_are_isolated(unpinned):
    db.use_account("111")
    db.add_candidate("5", "alice")
    db.use_account("222")
    assert db.list_contacts() == []
    assert db.add_candidate("5", "alice") is not None      # separate account, separate history
    db.use_account("111")
    assert [c["username"] for c in db.list_contacts()] == ["alice"]


def test_no_account_means_no_data(unpinned):
    db.use_account(None)
    assert not db.has_account()
    with pytest.raises(db.NoAccount):
        db.list_contacts()


def test_legacy_db_moves_to_its_account(unpinned):
    legacy = unpinned / "outreach.db"
    c = sqlite3.connect(legacy)
    c.executescript(db.SCHEMA + db.APP_SCHEMA)
    c.execute("INSERT INTO auth VALUES (1,'tok','ref',0,'777','fund')")
    c.execute("INSERT INTO contacts (x_user_id, username, status, created_at, updated_at) "
              "VALUES ('9','bob','sent','t','t')")
    c.commit(); c.close()
    assert "account_777" in db.migrate_legacy()
    assert not legacy.exists()
    assert db.get_tokens()["user_id"] == "777"               # still logged in
    db.use_account("777")
    assert db.list_contacts()[0]["username"] == "bob"


def test_live_without_login_shows_only_connect(unpinned, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    import main
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main.x_api, "CLIENT_ID", "abc")
    client = TestClient(main.app)
    home = client.get("/")
    assert "Connect your X account" in home.text and "Review" not in home.text
    r = client.get("/review", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/?")
    assert client.post("/send", follow_redirects=False).status_code == 303
    monkeypatch.setenv("DRY_RUN", "1")
