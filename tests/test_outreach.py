import os, sys, time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DRY_RUN"] = "1"
import db, pipeline, outreach, x_api


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "t.db")
    db.init_db()
    monkeypatch.setattr(outreach, "_sleep_between", lambda: None)


def approve_all():
    pipeline.discover_from_seeds(["demo_seed_sea"])
    pipeline.analyse_pending()
    for c in db.list_contacts("drafted"):
        db.approve(c["id"], c["draft"])


def by_name():
    return {c["username"]: c for c in db.list_contacts()}


def test_full_send_flow():
    approve_all()
    s = outreach.send_approved()
    people = by_name()
    assert people["lin_robotics"]["status"] == "sent"
    assert people["arjun_embodied"]["status"] == "dm_closed"        # DMs closed
    assert people["mai_agents"]["status"] == "needs_review"         # messaged outside the tool
    assert s["dm_closed"] == 1 and s["already_messaged"] == 1
    # second run sends nothing - nobody is messaged twice
    assert outreach.send_approved()["sent"] == 0
    assert db.sent_today() == s["sent"]


def test_daily_cap(monkeypatch):
    monkeypatch.setattr(outreach, "DAILY_SEND_CAP", 1)
    approve_all()
    s = outreach.send_approved()
    assert s["sent"] == 1 and "cap" in s["stopped_because"]
    assert db.status_counts()["approved"] >= 1      # the rest wait for tomorrow


def test_rate_limit_puts_back_in_queue(monkeypatch):
    approve_all()
    def boom(*a, **k):
        raise x_api.XError("rate_limited", "429", 429, reset_at=int(time.time()) + 900)
    monkeypatch.setattr(x_api, "send_dm", boom)
    n = db.status_counts()["approved"]
    s = outreach.send_approved()
    assert s["sent"] == 0 and s["stopped_because"]
    # at most the dm_closed/existing-conversation ones moved; nothing lost in 'sending'
    assert db.status_counts()["sending"] == 0
    assert db.status_counts()["approved"] >= n - 2


def test_timeout_is_never_auto_retried(monkeypatch):
    cid = db.add_candidate("101", "lin_robotics", bio="robots")
    db.save_draft(cid, 9, "robotics", "x", [], "hi", True)
    db.approve(cid, "hi")
    monkeypatch.setattr(x_api, "send_dm", lambda *a: (_ for _ in ()).throw(x_api.XError("network", "timeout")))
    outreach.send_approved()
    assert db.get_contact(cid)["status"] == "needs_review"
    assert outreach.send_approved()["sent"] == 0


def test_replies():
    approve_all()
    outreach.send_approved()
    r = outreach.sync_replies()
    assert r["new_replies"] == 2
    people = by_name()
    assert people["lin_robotics"]["status"] == "replied"
    assert "Tuesday" in people["lin_robotics"]["reply_text"]
    assert outreach.sync_replies()["new_replies"] == 0   # idempotent



def test_encrypted_chat_reply_detected(monkeypatch):
    cid = db.add_candidate("201", "zed", bio="robots")
    db.save_draft(cid, 9, "robotics", "x", [], "hi", True)
    db.approve(cid, "hi")
    monkeypatch.setattr(x_api, "send_dm", lambda *a: {"dm_event_id": "1000", "dm_conversation_id": "201-900"})
    outreach.send_approved()
    monkeypatch.setattr(x_api, "is_dry_run", lambda: False)
    monkeypatch.setattr(x_api, "connected_account", lambda: ("900", "me"))
    monkeypatch.setattr(x_api, "get_recent_dm_events", lambda n: [])
    monkeypatch.setattr(x_api, "get_chat_conversation_ids", lambda: {"201": "201-900"})
    events = [{"id": "999", "sender_id": "201", "created_at": "old"},      # before our DM: ignored
              {"id": "1001", "sender_id": "900", "created_at": "x"}]       # our own message: ignored
    monkeypatch.setattr(x_api, "get_chat_events", lambda conv, n: events)
    assert outreach.sync_replies()["new_replies"] == 0
    events.insert(0, {"id": "1002", "sender_id": "201", "created_at": "new"})
    r = outreach.sync_replies()
    assert r["new_replies"] == 1 and r["chat_checked"] == 1
    assert db.get_contact(cid)["status"] == "replied"


def test_chat_api_down_does_not_break_sync(monkeypatch):
    approve_all()
    outreach.send_approved()
    def down():
        raise x_api.XError("other", "[503] Service Unavailable", 503)
    monkeypatch.setattr(x_api, "get_chat_conversation_ids", down)
    r = outreach.sync_replies()
    assert r["new_replies"] == 2 and r["note"]
