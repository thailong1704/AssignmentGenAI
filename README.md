# X Outreach – GenAI Fund

A small local web app that finds AI and robotics builders on X, explains why each one fits, drafts a personalised DM that cites one of their real posts, and sends only the messages you approve, from your own X account. It tracks replies, never messages the same person twice, and shows clearly what was blocked and why.

**Try it first in dry-run.** You need no keys for that. It uses fictional accounts, sends nothing and costs nothing.

---

## Setup (about 10 minutes)

**Requirements:** Python 3.10 or newer.

```bash
git clone <this repo> && cd outreach
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # Windows: copy .env.example .env
python main.py
```

Open **http://127.0.0.1:8000**. The badge at the top right says **DRY RUN**. On the Home page, enter `demo_seed_sea` as the seed account, click **Find & analyse**, then work through Review → Send → Needs attention.

### Going live with your X account

1. Go to **console.x.com** and create a Project and App. The pay-per-use plan works; there is no monthly fee.
2. In the app's **User authentication settings**:
   - App permissions: **Read and write and Direct message**
   - Type of App: **Web App, Automated App or Bot**
   - Callback URI: `http://127.0.0.1:8000/callback`
   - Website URL: any URL you own
3. Copy the **OAuth 2.0 Client ID and Client Secret** into `.env`.
4. Add a few dollars of credit and set a **spending limit** in the console.
5. In `.env`, set `OPENAI_API_KEY`, then `DRY_RUN=0`, then restart `python main.py`.
6. Click **Connect X** and authorize with the account you'll send from.

> The **"Send approved"** button now sends real DMs. For testing, use accounts you control or people who have agreed to receive a test message.

### Settings (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `DRY_RUN` | `1` | `1` = fictional data, nothing sent or billed |
| `LLM_DRY_RUN` | `1` | In dry-run, `0` uses the real OpenAI model on the fictional data |
| `OPENAI_MODEL` | `gpt-4.1-mini` | Any chat model that supports structured outputs |
| `MIN_FIT_SCORE` | `6` | People scored below this are skipped, with the reason recorded |
| `DAILY_SEND_CAP` | `20` | Maximum DMs sent per day (UTC) |
| `SEND_DELAY_MIN` / `MAX` | `30` / `90` | Random pause in seconds between two DMs |
| `POSTS_PER_PERSON` | `15` | Number of recent posts read as evidence (also the main cost driver) |
| `SENDER_NAME`, `SENDER_CONTEXT` | | Who we are; used in drafts |

---

## How it works

```
seed accounts / topic ──► discovery ──► free bio filter ──► read recent posts ──► 1 LLM call
   (who they follow /      (dedup,       (no AI/robotics      (official API)        score, reason,
    7-day search)           do-not-       signal → skip,                             evidence IDs, draft
                            contact)      no cost)                                        │
                                                                        code checks ◄─────┘
                                                     cited post IDs must be real; flags "we met",
                                                     "big fan"… ; no evidence → marked GENERIC
                                                                              │
          human review: approve / edit / reject / do-not-contact ◄────────────┘
                                   │
   send queue ─► atomic claim ─► daily cap ─► "any DM history with this person?" ─► send ─► log
                                   │
             replies synced from DM events ─► "Needs attention"
```

| File | Role |
|---|---|
| `db.py` | SQLite store. One row per X user, guarded status changes, do-not-contact list, audit log, cost log |
| `x_api.py` | All X calls (OAuth 2.0 PKCE, token refresh, errors explained in plain language, cost per call, dry-run fixtures) |
| `pipeline.py` | Discovery, scoring and drafting with OpenAI, evidence verification |
| `outreach.py` | Safe sending and reply sync |
| `main.py` + `templates/` | Web UI |

### Data is kept per X account
- Nothing is shown until an X account is connected (in live mode only the Connect screen is reachable).
- Each connected account has its own database `data/account_<x_user_id>.db`: its own people, drafts, history, do-not-contact list and costs. Switching accounts never shows or sends another account's data. Disconnecting keeps the data, and it comes back on reconnect.
- Login tokens live separately in `data/app.db`; dry-run data lives in `data/demo.db`. The whole `data/` folder is git-ignored.
- An `outreach.db` from an older version is moved automatically to the account recorded in it.

### How double-messaging is prevented
1. The database allows **one row per X user**. People found again are ignored.
2. Sending starts with an **atomic `approved → sending` claim**. Double clicks, two browser tabs or two workers cannot both win it; this is tested with 20 concurrent threads.
3. Before every send, the tool **asks X whether any DM history already exists** with that person. If someone messaged them by hand, the tool does not send and moves them to *Needs review*.
4. If a send is interrupted (crash or timeout), the outcome is unknown, so the contact goes to **Needs review and is never resent automatically**.
5. The **do-not-contact list** is checked at discovery time and again at send time. You can import people already contacted outside the tool from a CSV.

### What it refuses to do, and what you will see
| Situation | What happens |
|---|---|
| Recipient doesn't accept DMs from you | Marked **DM closed** and never retried; shown under *Needs attention* |
| Daily cap reached | Sending stops; the rest wait in the queue |
| X rate limit (429) | Stops and shows when it resets; nothing is lost |
| Out of X credits / login expired | Stops with a plain message telling you what to do |
| LLM cited a post that doesn't exist | That citation is removed and a warning shown; with no real evidence the draft is marked **generic** |
| Draft sounds falsely familiar ("great to meet you") | Warning shown on the review card |

---

## Costs (estimates, November 2026)

X API (pay-per-use; check `docs.x.com/x-api/getting-started/pricing` and edit `COST` in `x_api.py` if prices change):
reading posts costs about $0.005 each, user reads about $0.01, DM event reads about $0.01, and each DM sent about $0.015.

| Action | Approx. cost |
|---|---|
| Read who a seed follows (50 people) | $0.50 |
| Analyse one person (15 posts + LLM) | ~$0.08 |
| Send one DM (+ pre-send history check) | ~$0.03 |
| **Campaign: 2 seeds → 100 found → 40 analysed → 20 sent** | **≈ $5** |

OpenAI `gpt-4.1-mini` costs roughly $0.001 per person. Live spend (X + OpenAI combined) is shown on the Home and Log pages. The X part follows X's billing rules: each post, user or DM event is charged **once per UTC day** (re-reading it is free), and reading the connected account's own posts or following list uses the **owned-reads rate ($0.001)**. The X developer console remains the source of truth.

## Limits / not done
- Topic search only covers the **last 7 days** (an X pay-per-use limit).
- **Encrypted X Chat:** replies sent through X's end-to-end encrypted Chat are detected from the Chat API's event metadata (sender and time); their text is ciphertext, so the tool shows "replied in encrypted chat — open on X to read" and never tries to decrypt. The pre-send "existing conversation" check uses the classic DM history; encrypted messages someone sent by hand outside the tool may not be seen there. Messages sent *by this tool* are always tracked in the database, so the tool itself never double-messages. **Mark as replied** remains as a manual fallback.
- Reply detection reads recent DM events (roughly the last 30 days). Click **Check for replies**; it does not poll in the background.
- Single user, single X account, runs locally. No hosted multi-user version.
- No automatic follow-ups. A second message to the same person is deliberately impossible in this version.

## Tests
```bash
python -m pytest -q tests      # 31 tests, all in dry-run: dedup, concurrency, caps, errors, replies (incl. encrypted chat), per-account isolation, UI click-through
```
