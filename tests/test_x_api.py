import os, sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DRY_RUN"] = "1"
import db, x_api


@pytest.fixture(autouse=True)
def fresh(tmp_path):
    db.set_db_path(tmp_path / "t.db")
    db.init_db()


def test_dry_run_discovery_and_tweets():
    seed = x_api.get_user_by_username("@demo_seed_sea")
    people = x_api.get_following(seed["id"])
    assert {p["username"] for p in people} >= {"lin_robotics", "crypto_kev"}
    assert x_api.get_user_tweets("101")[0]["id"] == "5001"
    hits = x_api.search_recent("grasping OR tokenizer")
    assert {h["author"]["username"] for h in hits} == {"lin_robotics", "minh_llm"}


def test_dry_run_dm_closed_and_existing_conversation():
    with pytest.raises(x_api.XError) as e:
        x_api.send_dm("107", "hi")
    assert e.value.kind == "dm_closed"
    assert x_api.get_conversation_events("108")      # already messaged
    assert x_api.get_conversation_events("101") == []
    assert x_api.send_dm("101", "hi")["dm_event_id"].startswith("dry-")


def test_dry_run_costs_not_counted_as_real():
    x_api.get_user_tweets("101")
    assert db.cost_summary()["total"] == 0


def _resp(status, body, headers=None):
    r = MagicMock(status_code=status, headers=headers or {}, text=str(body))
    r.json.return_value = body
    return r


def test_error_classification():
    c = x_api._classify
    assert c(_resp(402, {"type": "https://api.x.com/2/problems/credits-depleted", "detail": "credits depleted"})).kind == "credits"
    assert c(_resp(403, {"type": "https://api.x.com/2/problems/dm-recipient-not-accepting",
                         "detail": "You do not have permission to DM one or more participants."})).kind == "dm_closed"
    rl = c(_resp(429, {"title": "Too Many Requests"}, {"x-rate-limit-reset": "1800000000"}))
    assert rl.kind == "rate_limited" and rl.reset_at == 1800000000
    assert c(_resp(401, {"title": "Unauthorized"})).kind == "auth"


def test_real_mode_logs_cost(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    db.save_tokens("tok", "ref", 7200, "900", "me")
    monkeypatch.setattr(x_api.requests, "request",
                        lambda *a, **k: MagicMock(status_code=200, content=b"x",
                                                  json=lambda: {"data": [{"id": "1", "text": "a"}, {"id": "2", "text": "b"}]}))
    x_api.get_user_tweets("101")
    assert db.cost_summary()["total"] == 0.01


def _fake_posts(monkeypatch, ids):
    monkeypatch.setattr(x_api.requests, "request",
                        lambda *a, **k: MagicMock(status_code=200, content=b"x",
                                                  json=lambda: {"data": [{"id": i, "text": "t"} for i in ids]}))


def test_same_resources_billed_once_per_day(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    db.save_tokens("tok", "ref", 7200, "900", "me")
    _fake_posts(monkeypatch, ["1", "2"])
    x_api.get_user_tweets("101")
    x_api.get_user_tweets("101")                 # same 2 posts again: X doesn't charge twice
    assert db.cost_summary()["total"] == 0.01
    _fake_posts(monkeypatch, ["2", "3"])
    x_api.get_user_tweets("101")                 # only post 3 is new
    assert db.cost_summary()["total"] == 0.015


def test_owned_reads_are_cheaper(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    db.save_tokens("tok", "ref", 7200, "900", "me")
    _fake_posts(monkeypatch, ["11", "12", "13"])
    x_api.get_following("900")                   # the connected account's own following list
    assert db.cost_summary()["total"] == 0.003
