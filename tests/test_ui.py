"""Clicks through the whole UI in dry-run mode."""
import os, sys, time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DRY_RUN"] = "1"
import db, outreach

db.set_db_path(":memory:")  # main.py runs init_db() on import
from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "ui.db")
    db.init_db()
    monkeypatch.setattr(outreach, "_sleep_between", lambda: None)
    return TestClient(main.app)


def wait_job():
    for _ in range(100):
        if not main.JOB["running"]:
            return
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_click_through(client):
    for p in ["/", "/review", "/sent", "/attention", "/people", "/log", "/job"]:
        assert client.get(p).status_code == 200, p

    client.post("/find", data={"seeds": "demo_seed_sea", "query": "", "focus": ""})
    wait_job()
    assert main.JOB["error"] is None, main.JOB
    page = client.get("/review").text
    assert "lin_robotics" in page and "open post" in page

    lin = db.get_contact_by_user_id("101")
    client.post(f"/contact/{lin['id']}/approve", data={"text": lin["draft"] + " (edited)"})
    arjun = db.get_contact_by_user_id("107")
    client.post(f"/contact/{arjun['id']}/approve", data={"text": arjun["draft"]})

    client.post("/send")
    wait_job()
    assert db.get_contact(lin["id"])["status"] == "sent"
    assert db.get_contact(lin["id"])["final_text"].endswith("(edited)")
    assert db.get_contact(arjun["id"])["status"] == "dm_closed"

    client.post("/sync", data={"back": "/attention"})
    att = client.get("/attention").text
    assert "Tuesday" in att and "arjun_embodied" in att

    # cannot send again
    client.post("/send")
    wait_job()
    assert db.sent_today() == 1
    assert "lin_robotics" in client.get("/sent").text


def test_finished_job_banner_shows_once(client):
    client.post("/find", data={"seeds": "demo_seed_sea", "query": "", "focus": ""})
    wait_job()
    assert "Find &amp; analyse" in client.get("/review").text      # first page after it finished
    assert "Find &amp; analyse" not in client.get("/attention").text  # gone afterwards
