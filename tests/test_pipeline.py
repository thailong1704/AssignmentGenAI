import os, sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DRY_RUN"] = "1"
import db, pipeline


@pytest.fixture(autouse=True)
def fresh(tmp_path):
    db.set_db_path(tmp_path / "t.db")
    db.init_db()


def test_end_to_end_dry_run():
    new, errors = pipeline.discover_from_seeds(["@demo_seed_sea"])
    assert not errors and len(new) == 9
    # running discovery again adds nobody (dedup)
    assert pipeline.discover_from_seeds(["demo_seed_sea"])[0] == []

    out = pipeline.analyse_pending()
    assert out["skipped_no_signal"] == 2          # crypto_kev, foodie_tom: no AI words in bio
    drafted = {c["username"]: c for c in db.list_contacts("drafted")}
    assert "lin_robotics" in drafted and "crypto_kev" not in drafted
    lin = drafted["lin_robotics"]
    assert lin["evidence"][0]["tweet_id"] == "5001"
    assert lin["evidence"][0]["url"].endswith("/lin_robotics/status/5001")
    assert drafted["paul_ai_quiet"]["generic"] == 1   # no posts -> flagged generic


def test_hallucinated_evidence_is_removed(monkeypatch):
    cid = db.add_candidate("101", "lin_robotics", bio="robotics founder")
    monkeypatch.setattr(pipeline, "_llm", lambda *a: {
        "fit_score": 9, "category": "robotics", "reason": "builds robots",
        "evidence_post_ids": ["5001", "999999"],
        "dm": "Hi Lin, great to meet you at the conference! Loved your sim2real result."})
    r = pipeline.analyse(cid)
    c = db.get_contact(cid)
    assert [e["tweet_id"] for e in c["evidence"]] == ["5001"]
    assert any("999999" in w for w in r["warnings"])
    assert any("familiarity" in w for w in r["warnings"])


def test_all_evidence_fake_means_generic(monkeypatch):
    cid = db.add_candidate("101", "lin_robotics", bio="robotics founder")
    monkeypatch.setattr(pipeline, "_llm", lambda *a: {
        "fit_score": 9, "category": "robotics", "reason": "x",
        "evidence_post_ids": ["123"], "dm": "Hi Lin, loved your post."})
    pipeline.analyse(cid)
    assert db.get_contact(cid)["generic"] == 1


def test_restore_after_auto_reject():
    pipeline.discover_from_seeds(["demo_seed_sea"])
    pipeline.analyse_pending()
    kev = db.get_contact_by_user_id("104")
    assert kev["status"] == "rejected"
    assert db.restore(kev["id"])
    db.add_do_not_contact("106", "foodie_tom")
    assert not db.restore(db.get_contact_by_user_id("106")["id"])
