import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from pathlib import Path

import requests

import db

# ------------------------------------------------------------------ config

CLIENT_ID = os.environ.get("X_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("X_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("X_REDIRECT_URI", "http://127.0.0.1:8000/callback")
SCOPES = "tweet.read users.read follows.read dm.read dm.write offline.access"

AUTH_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
API = "https://api.x.com/2"

FIXTURES_PATH = Path(__file__).with_name("fixtures.json")

USER_FIELDS = "description,public_metrics,location,url,verified,created_at"
TWEET_FIELDS = "created_at,public_metrics,lang,author_id"
DM_FIELDS = "created_at,sender_id,text,dm_conversation_id,event_type"

# Estimated pay-per-use prices (USD). Check docs.x.com/x-api/getting-started/pricing
# and adjust here if X changes them - everything else reads from this table.
COST = {
    "post_read": 0.005,     # per post returned
    "user_read": 0.010,     # per user returned (lookup, following list)
    "dm_event_read": 0.010, # per DM event returned
    "dm_send": 0.015,       # per DM sent
    "owned_read": 0.001,    # reading the CONNECTED account's own posts / following ("Owned Reads")
}


def is_dry_run() -> bool:
    v = os.environ.get("DRY_RUN")
    if v is None:
        return not CLIENT_ID          # no credentials -> safe default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ------------------------------------------------------------------ errors

class XError(Exception):
    """kind is one of:
       auth          - not connected / token revoked -> reconnect
       credits       - out of API credits -> top up in developer console
       rate_limited  - too many requests; .reset_at = unix time it clears
       dm_closed     - recipient doesn't accept DMs from this account (final)
       not_found     - user doesn't exist / suspended
       forbidden     - other permission problem
       network       - timeout / connection error (outcome may be unknown)
       other
    """
    def __init__(self, kind, message, status=None, reset_at=None, raw=None):
        super().__init__(message)
        self.kind, self.status, self.reset_at, self.raw = kind, status, reset_at, raw

    def human(self) -> str:
        msgs = {
            "auth": "X account not connected or the login expired - click 'Connect X'.",
            "credits": "X API credits are used up - top up in the X developer console.",
            "rate_limited": "X rate limit reached" + (
                f" - try again after {time.strftime('%H:%M', time.localtime(self.reset_at))}."
                if self.reset_at else "."),
            "dm_closed": "This person doesn't accept DMs from your account.",
            "not_found": "User not found or suspended.",
            "forbidden": "X refused this action (permissions)",
            "network": "Network problem talking to X.",
        }
        base = msgs.get(self.kind, str(self))
        if self.kind in ("forbidden", "other", "not_found") and str(self) and str(self) not in base:
            base += f" - X said: {self}"
        return base


def _classify(resp) -> XError:
    try:
        body = resp.json()
    except ValueError:
        body = {"detail": resp.text[:300]}
    detail = body.get("detail") or body.get("title") or json.dumps(body)[:300]
    ptype = str(body.get("type", ""))
    errs = body.get("errors") or []
    if errs and not ptype:
        ptype = str(errs[0].get("type", ""))
        detail = errs[0].get("detail") or errs[0].get("message") or detail
    s = resp.status_code
    if s == 401:
        return XError("auth", detail, s, raw=body)
    if s == 402 or "credits" in ptype:
        return XError("credits", detail, s, raw=body)
    if s == 429:
        reset = resp.headers.get("x-rate-limit-reset")
        return XError("rate_limited", detail, s, reset_at=int(reset) if reset else None, raw=body)
    if "dm-recipient-not-accepting" in ptype or "permission to DM" in detail:
        return XError("dm_closed", detail, s, raw=body)
    if s == 404 or "resource-not-found" in ptype:
        return XError("not_found", detail, s, raw=body)
    if s == 403:
        return XError("forbidden", detail, s, raw=body)
    return XError("other", f"[{s}] {detail}", s, raw=body)


# ------------------------------------------------------------------ OAuth

def authorize_url() -> str:
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    db.set_app_meta("oauth_verifier", verifier)
    db.set_app_meta("oauth_state", state)
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI,
        "scope": SCOPES, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })


def _token_request(data) -> dict:
    data = {**data, "client_id": CLIENT_ID}
    auth = (CLIENT_ID, CLIENT_SECRET) if CLIENT_SECRET else None
    try:
        r = requests.post(TOKEN_URL, data=data, auth=auth, timeout=30)
    except requests.RequestException as e:
        raise XError("network", str(e))
    if r.status_code != 200:
        err = _classify(r)
        err.kind = "auth" if err.kind in ("other", "forbidden") else err.kind
        raise err
    return r.json()


def handle_callback(code, state) -> dict:
    if not state or state != db.get_app_meta("oauth_state"):
        raise XError("auth", "OAuth state mismatch - please try connecting again.")
    tok = _token_request({
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT_URI, "code_verifier": db.get_app_meta("oauth_verifier"),
    })
    db.save_tokens(tok["access_token"], tok.get("refresh_token"), tok.get("expires_in"))
    db.set_app_meta("granted_scopes", tok.get("scope", ""))
    me = get_me()
    db.save_tokens(tok["access_token"], tok.get("refresh_token"), tok.get("expires_in"),
                   user_id=me["id"], username=me["username"])
    db.set_app_meta("oauth_state", "")
    return me


def _access_token() -> str:
    t = db.get_tokens()
    if not t or not t.get("access_token"):
        raise XError("auth", "Not connected.")
    if time.time() < (t.get("expires_at") or 0):
        return t["access_token"]
    if not t.get("refresh_token"):
        raise XError("auth", "Session expired.")
    tok = _token_request({"grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
    db.save_tokens(tok["access_token"], tok.get("refresh_token", t["refresh_token"]), tok.get("expires_in"))
    return tok["access_token"]


def missing_scopes():
    """Scopes we need that the stored token doesn't have (e.g. after an upgrade)."""
    granted = set((db.get_app_meta("granted_scopes") or "").split())
    if is_dry_run() or not granted:
        return []
    return [x for x in SCOPES.split() if x not in granted]


def connected_account():
    """(user_id, username) of the connected sender, or None."""
    if is_dry_run():
        f = _fixtures()["me"]
        return f["id"], f["username"]
    t = db.get_tokens()
    if t and t.get("user_id"):
        return t["user_id"], t["username"]
    return None


def disconnect() -> None:
    db.clear_tokens()


# ------------------------------------------------------------------ HTTP core

RESOURCE_KIND = {"post_read": "post", "user_read": "user", "dm_event_read": "dm_event"}
OWNED_READ_PATHS = ("/tweets", "/following", "/followers", "/mentions", "/liked_tweets")


def _is_owned_read(path) -> bool:
    """X bills reads of the connected account's own data at the 'Owned Reads' rate."""
    acct = connected_account()
    return bool(acct) and path.startswith(f"/users/{acct[0]}/") and path.endswith(OWNED_READ_PATHS)


def _bill(path, cost_key, ids):
    """Log what X will actually charge. X deduplicates reads of the same resource
    within a UTC day, so only resources not already billed today are counted."""
    if cost_key == "dm_send":
        db.log_usage("/dm_conversations/with/messages", 1, COST["dm_send"])
        return
    new = db.new_billable(RESOURCE_KIND.get(cost_key, cost_key), ids)
    if not new:
        return
    rate = COST["owned_read"] if _is_owned_read(path) else COST[cost_key]
    db.log_usage(path.split("?")[0], new, round(new * rate, 4))


def _request(method, path, cost_key=None, ids_fn=None, **kw):
    """ids_fn(body) -> IDs of the billable resources returned (for daily dedup)."""
    token = _access_token()
    try:
        r = requests.request(method, API + path, headers={"Authorization": f"Bearer {token}"},
                             timeout=30, **kw)
    except requests.Timeout as e:
        raise XError("network", f"Timeout: {e}")
    except requests.RequestException as e:
        raise XError("network", str(e))
    if r.status_code == 401:            # token revoked early: one refresh + retry
        t = db.get_tokens()
        if t:
            db.save_tokens(t["access_token"], t["refresh_token"], 0)
        token = _access_token()
        r = requests.request(method, API + path, headers={"Authorization": f"Bearer {token}"},
                             timeout=30, **kw)
    if r.status_code >= 400:
        raise _classify(r)
    body = r.json() if r.content else {}
    if cost_key:
        _bill(path, cost_key, ids_fn(body) if ids_fn else [])
    return body


def _ids(body):
    d = body.get("data")
    if isinstance(d, list):
        return [str(x.get("id")) for x in d if isinstance(x, dict) and x.get("id")]
    return [str(d["id"])] if isinstance(d, dict) and d.get("id") else []


def _norm_user(u) -> dict:
    pm = u.get("public_metrics") or {}
    return {
        "id": str(u["id"]), "username": u.get("username"), "name": u.get("name"),
        "bio": u.get("description", ""), "location": u.get("location"),
        "followers": pm.get("followers_count"), "url": u.get("url"),
    }


def _norm_tweet(t) -> dict:
    return {"id": str(t["id"]), "text": t.get("text", ""), "created_at": t.get("created_at"),
            "author_id": str(t.get("author_id", "")), "metrics": t.get("public_metrics") or {}}


def tweet_url(username, tweet_id) -> str:
    return f"https://x.com/{username}/status/{tweet_id}"


# ------------------------------------------------------------------ public API

def get_me() -> dict:
    if is_dry_run():
        return _fixtures()["me"]
    body = _request("GET", "/users/me", params={"user.fields": USER_FIELDS})
    return _norm_user(body["data"])


def get_user_by_username(username) -> dict:
    username = username.strip().lstrip("@")
    if is_dry_run():
        u = _fx_user_by_name(username)
        _fx_cost("/users/by/username", 1, "user_read")
        return u
    body = _request("GET", f"/users/by/username/{username}", "user_read", _ids,
                    params={"user.fields": USER_FIELDS})
    if "data" not in body:
        raise XError("not_found", f"@{username} not found")
    return _norm_user(body["data"])


def get_following(user_id, max_users=100) -> list:
    """People a seed account follows (paginates up to max_users)."""
    if is_dry_run():
        ids = _fixtures()["following"].get(str(user_id), [])[:max_users]
        users = [_fx_user(i) for i in ids]
        _fx_cost("/users/following", len(users), "user_read")
        return users
    out, token = [], None
    while len(out) < max_users:
        params = {"max_results": min(1000, max(1, max_users - len(out))), "user.fields": USER_FIELDS}
        if token:
            params["pagination_token"] = token
        body = _request("GET", f"/users/{user_id}/following", "user_read", _ids, params=params)
        out += [_norm_user(u) for u in body.get("data", [])]
        token = body.get("meta", {}).get("next_token")
        if not token:
            break
    return out[:max_users]


def search_recent(query, max_posts=50) -> list:
    """Recent (7-day) search. Returns [{tweet, author}] - authors are candidates."""
    q = f"({query}) -is:retweet"
    if is_dry_run():
        words = [w.lower() for w in query.replace('"', "").split() if w.upper() not in ("OR", "AND")]
        res = []
        for uid, tweets in _fixtures()["tweets"].items():
            for t in tweets:
                if any(w in t["text"].lower() for w in words):
                    res.append({"tweet": {**t, "author_id": uid}, "author": _fx_user(uid)})
        res = res[:max_posts]
        _fx_cost("/tweets/search/recent", len(res), "post_read")
        return res
    body = _request("GET", "/tweets/search/recent", "post_read", _ids, params={
        "query": q, "max_results": max(10, min(100, max_posts)),
        "tweet.fields": TWEET_FIELDS, "expansions": "author_id", "user.fields": USER_FIELDS,
    })
    users = {u["id"]: _norm_user(u) for u in body.get("includes", {}).get("users", [])}
    return [{"tweet": _norm_tweet(t), "author": users.get(t["author_id"])}
            for t in body.get("data", []) if t.get("author_id") in users]


def get_user_tweets(user_id, max_posts=15) -> list:
    """Recent original posts (no retweets/replies) - the evidence for personalisation."""
    if is_dry_run():
        tw = [{**t, "author_id": str(user_id)} for t in _fixtures()["tweets"].get(str(user_id), [])][:max_posts]
        _fx_cost("/users/tweets", len(tw), "post_read")
        return tw
    body = _request("GET", f"/users/{user_id}/tweets", "post_read", _ids, params={
        "max_results": max(5, min(100, max_posts)), "exclude": "retweets,replies",
        "tweet.fields": TWEET_FIELDS,
    })
    return [_norm_tweet(t) for t in body.get("data", [])][:max_posts]


def get_conversation_events(user_id, max_events=10) -> list:
    """DM history between the connected account and user_id (pre-send duplicate check)."""
    if is_dry_run():
        ev = _fixtures()["existing_conversations"].get(str(user_id), [])
        _fx_cost("/dm_conversations/with/dm_events", len(ev), "dm_event_read")
        return ev
    body = _request("GET", f"/dm_conversations/with/{user_id}/dm_events", "dm_event_read", _ids,
                    params={"max_results": max(1, min(100, max_events)), "dm_event.fields": DM_FIELDS,
                            "event_types": "MessageCreate"})
    return body.get("data", [])


def send_dm(user_id, text) -> dict:
    """Returns {dm_event_id, dm_conversation_id}. Raises XError (dm_closed etc.)."""
    if is_dry_run():
        if str(user_id) in _fixtures()["dm_closed"]:
            raise XError("dm_closed", "You do not have permission to DM one or more participants.", 403)
        _fx_cost("/dm_conversations/with/messages", 1, "dm_send")
        me = _fixtures()["me"]["id"]
        return {"dm_event_id": f"dry-{secrets.token_hex(6)}", "dm_conversation_id": f"{user_id}-{me}"}
    body = _request("POST", f"/dm_conversations/with/{user_id}/messages", "dm_send",
                    json={"text": text})
    d = body.get("data", {})
    return {"dm_event_id": d.get("dm_event_id"), "dm_conversation_id": d.get("dm_conversation_id")}


def get_recent_dm_events(max_events=100) -> list:
    """Recent DM events across all conversations (used to detect replies).
    Each: {id, text, sender_id, dm_conversation_id, created_at}."""
    if is_dry_run():
        me = _fixtures()["me"]["id"]
        sent_ids = {c["x_user_id"] for c in db.list_contacts(("sent", "replied", "needs_review"))}
        ev = [{**e, "dm_conversation_id": f"{e['sender_id']}-{me}"}
              for e in _fixtures()["replies"] if e["sender_id"] in sent_ids]
        _fx_cost("/dm_events", len(ev), "dm_event_read")
        return ev
    body = _request("GET", "/dm_events", "dm_event_read", _ids, params={
        "max_results": max(1, min(100, max_events)), "dm_event.fields": DM_FIELDS,
        "event_types": "MessageCreate",
    })
    return body.get("data", [])


# ------------------------------------------------------------------ encrypted X Chat (metadata only)
# X's newer end-to-end encrypted Chat isn't visible through the classic DM endpoints.
# The Chat API returns events whose CONTENT is ciphertext, but sender_id / created_at /
# id are plain. That's enough to know THAT someone replied (not what they said) -
# we never try to decrypt anything.

def get_chat_conversation_ids() -> dict:
    """{participant_user_id: conversation_id} for the connected account's chats."""
    if is_dry_run():
        return {}
    out, token = {}, None
    for _ in range(5):                      # up to 5 pages
        params = {"max_results": 100}
        if token:
            params["pagination_token"] = token
        body = _request("GET", "/chat/conversations", params=params)
        for conv in body.get("data", []):
            for pid in conv.get("participant_ids", []):
                out[str(pid)] = conv["id"]
        token = body.get("meta", {}).get("next_token")
        if not token or not body.get("meta", {}).get("has_more"):
            break
    return out


def get_chat_events(conversation_id, max_events=10) -> list:
    """[{id, sender_id, created_at}] - newest first. Content stays encrypted/unread."""
    if is_dry_run():
        return _fixtures().get("chat_events", {}).get(str(conversation_id), [])
    body = _request("GET", f"/chat/conversations/{conversation_id}/events", "dm_event_read", _ids,
                    params={"max_results": max(1, min(100, max_events))})
    return [{"id": str(e.get("id")), "sender_id": str(e.get("sender_id") or ""),
             "created_at": e.get("created_at")} for e in body.get("data", [])]


# ------------------------------------------------------------------ dry-run fixtures

_fx_cache = None


def _fixtures() -> dict:
    global _fx_cache
    if _fx_cache is None:
        _fx_cache = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    return _fx_cache


def _fx_user(uid) -> dict:
    u = _fixtures()["users"][str(uid)]
    return {"id": str(uid), **u}


def _fx_user_by_name(username) -> dict:
    for uid, u in _fixtures()["users"].items():
        if u["username"].lower() == username.lower():
            return {"id": uid, **u}
    raise XError("not_found", f"@{username} not found (dry-run fixtures)")


def _fx_cost(endpoint, n, key):
    if n:
        db.log_usage(endpoint, n, round(n * COST[key], 4), dry_run=True)
