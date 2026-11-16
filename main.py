"""
main.py - web UI. Run:  python main.py   then open http://127.0.0.1:8000
"""
import csv
import io
import threading
import traceback
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

import uvicorn  # noqa: E402
from fastapi import FastAPI, Form, Request, UploadFile  # noqa: E402
from fastapi.responses import HTMLResponse, RedirectResponse  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

import db  # noqa: E402
import outreach  # noqa: E402
import pipeline  # noqa: E402
import x_api  # noqa: E402

app = FastAPI(title="GenAI Fund - X Outreach")
templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))

# Times are stored in UTC and shown in the team's time zone (default GMT+7, Southeast Asia).
# A fixed offset avoids needing time-zone data on Windows.
import os  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

UTC_OFFSET = float(os.environ.get("DISPLAY_UTC_OFFSET", "7"))
TZ_LABEL = f"GMT{UTC_OFFSET:+g}"
_TZ = timezone(timedelta(hours=UTC_OFFSET))


def local_time(value, seconds=False):
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_TZ).strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")


templates.env.filters["local"] = local_time
templates.env.globals["TZ"] = TZ_LABEL
_migrated = db.migrate_legacy()
if _migrated:
    print("Data migration:", _migrated)

# ------------------------------------------------------------------ whose data?
# Every request works on the data of the X account that is connected right now
# (dry-run: the demo data). With no account connected, only the Connect screen
# is reachable - no contacts, drafts or history are shown or changed.
OPEN_PATHS = {"/", "/connect", "/callback", "/job"}


def select_account():
    if x_api.is_dry_run():
        db.use_account("demo")
    else:
        acct = x_api.connected_account()
        db.use_account(acct[0] if acct else None)


@app.middleware("http")
async def scope_to_account(request: Request, call_next):
    select_account()
    if not db.has_account() and request.url.path not in OPEN_PATHS:
        return RedirectResponse("/?msg=Connect%20your%20X%20account%20first.&kind=error", status_code=303)
    return await call_next(request)

# ------------------------------------------------------------------ background jobs
# One job at a time (discovery/analysis/sending can take minutes). The page polls.
JOB = {"name": None, "running": False, "log": [], "result": None, "error": None, "shown": True}
_job_lock = threading.Lock()


def start_job(name, fn):
    with _job_lock:
        if JOB["running"]:
            return False
        JOB.update(name=name, running=True, log=[], result=None, error=None, shown=False)

    def say(msg):
        JOB["log"].append(msg)
        JOB["log"] = JOB["log"][-200:]

    def run():
        try:
            JOB["result"] = fn(say)
        except x_api.XError as e:
            JOB["error"] = e.human()
        except Exception as e:  # noqa: BLE001
            JOB["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            JOB["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return True


def _reset_job():
    if not JOB["running"]:
        JOB.update(name=None, log=[], result=None, error=None)


def flash(request: Request, path: str, msg: str = "", kind: str = "info"):
    from urllib.parse import quote
    sep = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{sep}msg={quote(msg)}&kind={kind}" if msg else path, status_code=303)


def _job_for_page():
    """A running job is shown on every page; a finished one only on the first page
    load after it ended (the result is in the Log page afterwards)."""
    view = dict(JOB)
    if not JOB["running"]:
        if JOB.get("shown"):
            view["name"] = None
        else:
            JOB["shown"] = True
    return view


def ctx(request: Request, **kw):
    acct = x_api.connected_account()
    counts = db.status_counts() if db.has_account() else {st: 0 for st in db.STATUSES}
    return {
        "request": request, "acct": acct, "dry_run": x_api.is_dry_run(),
        "llm_live": bool(pipeline.OPENAI_API_KEY) and not (x_api.is_dry_run() and
                                                            __import__("os").environ.get("LLM_DRY_RUN", "1") == "1"),
        "counts": counts, "attention": counts["replied"] + counts["needs_review"] + counts["failed"],
        "cost": db.cost_summary() if db.has_account() else {"today": 0, "total": 0, "by_endpoint": []},
        "sent_today": db.sent_today() if db.has_account() else 0, "cap": outreach.DAILY_SEND_CAP,
        "job": _job_for_page(), "msg": request.query_params.get("msg"), "kind": request.query_params.get("kind", "info"),
        "missing_scopes": x_api.missing_scopes(),
        "model": pipeline.OPENAI_MODEL, "min_score": pipeline.MIN_SCORE, **kw,
    }


# ------------------------------------------------------------------ pages

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not db.has_account():
        return templates.TemplateResponse(request, "connect.html", ctx(request))
    return templates.TemplateResponse(request, "home.html", ctx(
        request, events=db.recent_events(15),
        last_sync=db.get_meta("last_reply_sync")))


@app.get("/review", response_class=HTMLResponse)
def review(request: Request):
    drafted = db.list_contacts("drafted", order="generic ASC, score DESC, id ASC")
    approved = db.list_contacts("approved", order="approved_at ASC")
    return templates.TemplateResponse(request, "review.html", ctx(request, drafted=drafted, approved=approved))


@app.get("/sent", response_class=HTMLResponse)
def sent(request: Request):
    rows = db.list_contacts(("sent", "replied", "dm_closed", "failed", "needs_review"), order="updated_at DESC")
    return templates.TemplateResponse(request, "sent.html", ctx(request, rows=rows))


@app.get("/attention", response_class=HTMLResponse)
def attention(request: Request):
    return templates.TemplateResponse(request, "attention.html", ctx(
        request,
        replied=db.list_contacts("replied", order="replied_at DESC"),
        review=db.list_contacts("needs_review"),
        failed=db.list_contacts("failed"),
        closed=db.list_contacts("dm_closed")))


@app.get("/people", response_class=HTMLResponse)
def people(request: Request):
    return templates.TemplateResponse(request, "people.html", ctx(
        request, found=db.list_contacts("found", order="id ASC"),
        rejected=db.list_contacts("rejected", order="updated_at DESC")))


@app.get("/log", response_class=HTMLResponse)
def log(request: Request):
    return templates.TemplateResponse(request, "log.html", ctx(request, events=db.recent_events(300)))


@app.get("/job", response_class=HTMLResponse)
def job_status(request: Request):
    return templates.TemplateResponse(request, "_job.html", {"request": request, "job": JOB})


# ------------------------------------------------------------------ auth

@app.get("/connect")
def connect(request: Request):
    if x_api.is_dry_run():
        return flash(request, "/", "Dry-run mode: no X connection needed. Set DRY_RUN=0 in .env to use your real account.")
    if not x_api.CLIENT_ID:
        return flash(request, "/", "X_CLIENT_ID is missing in .env", "error")
    if JOB["running"]:
        return flash(request, "/", "Wait for the running job to finish before switching accounts.", "error")
    return RedirectResponse(x_api.authorize_url())


@app.get("/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return flash(request, "/", f"X authorization was cancelled: {error}", "error")
    try:
        me = x_api.handle_callback(code, state)
    except x_api.XError as e:
        return flash(request, "/", e.human() + f" ({e})", "error")
    _reset_job()
    return RedirectResponse("/", status_code=303)


@app.post("/disconnect")
def disconnect(request: Request):
    if JOB["running"]:
        return flash(request, "/", "Wait for the running job to finish before disconnecting.", "error")
    x_api.disconnect()
    db.use_account(None)
    _reset_job()
    return RedirectResponse("/", status_code=303)


# ------------------------------------------------------------------ discovery / analysis

@app.post("/find")
def find(request: Request, seeds: str = Form(""), query: str = Form(""), focus: str = Form(""),
         max_per_seed: int = Form(50), analyse_limit: int = Form(25)):
    handles = [h for h in seeds.replace(",", "\n").split() if h.strip()]
    if not handles and not query.strip():
        return flash(request, "/", "Enter seed accounts or a search query.", "error")

    def job(say):
        new_total = []
        if handles:
            say(f"Reading who {', '.join('@' + h.lstrip('@') for h in handles)} follow...")
            new, errs = pipeline.discover_from_seeds(handles, max_per_seed)
            new_total += new
            for e in errs:
                say("⚠ " + e)
        if query.strip():
            say(f"Searching recent posts: {query}")
            new, errs = pipeline.discover_from_search(query.strip(), max_per_seed)
            new_total += new
            for e in errs:
                say("⚠ " + e)
        say(f"{len(new_total)} new people (already-known and do-not-contact skipped). Analysing...")
        out = pipeline.analyse_pending(extra_context=focus, limit=analyse_limit)
        drafted = sum(1 for r in out["results"] if r.get("score") is not None and not r.get("low_fit"))
        low = sum(1 for r in out["results"] if r.get("low_fit"))
        errs = [r["error"] for r in out["results"] if r.get("error")]
        for e in errs[:5]:
            say("⚠ " + e)
        say(f"Done: {drafted} drafts ready for review, {low} low fit, "
            f"{out['skipped_no_signal']} skipped (no AI/robotics signal in bio).")
        return {"new": len(new_total), "drafted": drafted}

    if not start_job("Find & analyse", job):
        return flash(request, "/", "Another job is running - wait for it to finish.", "error")
    return flash(request, "/")


@app.post("/analyse")
def analyse(request: Request, focus: str = Form(""), include_no_signal: str = Form("")):
    def job(say):
        out = pipeline.analyse_pending(extra_context=focus, include_no_signal=bool(include_no_signal))
        say(f"Analysed {len(out['results'])}, skipped {out['skipped_no_signal']} (no AI signal).")
        return out
    if not start_job("Analyse", job):
        return flash(request, "/people", "Another job is running.", "error")
    return flash(request, "/people")


@app.post("/contact/{cid}/restore")
def restore(request: Request, cid: int):
    ok = db.restore(cid)
    return flash(request, "/people", "Moved back to 'found' - click Analyse." if ok else
                 "Can't restore (on do-not-contact list?)", "ok" if ok else "error")


# ------------------------------------------------------------------ review

@app.post("/contact/{cid}/approve")
def approve(request: Request, cid: int, text: str = Form(...)):
    try:
        ok = db.approve(cid, text)
    except ValueError as e:
        return flash(request, "/review", str(e), "error")
    return flash(request, "/review", "Approved." if ok else "Not in a state that can be approved.", "ok" if ok else "error")


@app.post("/contact/{cid}/save")
def save_edit(request: Request, cid: int, text: str = Form(...)):
    c = db.get_contact(cid)
    if c and c["status"] == "drafted":
        with db.connect() as conn:
            conn.execute("UPDATE contacts SET draft=?, updated_at=? WHERE id=? AND status='drafted'",
                         (text.strip(), db.now(), cid))
        db.log_event(cid, "draft_edited", text.strip())
    return flash(request, "/review", "Draft saved.", "ok")


@app.post("/contact/{cid}/reject")
def reject(request: Request, cid: int, back: str = Form("/review")):
    db.reject(cid, "manual")
    return flash(request, back, "Rejected.")


@app.post("/contact/{cid}/unapprove")
def unapprove(request: Request, cid: int):
    db.unapprove(cid)
    return flash(request, "/review", "Moved back to drafts.")


@app.post("/contact/{cid}/dnc")
def dnc(request: Request, cid: int, back: str = Form("/attention")):
    c = db.get_contact(cid)
    if c:
        db.add_do_not_contact(c["x_user_id"], c["username"], "manual")
    return flash(request, back, f"@{c['username']} will never be contacted again." if c else "Not found.", "ok")


@app.post("/contact/{cid}/mark_replied")
def mark_replied_manual(request: Request, cid: int, text: str = Form(""), back: str = Form("/attention")):
    """X's DM API can't read encrypted X Chat messages, so a human can record a reply seen in the X app."""
    ok = db.mark_replied(cid, text.strip() or "(reply seen in the X app - marked manually)")
    if ok:
        db.log_event(cid, "reply_marked_manually")
    return flash(request, back, "Marked as replied." if ok else "Only sent contacts can be marked replied.",
                 "ok" if ok else "error")


@app.post("/contact/{cid}/reapprove")
def reapprove(request: Request, cid: int):
    c = db.get_contact(cid)
    ok = c and db.approve(cid, c["final_text"])
    return flash(request, "/attention", "Back in the send queue." if ok else "Can't re-approve.", "ok" if ok else "error")


@app.post("/contact/{cid}/resolve")
def resolve(request: Request, cid: int, outcome: str = Form(...)):
    """needs_review -> human checked X: it was sent / it wasn't."""
    c = db.get_contact(cid)
    if not c or c["status"] != "needs_review":
        return flash(request, "/attention", "Not in review.", "error")
    with db.connect() as conn:
        if outcome == "sent":
            conn.execute("UPDATE contacts SET status='sent', sent_at=COALESCE(sent_at, ?), updated_at=? WHERE id=?",
                         (db.now(), db.now(), cid))
        else:
            conn.execute("UPDATE contacts SET status='rejected', updated_at=? WHERE id=?", (db.now(), cid))
    db.log_event(cid, "review_resolved", outcome)
    return flash(request, "/attention", "Updated.", "ok")


# ------------------------------------------------------------------ sending / replies

@app.post("/send")
def send(request: Request):
    n = db.status_counts()["approved"]
    if not n:
        return flash(request, "/review", "Nothing approved to send.", "error")
    if outreach.remaining_today() <= 0:
        return flash(request, "/review", f"Daily cap of {outreach.DAILY_SEND_CAP} already reached.", "error")

    def job(say):
        say(f"Sending {min(n, outreach.remaining_today())} of {n} approved "
            f"({'DRY RUN - nothing really sent' if x_api.is_dry_run() else 'LIVE'}), "
            f"pausing {outreach.MIN_DELAY:.0f}-{outreach.MAX_DELAY:.0f}s between messages...")
        s = outreach.send_approved(progress=say)
        say(f"Done: {s['sent']} sent, {s['dm_closed']} DMs closed, {s['already_messaged']} already messaged, "
            f"{s['failed']} failed, {s['needs_review']} need review."
            + (f" Stopped: {s['stopped_because']}" if s["stopped_because"] else ""))
        return s

    if not start_job("Send", job):
        return flash(request, "/review", "Another job is running.", "error")
    return flash(request, "/review")


@app.post("/sync")
def sync(request: Request, back: str = Form("/attention")):
    try:
        r = outreach.sync_replies()
    except x_api.XError as e:
        return flash(request, back, e.human(), "error")
    msg = (f"Checked {r['checked']} DM events and {r['chat_checked']} encrypted chats - "
           f"{r['new_replies']} new replies.")
    if r.get("note"):
        msg += " " + r["note"]
    return flash(request, back, msg, "ok" if not r.get("note") else "error")


# ------------------------------------------------------------------ import do-not-contact

@app.post("/import")
async def import_dnc(request: Request, file: UploadFile):
    """CSV with a column 'username' or 'user_id' - people already contacted outside the tool."""
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    done, errors = 0, []
    for r in rows:
        uid = (r.get("user_id") or r.get("x_user_id") or "").strip()
        uname = (r.get("username") or r.get("handle") or "").strip().lstrip("@")
        try:
            if not uid and uname:
                uid = x_api.get_user_by_username(uname)["id"]
            if uid:
                done += db.import_already_contacted([(uid, uname or None)])
        except x_api.XError as e:
            errors.append(f"@{uname}: {e.human()}")
            if e.kind in outreach.STOP_KINDS:
                break
    msg = f"Added {done} people to do-not-contact."
    if errors:
        msg += " Problems: " + "; ".join(errors[:3])
    return flash(request, "/people", msg, "ok" if not errors else "error")


if __name__ == "__main__":
    print("Open http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)
