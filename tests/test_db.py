import sys, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db


def fresh(tmp_path):
    db.set_db_path(tmp_path / "t.db")
    db.init_db()


def make_approved(uid="1", name="alice"):
    cid = db.add_candidate(uid, name, bio="robotics")
    db.save_draft(cid, 8, "founder", "builds robots", [{"tweet_id": "9"}], "hi", False)
    assert db.approve(cid, "hi alice")
    return cid


def test_no_duplicate_candidates(tmp_path):
    fresh(tmp_path)
    assert db.add_candidate("1", "alice") is not None
    assert db.add_candidate("1", "alice") is None


def test_do_not_contact_blocks_discovery_and_sending(tmp_path):
    fresh(tmp_path)
    db.add_do_not_contact("2", "bob")
    assert db.add_candidate("2", "bob") is None
    cid = make_approved("3", "carol")
    db.add_do_not_contact("3", "carol")       # added after approval
    assert db.claim_for_sending(cid) is False
    assert db.get_contact(cid)["status"] == "rejected"


def test_double_click_sends_once(tmp_path):
    fresh(tmp_path)
    cid = make_approved()
    assert db.claim_for_sending(cid) is True
    assert db.claim_for_sending(cid) is False
    db.mark_sent(cid, "e1", "c1")
    assert db.claim_for_sending(cid) is False
    assert db.approve(cid, "again") is False   # sent contacts can't be re-approved
    assert db.sent_today() == 1


def test_concurrent_claims_only_one_wins(tmp_path):
    fresh(tmp_path)
    cid = make_approved()
    wins = []
    def go():
        wins.append(db.claim_for_sending(cid))
    ts = [threading.Thread(target=go) for _ in range(20)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert wins.count(True) == 1


def test_crash_while_sending_goes_to_review(tmp_path):
    fresh(tmp_path)
    cid = make_approved()
    db.claim_for_sending(cid)
    db.init_db()   # simulate restart
    c = db.get_contact(cid)
    assert c["status"] == "needs_review"
    assert db.claim_for_sending(cid) is False


def test_dm_closed_and_reply(tmp_path):
    fresh(tmp_path)
    a = make_approved("1", "a"); b = make_approved("2", "b")
    db.claim_for_sending(a); db.mark_send_failed(a, "dm_closed", "not accepting")
    db.claim_for_sending(b); db.mark_sent(b, "e", "c")
    assert db.mark_replied(b, "sure, let's talk")
    counts = db.status_counts()
    assert counts["dm_closed"] == 1 and counts["replied"] == 1


def test_cost_tracking(tmp_path):
    fresh(tmp_path)
    db.log_usage("dm_send", 1, 0.015)
    db.log_usage("user_lookup", 3, 0.03)
    db.log_usage("dm_send", 1, 0.015, dry_run=True)
    assert db.cost_summary()["today"] == 0.045
