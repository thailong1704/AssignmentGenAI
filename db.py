r"""
db.py - the single source of truth.

Everything that matters for "never double-message anyone" lives here:
  * one row per X user (UNIQUE x_user_id)
  * status changes are guarded by WHERE status = <expected>, so two clicks /
    two processes can never both move the same contact into 'sending'
  * do-not-contact list checked on insert and again before sending
  * every action is written to `events` (audit log) and every API call's
    estimated cost to `api_usage`

Contact lifecycle:
  found -> drafted -> approved -> sending -> sent -> replied
                 \-> rejected          \-> dm_closed   (recipient doesn't accept DMs)
                                       \-> failed      (other API error, retryable by a human)
                                       \-> needs_review (crash/unknown outcome: never auto-resent)
"""
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# One database PER X ACCOUNT, so data never leaks between accounts and nothing is
# visible before someone connects:
#   data/app.db                 login tokens + OAuth state (no outreach data)
#   data/account_<x_user_id>.db everything for that sending account
#   data/demo.db                dry-run data
DATA_DIR = Path(__file__).with_name("data")
LEGACY_DB = Path(__file__).with_name("outreach.db")   # single shared file used by older versions


class NoAccount(RuntimeError):
    """Raised when outreach data is touched with no X account selected."""

STATUSES = (
    "found", "drafted", "rejected", "approved", "sending",
    "sent", "replied", "dm_closed", "failed", "needs_review",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    x_user_id       TEXT NOT NULL UNIQUE,
    username        TEXT NOT NULL,
    name            TEXT,
    bio             TEXT,
    followers       INTEGER,
    source          TEXT,             -- e.g. "seed:@karpathy" or "search:embodied AI"
    status          TEXT NOT NULL DEFAULT 'found',
    score           INTEGER,          -- 0-10
    category        TEXT,             -- founder / researcher / builder / infra / robotics ...
    reason          TEXT,             -- why they fit
    evidence        TEXT,             -- JSON list [{tweet_id, text, url}] (verified real)
    draft           TEXT,             -- LLM draft
    generic         INTEGER DEFAULT 0,-- 1 = no verified evidence, draft is not personalised
    final_text      TEXT,             -- text a human approved (may be edited)
    dm_event_id     TEXT,
    dm_conversation_id TEXT,
    last_error      TEXT,
    reply_text      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    approved_at     TEXT,
    sent_at         TEXT,
    replied_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);

CREATE TABLE IF NOT EXISTS do_not_contact (
    x_user_id   TEXT PRIMARY KEY,
    username    TEXT,
    reason      TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id  INTEGER,
    type        TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint    TEXT NOT NULL,
    units       INTEGER NOT NULL DEFAULT 1,
    cost_usd    REAL NOT NULL DEFAULT 0,
    dry_run     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

APP_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    access_token  TEXT,
    refresh_token TEXT,
    expires_at    REAL,
    user_id       TEXT,
    username      TEXT
);
CREATE TABLE IF NOT EXISTS billed (      -- X bills each resource once per UTC day
    kind  TEXT NOT NULL,
    rid   TEXT NOT NULL,
    day   TEXT NOT NULL,
    PRIMARY KEY (kind, rid, day)
);
CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_prefix() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


_db_path = None              # active account database (None = no account selected)
_app_path = DATA_DIR / "app.db"
_active = None               # "demo" | x_user_id | None
_pinned = False              # tests pin explicit paths


def set_db_path(path) -> None:
    """Tests: use explicit files and stop automatic account switching."""
    global _db_path, _app_path, _pinned, _active
    _pinned = True
    _active = "pinned"
    _db_path = path if path == ":memory:" else Path(path)
    _app_path = path if path == ":memory:" else Path(str(path) + ".app")


def account_db_path(account) -> Path:
    return DATA_DIR / ("demo.db" if account == "demo" else f"account_{account}.db")


def use_account(account) -> None:
    """Select whose data every following call reads and writes.
    account = "demo" (dry-run), an X user id, or None (nothing visible)."""
    global _db_path, _active
    if _pinned or account == _active:
        return
    if account is None:
        _db_path, _active = None, None
        return
    DATA_DIR.mkdir(exist_ok=True)
    _db_path, _active = account_db_path(str(account)), str(account)
    init_db()


def active_account():
    return _active


def has_account() -> bool:
    return _db_path is not None


@contextmanager
def _open(path, schema=None):
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)  # autocommit; explicit BEGIN when needed
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    if schema:
        conn.executescript(schema)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def connect():
    """Connection to the ACTIVE account's outreach data."""
    if _db_path is None:
        raise NoAccount("No X account connected.")
    with _open(_db_path) as c:
        yield c


@contextmanager
def connect_app():
    """Connection to app-level storage (login tokens, OAuth state)."""
    if _app_path != ":memory:":
        Path(_app_path).parent.mkdir(exist_ok=True)
    with _open(_app_path, APP_SCHEMA) as c:
        yield c


def migrate_legacy() -> str:
    """Older versions kept everything in ./outreach.db. Move it to the account it
    belongs to (and keep that account logged in). Returns what happened."""
    if _pinned or not LEGACY_DB.exists():
        return ""
    DATA_DIR.mkdir(exist_ok=True)
    with _open(LEGACY_DB) as c:
        has_auth = c.execute("SELECT 1 FROM sqlite_master WHERE name='auth'").fetchone()
        auth = c.execute("SELECT * FROM auth WHERE id=1").fetchone() if has_auth else None
        scopes = None
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():
            r = c.execute("SELECT value FROM meta WHERE key='granted_scopes'").fetchone()
            scopes = r["value"] if r else None
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    if auth and auth["user_id"]:
        target = account_db_path(auth["user_id"])
        if target.exists():
            return ""
        LEGACY_DB.rename(target)
        with connect_app() as a:
            if not a.execute("SELECT 1 FROM auth WHERE id=1").fetchone():
                a.execute("INSERT INTO auth VALUES (1,?,?,?,?,?)",
                          (auth["access_token"], auth["refresh_token"], auth["expires_at"],
                           auth["user_id"], auth["username"]))
                if scopes:
                    a.execute("INSERT OR REPLACE INTO app_meta VALUES ('granted_scopes', ?)", (scopes,))
        msg = f"moved outreach.db -> {target.name} (@{auth['username']})"
    else:
        target = DATA_DIR / "legacy_unassigned.db"
        LEGACY_DB.rename(target)
        msg = f"moved outreach.db -> {target.name} (no account recorded in it)"
    for suffix in ("-wal", "-shm"):
        Path(str(LEGACY_DB) + suffix).unlink(missing_ok=True)
    return msg


def init_db() -> None:
    with connect() as c:
        c.executescript(SCHEMA)
        # Crash recovery: anything stuck in 'sending' has an UNKNOWN outcome
        # (the DM may or may not have gone out). Never auto-resend: park it for a human.
        stuck = c.execute("SELECT id FROM contacts WHERE status = 'sending'").fetchall()
        for row in stuck:
            c.execute(
                "UPDATE contacts SET status='needs_review', last_error=?, updated_at=? WHERE id=?",
                ("Interrupted while sending - check X before retrying.", now(), row["id"]),
            )
            _log(c, row["id"], "recovered_stuck_send", "moved sending -> needs_review on startup")


# ---------------------------------------------------------------- helpers

def _log(c, contact_id, type_, detail=None):
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False)
    c.execute(
        "INSERT INTO events (contact_id, type, detail, created_at) VALUES (?,?,?,?)",
        (contact_id, type_, detail, now()),
    )


def log_event(contact_id, type_, detail=None) -> None:
    with connect() as c:
        _log(c, contact_id, type_, detail)


def _row(r):
    if r is None:
        return None
    d = dict(r)
    if d.get("evidence"):
        try:
            d["evidence"] = json.loads(d["evidence"])
        except ValueError:
            d["evidence"] = []
    else:
        d["evidence"] = []
    return d


def _transition(c, contact_id, from_statuses, to_status, **fields) -> bool:
    """Guarded status change. Returns False if the contact was not in an allowed
    `from` status (i.e. someone else already moved it) - the caller must then stop."""
    assert to_status in STATUSES
    if isinstance(from_statuses, str):
        from_statuses = (from_statuses,)
    fields["status"] = to_status
    fields["updated_at"] = now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    placeholders = ",".join("?" * len(from_statuses))
    cur = c.execute(
        f"UPDATE contacts SET {sets} WHERE id = ? AND status IN ({placeholders})",
        (*fields.values(), contact_id, *from_statuses),
    )
    return cur.rowcount == 1


# ---------------------------------------------------------------- contacts

def is_do_not_contact(x_user_id) -> bool:
    with connect() as c:
        return c.execute(
            "SELECT 1 FROM do_not_contact WHERE x_user_id = ?", (str(x_user_id),)
        ).fetchone() is not None


def add_candidate(x_user_id, username, name=None, bio=None, followers=None, source=None):
    """Insert a newly discovered person. Returns the new contact id, or None if
    they already exist (any status) or are on the do-not-contact list."""
    x_user_id = str(x_user_id)
    with connect() as c:
        if c.execute("SELECT 1 FROM do_not_contact WHERE x_user_id=?", (x_user_id,)).fetchone():
            return None
        ts = now()
        cur = c.execute(
            """INSERT OR IGNORE INTO contacts
               (x_user_id, username, name, bio, followers, source, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?, 'found', ?, ?)""",
            (x_user_id, username, name, bio, followers, source, ts, ts),
        )
        if cur.rowcount == 0:
            return None
        cid = cur.lastrowid
        _log(c, cid, "discovered", source)
        return cid


def known_user_ids() -> set:
    """Everyone we already have OR must never contact - discovery skips these
    before spending money on scoring them."""
    with connect() as c:
        a = {r[0] for r in c.execute("SELECT x_user_id FROM contacts")}
        b = {r[0] for r in c.execute("SELECT x_user_id FROM do_not_contact")}
    return a | b


def get_contact(contact_id):
    with connect() as c:
        return _row(c.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone())


def get_contact_by_user_id(x_user_id):
    with connect() as c:
        return _row(c.execute("SELECT * FROM contacts WHERE x_user_id=?", (str(x_user_id),)).fetchone())


def list_contacts(statuses=None, order="updated_at DESC", limit=500):
    with connect() as c:
        if statuses:
            if isinstance(statuses, str):
                statuses = (statuses,)
            q = f"SELECT * FROM contacts WHERE status IN ({','.join('?'*len(statuses))}) ORDER BY {order} LIMIT ?"
            rows = c.execute(q, (*statuses, limit)).fetchall()
        else:
            rows = c.execute(f"SELECT * FROM contacts ORDER BY {order} LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]


def status_counts() -> dict:
    with connect() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM contacts GROUP BY status").fetchall()
    counts = {s: 0 for s in STATUSES}
    counts.update({r["status"]: r["n"] for r in rows})
    return counts


# ---------------------------------------------------------------- pipeline results

def save_draft(contact_id, score, category, reason, evidence, draft, generic) -> bool:
    with connect() as c:
        ok = _transition(
            c, contact_id, ("found", "drafted"), "drafted",
            score=score, category=category, reason=reason,
            evidence=json.dumps(evidence, ensure_ascii=False),
            draft=draft, generic=1 if generic else 0,
        )
        if ok:
            _log(c, contact_id, "drafted", {"score": score, "generic": bool(generic)})
        return ok


# ---------------------------------------------------------------- human review

def approve(contact_id, final_text) -> bool:
    final_text = (final_text or "").strip()
    if not final_text:
        raise ValueError("Message text is empty.")
    with connect() as c:
        ok = _transition(c, contact_id, ("drafted", "failed"), "approved",
                         final_text=final_text, approved_at=now(), last_error=None)
        if ok:
            _log(c, contact_id, "approved", final_text)
        return ok


def unapprove(contact_id) -> bool:
    with connect() as c:
        ok = _transition(c, contact_id, "approved", "drafted")
        if ok:
            _log(c, contact_id, "unapproved")
        return ok


def reject(contact_id, note=None) -> bool:
    with connect() as c:
        ok = _transition(c, contact_id, ("found", "drafted", "approved", "failed"), "rejected")
        if ok:
            _log(c, contact_id, "rejected", note)
        return ok


def restore(contact_id) -> bool:
    """Undo a (manual or automatic) rejection so the person can be analysed again.
    Refuses if they're on the do-not-contact list."""
    with connect() as c:
        if c.execute("SELECT 1 FROM do_not_contact d JOIN contacts ct ON ct.x_user_id=d.x_user_id "
                     "WHERE ct.id=?", (contact_id,)).fetchone():
            return False
        ok = _transition(c, contact_id, "rejected", "found")
        if ok:
            _log(c, contact_id, "restored")
        return ok


def add_do_not_contact(x_user_id, username=None, reason="manual") -> None:
    x_user_id = str(x_user_id)
    with connect() as c:
        c.execute(
            "INSERT OR IGNORE INTO do_not_contact (x_user_id, username, reason, created_at) VALUES (?,?,?,?)",
            (x_user_id, username, reason, now()),
        )
        row = c.execute("SELECT id, status FROM contacts WHERE x_user_id=?", (x_user_id,)).fetchone()
        if row:
            # Pull them out of the queue if not yet sent.
            if row["status"] in ("found", "drafted", "approved", "failed"):
                _transition(c, row["id"], row["status"], "rejected")
            _log(c, row["id"], "do_not_contact", reason)


def import_already_contacted(rows, reason="previously contacted (import)") -> int:
    """rows: iterable of (x_user_id, username). Marks people the fund already
    messaged outside this tool so they're never messaged again."""
    n = 0
    for uid, uname in rows:
        if not is_do_not_contact(uid):
            add_do_not_contact(uid, uname, reason)
            n += 1
    return n


# ---------------------------------------------------------------- sending

def claim_for_sending(contact_id) -> bool:
    """Atomically approved -> sending. Only ONE caller can ever win this for a
    given contact; everyone else gets False and must not send."""
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            if c.execute("SELECT 1 FROM do_not_contact dnc JOIN contacts ct ON ct.x_user_id = dnc.x_user_id "
                         "WHERE ct.id = ?", (contact_id,)).fetchone():
                _transition(c, contact_id, "approved", "rejected")
                _log(c, contact_id, "send_blocked", "on do-not-contact list")
                c.execute("COMMIT")
                return False
            ok = _transition(c, contact_id, "approved", "sending")
            if ok:
                _log(c, contact_id, "send_started")
            c.execute("COMMIT")
            return ok
        except Exception:
            c.execute("ROLLBACK")
            raise


def mark_sent(contact_id, dm_event_id, dm_conversation_id) -> None:
    with connect() as c:
        _transition(c, contact_id, "sending", "sent", dm_event_id=dm_event_id,
                    dm_conversation_id=dm_conversation_id, sent_at=now(), last_error=None)
        _log(c, contact_id, "sent", {"dm_event_id": dm_event_id})


def mark_already_messaged(contact_id, detail) -> None:
    """Pre-send check found an existing conversation (e.g. someone messaged them
    by hand). Treat as sent-by-someone-else: do not send."""
    with connect() as c:
        _transition(c, contact_id, "sending", "needs_review",
                    last_error="Existing DM conversation found - not sent to avoid double-messaging.")
        _log(c, contact_id, "send_skipped_existing_conversation", detail)


def mark_send_failed(contact_id, kind, message) -> None:
    """kind: 'dm_closed' (recipient doesn't accept DMs - final) or
             'failed' (other error - a human may retry by re-approving) or
             'needs_review' (unknown outcome, e.g. timeout)."""
    assert kind in ("dm_closed", "failed", "needs_review")
    with connect() as c:
        _transition(c, contact_id, "sending", kind, last_error=message)
        _log(c, contact_id, f"send_{kind}", message)


def release_claim(contact_id, reason) -> None:
    """Send was not attempted (e.g. rate limit / daily cap hit before calling X):
    put it back in the approved queue."""
    with connect() as c:
        _transition(c, contact_id, "sending", "approved", last_error=reason)
        _log(c, contact_id, "send_deferred", reason)


def sent_today() -> int:
    with connect() as c:
        return c.execute(
            "SELECT COUNT(*) FROM events WHERE type='sent' AND created_at LIKE ?",
            (today_prefix() + "%",),
        ).fetchone()[0]


# ---------------------------------------------------------------- replies

def mark_replied(contact_id, reply_text) -> bool:
    with connect() as c:
        ok = _transition(c, contact_id, ("sent", "needs_review"), "replied",
                         reply_text=reply_text, replied_at=now())
        if ok:
            _log(c, contact_id, "replied", reply_text)
        else:
            # Already replied before: just keep the latest text.
            c.execute("UPDATE contacts SET reply_text=?, updated_at=? WHERE id=? AND status='replied'",
                      (reply_text, now(), contact_id))
        return ok


# ---------------------------------------------------------------- events / cost

def recent_events(limit=100, contact_id=None):
    with connect() as c:
        if contact_id:
            rows = c.execute(
                "SELECT e.*, ct.username FROM events e LEFT JOIN contacts ct ON ct.id=e.contact_id "
                "WHERE e.contact_id=? ORDER BY e.id DESC LIMIT ?", (contact_id, limit)).fetchall()
        else:
            rows = c.execute(
                "SELECT e.*, ct.username FROM events e LEFT JOIN contacts ct ON ct.id=e.contact_id "
                "ORDER BY e.id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def log_usage(endpoint, units=1, cost_usd=0.0, dry_run=False) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO api_usage (endpoint, units, cost_usd, dry_run, created_at) VALUES (?,?,?,?,?)",
            (endpoint, units, cost_usd, 1 if dry_run else 0, now()),
        )


def cost_summary() -> dict:
    with connect() as c:
        today = c.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM api_usage WHERE dry_run=0 AND created_at LIKE ?",
            (today_prefix() + "%",)).fetchone()[0]
        total = c.execute("SELECT COALESCE(SUM(cost_usd),0) FROM api_usage WHERE dry_run=0").fetchone()[0]
        by_ep = c.execute(
            "SELECT endpoint, SUM(units) units, ROUND(SUM(cost_usd),4) cost FROM api_usage "
            "WHERE dry_run=0 GROUP BY endpoint ORDER BY cost DESC").fetchall()
    return {"today": round(today, 4), "total": round(total, 4), "by_endpoint": [dict(r) for r in by_ep]}


# ---------------------------------------------------------------- auth tokens (app-level)

def save_tokens(access_token, refresh_token, expires_in, user_id=None, username=None) -> None:
    with connect_app() as c:
        existing = c.execute("SELECT user_id, username FROM auth WHERE id=1").fetchone()
        if existing and user_id is None:
            user_id, username = existing["user_id"], existing["username"]
        c.execute(
            """INSERT INTO auth (id, access_token, refresh_token, expires_at, user_id, username)
               VALUES (1,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET access_token=excluded.access_token,
                 refresh_token=excluded.refresh_token, expires_at=excluded.expires_at,
                 user_id=excluded.user_id, username=excluded.username""",
            (access_token, refresh_token, time.time() + int(expires_in or 7200) - 60, user_id, username),
        )


def get_tokens():
    with connect_app() as c:
        r = c.execute("SELECT * FROM auth WHERE id=1").fetchone()
    return dict(r) if r else None


def clear_tokens() -> None:
    with connect_app() as c:
        c.execute("DELETE FROM auth")
        c.execute("DELETE FROM app_meta WHERE key='granted_scopes'")


def new_billable(kind, ids) -> int:
    """How many of these resources X has NOT yet billed today (and remember them)."""
    day = today_prefix()
    n = 0
    with connect_app() as c:
        for rid in ids:
            n += c.execute("INSERT OR IGNORE INTO billed (kind, rid, day) VALUES (?,?,?)",
                           (kind, str(rid), day)).rowcount
        c.execute("DELETE FROM billed WHERE day < ?", (day,))
    return n


def get_app_meta(key, default=None):
    with connect_app() as c:
        r = c.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_app_meta(key, value) -> None:
    with connect_app() as c:
        c.execute("INSERT INTO app_meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, str(value)))


# ---------------------------------------------------------------- per-account settings

def get_meta(key, default=None):
    with connect() as c:
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_meta(key, value) -> None:
    with connect() as c:
        c.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, str(value)))
