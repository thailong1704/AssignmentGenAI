"""
pipeline.py - find people, decide if they fit, draft a DM grounded in real posts.

  discover_from_seeds / discover_from_search
        -> new rows in contacts (dedup + do-not-contact handled by db)
  free bio pre-filter
        -> people with no AI/robotics signal in their bio are skipped BEFORE we
           pay to read their posts (can be analysed manually anyway)
  analyse(contact)
        -> read recent posts -> ONE LLM call (score + reason + evidence + draft)
        -> code-level checks: evidence IDs must be real posts we fetched,
           banned "fake familiarity" phrases flagged; no evidence => 'generic'

The LLM never sees anything we didn't fetch from X, and its claims about
evidence are verified by code, not trusted.
"""
import json
import os
import re

import requests

import db
import x_api

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
# USD per 1M tokens - adjust if you change model (see openai.com/api/pricing)
PRICE_IN = float(os.environ.get("OPENAI_PRICE_IN", "0.40"))
PRICE_OUT = float(os.environ.get("OPENAI_PRICE_OUT", "1.60"))

MIN_SCORE = int(os.environ.get("MIN_FIT_SCORE", "6"))
POSTS_PER_PERSON = int(os.environ.get("POSTS_PER_PERSON", "15"))
SENDER_CONTEXT = os.environ.get(
    "SENDER_CONTEXT",
    "GenAI Fund builds AI ecosystems in Southeast Asia: we back and connect AI and "
    "robotics founders, builders and researchers in the region.",
)

CATEGORIES = ["founder", "researcher", "builder", "devtools", "infrastructure",
              "robotics", "embodied_ai", "not_a_fit"]

AI_SIGNAL = re.compile(
    r"\b(ai|a\.i\.|ml|llms?|gpt|genai|machine learning|deep learning|neural|"
    r"robot\w*|embodied|autonomous|agents?|computer vision|cv|nlp|transformers?|"
    r"diffusion|rl|reinforcement|model|models|inference|gpu|mlops|data scien\w*|"
    r"phd|research\w*|founder|co-founder|cto|startup|open[- ]source|eval\w*)\b",
    re.I,
)

FAKE_FAMILIARITY = [
    r"\bwe met\b", r"\bgreat (to|meeting) (meet|you)\b", r"\bas (we )?discussed\b",
    r"\blong[- ]time (fan|follower)\b", r"\bbig fan\b", r"\bfollowed your work for years\b",
    r"\bremember (you|our)\b", r"\bour (last )?(call|chat|conversation)\b",
    r"\bfriend of (mine|ours)\b", r"\breferred (me|us)\b",
]


# ------------------------------------------------------------------ discovery

def has_ai_signal(bio: str) -> bool:
    return bool(AI_SIGNAL.search(bio or ""))


def _add(users, source):
    known = db.known_user_ids()
    me = x_api.connected_account()
    new_ids = []
    for u in users:
        if not u or u["id"] in known or (me and u["id"] == me[0]):
            continue
        cid = db.add_candidate(u["id"], u["username"], u.get("name"), u.get("bio"),
                               u.get("followers"), source)
        if cid:
            known.add(u["id"])
            new_ids.append(cid)
    return new_ids


def discover_from_seeds(handles, max_per_seed=50):
    """People that seed accounts follow. Returns (new_contact_ids, errors)."""
    new, errors = [], []
    for h in handles:
        h = h.strip().lstrip("@")
        if not h:
            continue
        try:
            seed = x_api.get_user_by_username(h)
            users = x_api.get_following(seed["id"], max_per_seed)
            new += _add(users, f"seed:@{h}")
        except x_api.XError as e:
            errors.append(f"@{h}: {e.human()}")
            if e.kind in ("auth", "credits", "rate_limited"):
                break
    return new, errors


def discover_from_search(query, max_posts=50):
    """Authors of recent (7-day) posts matching a segment query."""
    try:
        hits = x_api.search_recent(query, max_posts)
    except x_api.XError as e:
        return [], [e.human()]
    return _add([h["author"] for h in hits], f"search:{query}"), []


# ------------------------------------------------------------------ LLM

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "fit_score": {"type": "integer", "description": "0-10 relevance to the target segment"},
        "category": {"type": "string", "enum": CATEGORIES},
        "reason": {"type": "string", "description": "1-2 sentences, why they fit or not, based only on the given bio/posts"},
        "evidence_post_ids": {"type": "array", "items": {"type": "string"},
                              "description": "IDs of the posts that support the reason and the DM (1-3)"},
        "dm": {"type": "string", "description": "The draft DM, empty if fit_score < threshold"},
    },
    "required": ["fit_score", "category", "reason", "evidence_post_ids", "dm"],
}

SYSTEM = """You help a Southeast Asian AI ecosystem fund find and message relevant people on X.

Target segment: AI and robotics builders, founders and researchers - people working on AI models,
applications, developer tools, infrastructure, robotics or embodied AI. Relevance and a real reason
to talk matter more than follower count. Investors, marketers, crypto/memecoin accounts, and generic
"AI news" curators are NOT a fit.

Rules for the DM:
- Use ONLY facts present in the provided bio and posts. Never invent shared history, meetings,
  mutual friends, or that we've "followed their work for years".
- Reference ONE specific thing they actually posted (cite its id in evidence_post_ids).
- Say briefly who we are, and make one low-pressure ask (e.g. a short chat, or whether they'd like
  intros to others building similar things in SEA). No links, no hashtags, no emojis, no hype.
- Plain, warm, peer-to-peer tone. 350 characters max. Sign off with the sender name if provided.
- If there is no specific post worth referencing, return evidence_post_ids = [] and still write a
  short honest DM based on the bio only.
If fit_score < {min_score}, return dm = "".
"""


def _llm(bio_block: str, posts: list, extra_context: str):
    posts_block = "\n".join(f"[id={p['id']}] ({(p.get('created_at') or '')[:10]}) {p['text']}" for p in posts) \
        or "(no recent original posts)"
    user = (f"About us: {SENDER_CONTEXT}\n"
            f"{('Campaign focus: ' + extra_context) if extra_context else ''}\n"
            f"Sender name: {os.environ.get('SENDER_NAME', '')}\n\n"
            f"PERSON\n{bio_block}\n\nRECENT POSTS\n{posts_block}")

    if not OPENAI_API_KEY or x_api.is_dry_run() and os.environ.get("LLM_DRY_RUN", "1") == "1":
        return _fake_llm(bio_block, posts)

    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        json={
            "model": OPENAI_MODEL,
            "temperature": 0.4,
            "messages": [{"role": "system", "content": SYSTEM.replace("{min_score}", str(MIN_SCORE))},
                         {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "assessment", "strict": True, "schema": SCHEMA}},
        },
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI error [{r.status_code}]: {r.text[:300]}")
    body = r.json()
    usage = body.get("usage", {})
    cost = usage.get("prompt_tokens", 0) / 1e6 * PRICE_IN + usage.get("completion_tokens", 0) / 1e6 * PRICE_OUT
    db.log_usage(f"openai:{OPENAI_MODEL}", usage.get("total_tokens", 0), round(cost, 5))
    msg = body["choices"][0]["message"]
    if msg.get("refusal"):
        raise RuntimeError(f"Model refused: {msg['refusal']}")
    return json.loads(msg["content"])


def _fake_llm(bio_block, posts):
    """Deterministic stand-in for dry-run / no API key: keyword scoring + template."""
    text = (bio_block + " " + " ".join(p["text"] for p in posts)).lower()
    strong = ["robot", "embodied", "llm", "model", "eval", "gpu", "inference", "agent", "sim2real", "locomotion", "tokenizer", "founder", "paper"]
    score = min(10, 2 + 2 * sum(w in text for w in strong))
    if any(w in text for w in ["memecoin", "coin", "khao soi"]):
        score = 1
    if score < MIN_SCORE:
        return {"fit_score": score, "category": "not_a_fit", "reason": "No sign of building in AI/robotics.",
                "evidence_post_ids": [], "dm": ""}
    if not posts:
        return {"fit_score": score, "category": "builder", "reason": "AI-related bio but no recent posts to go on.",
                "evidence_post_ids": [], "dm": "Hi - I'm with GenAI Fund; we connect AI builders across Southeast Asia. "
                                                "Would you be open to a short chat about what you're working on?"}
    p = posts[0]
    snippet = p["text"][:90].rsplit(" ", 1)[0]
    return {"fit_score": score, "category": "builder",
            "reason": f"Recent post shows hands-on AI/robotics work: \"{snippet}...\"",
            "evidence_post_ids": [p["id"]],
            "dm": f"Hi - saw your post about \"{snippet}...\". I'm with GenAI Fund; we connect people building AI "
                  f"in Southeast Asia. Would you be up for a 20-minute chat about what you're working on? [dry-run draft]"}


# ------------------------------------------------------------------ analysis

def check_draft(dm: str) -> list:
    """Code-level guardrails on the LLM's text. Returns a list of warnings."""
    w = []
    for pat in FAKE_FAMILIARITY:
        if re.search(pat, dm, re.I):
            w.append(f"possible invented familiarity: '{re.search(pat, dm, re.I).group(0)}'")
    if re.search(r"https?://", dm):
        w.append("contains a link")
    if len(dm) > 600:
        w.append(f"long ({len(dm)} chars)")
    return w


def analyse(contact_id, extra_context="") -> dict:
    """Score + draft one contact. Returns a small result dict for the UI."""
    c = db.get_contact(contact_id)
    if not c or c["status"] not in ("found", "drafted"):
        return {"id": contact_id, "skipped": "not in an analysable state"}
    try:
        posts = x_api.get_user_tweets(c["x_user_id"], POSTS_PER_PERSON)
    except x_api.XError as e:
        db.log_event(contact_id, "analyse_failed", e.human())
        return {"id": contact_id, "error": e.human(), "fatal": e.kind in ("auth", "credits", "rate_limited")}

    bio_block = (f"@{c['username']} ({c.get('name') or ''})\nBio: {c.get('bio') or ''}\n"
                 f"Followers: {c.get('followers')}")
    try:
        out = _llm(bio_block, posts, extra_context)
    except Exception as e:  # noqa: BLE001 - surface any LLM failure to the UI
        db.log_event(contact_id, "analyse_failed", str(e))
        return {"id": contact_id, "error": str(e)}

    # ---- verify evidence: only IDs of posts we actually fetched survive
    by_id = {p["id"]: p for p in posts}
    claimed = [str(i) for i in out.get("evidence_post_ids", [])]
    verified = [i for i in claimed if i in by_id]
    dropped = [i for i in claimed if i not in by_id]
    evidence = [{"tweet_id": i, "text": by_id[i]["text"],
                 "url": x_api.tweet_url(c["username"], i)} for i in verified]

    score = max(0, min(10, int(out.get("fit_score", 0))))
    reason = out.get("reason", "").strip()
    dm = (out.get("dm") or "").strip()

    if score < MIN_SCORE or out.get("category") == "not_a_fit" or not dm:
        db.save_draft(contact_id, score, out.get("category"), reason, evidence, "", True)
        db.reject(contact_id, f"auto: low fit (score {score}) - {reason}")
        return {"id": contact_id, "score": score, "low_fit": True}

    warnings = check_draft(dm)
    if dropped:
        warnings.append(f"LLM cited post IDs that don't exist: {', '.join(dropped)} (removed)")
    generic = not evidence
    if generic:
        warnings.append("no verified post to reference - draft is NOT personalised")
    if warnings:
        reason = reason + "\n⚠ " + "\n⚠ ".join(warnings)

    db.save_draft(contact_id, score, out.get("category"), reason, evidence, dm, generic)
    return {"id": contact_id, "score": score, "generic": generic, "warnings": warnings}


def analyse_pending(extra_context="", include_no_signal=False, limit=25) -> dict:
    """Analyse 'found' contacts. People whose bio has no AI/robotics signal are
    skipped (free) unless include_no_signal - this is the main cost saver."""
    results, skipped = [], 0
    for c in db.list_contacts("found", order="id ASC", limit=500):
        if len(results) >= limit:
            break
        if not include_no_signal and not has_ai_signal(c.get("bio")):
            db.reject(c["id"], "auto: bio has no AI/robotics signal (not analysed, saved API cost)")
            skipped += 1
            continue
        r = analyse(c["id"], extra_context)
        results.append(r)
        if r.get("fatal"):
            break
    return {"results": results, "skipped_no_signal": skipped}
