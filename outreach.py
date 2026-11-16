"""
outreach.py - send approved DMs safely, and pick up replies.

Before EVERY send:
  1. db.claim_for_sending()  - atomic approved -> sending (double-click / two tabs safe),
                               also re-checks the do-not-contact list
  2. daily cap               - stop when DAILY_SEND_CAP DMs went out today
  3. X conversation check    - if ANY DM already exists with this person (e.g. someone
                               messaged them by hand), don't send; park for review
Then send, pause a randomised interval, next.

Outcomes are always recorded:
  sent | dm_closed (they don't accept DMs - never retried) | failed (human can re-approve)
  | needs_review (unknown outcome, e.g. timeout - never auto-retried)
  rate limit / credits / auth -> nothing was sent, contact goes back to 'approved', run stops.
"""
import os
import random
import time

import db
import x_api

DAILY_SEND_CAP = int(os.environ.get("DAILY_SEND_CAP", "20"))
MIN_DELAY = float(os.environ.get("SEND_DELAY_MIN", "30"))
MAX_DELAY = float(os.environ.get("SEND_DELAY_MAX", "90"))

STOP_KINDS = ("rate_limited", "credits", "auth")
ENCRYPTED_REPLY = "Replied in X's encrypted chat - the content can't be read by the API. Open the conversation on X to read it."


def remaining_today() -> int:
    return max(0, DAILY_SEND_CAP - db.sent_today())


def _sleep_between():
    if x_api.is_dry_run():
        time.sleep(0.3)
    else:
        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def send_one(contact_id) -> dict:
    """Try to send one approved contact. Returns {'outcome': ..., 'stop': bool, 'message': ...}."""
    if remaining_today() <= 0:
        return {"outcome": "cap", "stop": True, "message": f"Daily cap of {DAILY_SEND_CAP} reached."}
    if not db.claim_for_sending(contact_id):
        return {"outcome": "skipped", "stop": False, "message": "No longer approved (already handled or blocked)."}

    c = db.get_contact(contact_id)

    # --- check X for an existing conversation (covers messages sent outside this tool)
    try:
        history = x_api.get_conversation_events(c["x_user_id"], max_events=5)
    except x_api.XError as e:
        if e.kind in STOP_KINDS:
            db.release_claim(contact_id, e.human())
            return {"outcome": e.kind, "stop": True, "message": e.human()}
        if e.kind == "dm_closed":
            db.mark_send_failed(contact_id, "dm_closed", e.human())
            return {"outcome": "dm_closed", "stop": False, "message": e.human()}
        db.release_claim(contact_id, f"Pre-send check failed: {e.human()}")
        return {"outcome": "check_failed", "stop": False, "message": e.human()}
    if history:
        db.mark_already_messaged(contact_id, {"existing_events": len(history),
                                              "last": (history[0].get("text") or "")[:120]})
        return {"outcome": "already_messaged", "stop": False,
                "message": "Existing conversation found - not sent."}

    # --- send
    try:
        res = x_api.send_dm(c["x_user_id"], c["final_text"])
    except x_api.XError as e:
        if e.kind in STOP_KINDS:
            db.release_claim(contact_id, e.human())
            return {"outcome": e.kind, "stop": True, "message": e.human()}
        if e.kind == "dm_closed":
            db.mark_send_failed(contact_id, "dm_closed", e.human())
            return {"outcome": "dm_closed", "stop": False, "message": e.human()}
        if e.kind == "network":
            db.mark_send_failed(contact_id, "needs_review",
                                f"{e.human()} The DM may or may not have been delivered - check X before retrying.")
            return {"outcome": "needs_review", "stop": True, "message": e.human()}
        db.mark_send_failed(contact_id, "failed", e.human() if e.kind != "other" else str(e))
        return {"outcome": "failed", "stop": False, "message": str(e)}

    db.mark_sent(contact_id, res.get("dm_event_id"), res.get("dm_conversation_id"))
    return {"outcome": "sent", "stop": False, "message": f"Sent to @{c['username']}"}


def send_approved(max_count=None, progress=None) -> dict:
    """Send the approved queue (oldest approval first) until done, the daily cap,
    or a stopping error. progress(msg) is called for live status."""
    say = progress or (lambda m: None)
    queue = db.list_contacts("approved", order="approved_at ASC")
    if max_count:
        queue = queue[:max_count]
    summary = {"sent": 0, "dm_closed": 0, "already_messaged": 0, "failed": 0,
               "needs_review": 0, "skipped": 0, "stopped_because": None}
    for i, c in enumerate(queue):
        r = send_one(c["id"])
        say(f"@{c['username']}: {r['message']}")
        key = r["outcome"] if r["outcome"] in summary else None
        if key:
            summary[key] += 1
        elif r["outcome"] == "check_failed":
            summary["failed"] += 1
        if r["stop"]:
            summary["stopped_because"] = r["message"]
            break
        if r["outcome"] == "sent" and i < len(queue) - 1:
            _sleep_between()
    return summary


def _is_before_our_dm(ev, c) -> bool:
    """X event IDs are snowflakes: they grow with X's own clock, so comparing IDs
    is immune to the local computer's clock being off. Fall back to timestamps."""
    ours, theirs = str(c.get("dm_event_id") or ""), str(ev.get("id") or "")
    if ours.isdigit() and theirs.isdigit():
        return int(theirs) < int(ours)
    return bool(c.get("sent_at")) and (ev.get("created_at") or "") < c["sent_at"][:19]


def sync_replies() -> dict:
    """Pull recent DM events; any message FROM a contact we messaged, received
    after we sent, marks them 'replied' (shows up in Needs attention)."""
    me = x_api.connected_account()
    if not me:
        raise x_api.XError("auth", "Not connected.")
    my_id = me[0]
    events = x_api.get_recent_dm_events(100)
    new_replies = 0
    for ev in sorted(events, key=lambda e: e.get("created_at") or ""):
        sender = str(ev.get("sender_id") or "")
        if not sender or sender == my_id:
            continue
        c = db.get_contact_by_user_id(sender)
        if not c or c["status"] not in ("sent", "needs_review", "replied"):
            continue
        if not x_api.is_dry_run() and _is_before_our_dm(ev, c):
            continue  # older message from before our outreach
        if c["status"] == "replied" and c.get("reply_text") == ev.get("text"):
            continue
        if db.mark_replied(c["id"], ev.get("text") or ""):
            new_replies += 1
    # --- encrypted X Chat: detect THAT they replied (content is end-to-end encrypted)
    chat_checked, chat_note = 0, None
    waiting = db.list_contacts(("sent", "needs_review"), order="sent_at DESC", limit=50)
    if waiting:
        try:
            conv_map = x_api.get_chat_conversation_ids()
            for c in waiting:
                conv_id = conv_map.get(c["x_user_id"]) or c.get("dm_conversation_id")
                if not conv_id:
                    continue
                chat_checked += 1
                for ev in x_api.get_chat_events(conv_id, 10):
                    if ev["sender_id"] == c["x_user_id"] and not _is_before_our_dm(ev, c):
                        if db.mark_replied(c["id"], ENCRYPTED_REPLY):
                            new_replies += 1
                        break
        except x_api.XError as e:
            if e.kind in ("auth", "credits"):
                raise
            chat_note = f"Encrypted-chat check unavailable right now ({e.human()}). Standard DMs were checked."

    db.set_meta("last_reply_sync", db.now())
    return {"checked": len(events), "chat_checked": chat_checked,
            "new_replies": new_replies, "note": chat_note}
