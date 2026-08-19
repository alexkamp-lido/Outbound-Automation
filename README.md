# Sequence Reviewer

A tiny FastAPI service that posts a daily Slack digest of outbound-sequence
activity across **Origami** (sequencer at origami.chat) and **Plusvibe** (cold
email). Purpose:

1. Show today's replies from both platforms in one place.
2. Flag prospects who replied on one platform but still have an active sequence
   on the other — you click through to stop those manually.

No automated terminations. No interactive buttons.

## Architecture

```
Origami reply happens ────► POST /webhooks/origami (HMAC-signed)
                              │
                              ▼
                       SQLite event store (${DATA_DIR}/reviewer.sqlite)
                              ▲
                              │
Origami scheduled agent (5pm ET, daily)
   │
   ▼
POST /reviewer/run?secret=<REVIEWER_SHARED_SECRET>
   │
   ▼
collect_origami_active() ── walks campaigns for still-in-sequence roster
collect_origami_replies() ── reads reply events from SQLite within lookback
collect_plusvibe() ── stub until a Plusvibe read source is wired
compute_reconciliations() ── cross-joins replies vs. still-active by email
build_digest() ── Slack Block Kit
   │
   ▼
POST SLACK_WEBHOOK_URL → digest lands in Slack
```

Replies are captured in real time via webhook so the digest sees the actual
`received_at` timestamp — a polled fallback based on `addedAt` misses everyone
added to a campaign more than the lookback window ago.

## Files

| File | Purpose |
|------|---------|
| `app.py` | FastAPI app: `POST /reviewer/run`, `POST /webhooks/origami`, `GET /health`. |
| `services/origami_client.py` | Origami v2 REST client — `discover_campaigns()`, `iter_people()`, `get_sequence()`, `stop_sequence()`. |
| `services/origami_webhook.py` | HMAC-SHA256 signature verifier (Standard Webhooks spec). |
| `services/event_store.py` | SQLite wrapper for reply events — `insert_reply_event()`, `list_recent_replies()`. |
| `services/sequence_reviewer.py` | Collectors, reconciliation join, pure `build_digest()`, `run_reviewer()`. |
| `services/slack_notifier.py` | `post_digest(blocks)` → `SLACK_WEBHOOK_URL`. |
| `tests/` | Signature verify, event store, join + digest shape. |

## Configuration

Copy `.env.example` → `.env` and fill in:

| Var | Required | Notes |
|-----|----------|-------|
| `ORIGAMI_API_KEY` | ✓ | `og_live_...` bearer token. Alone, this is enough — the reviewer will `GET /workspaces`, enumerate every workspace, then every campaign in each. New workspaces + campaigns auto-included. |
| `ORIGAMI_WORKSPACE_ID` | optional | Narrows the reviewer to one workspace. |
| `ORIGAMI_CAMPAIGN_IDS` | optional | Comma-separated. Manual pin — new campaigns must be added here by hand. |
| `SLACK_WEBHOOK_URL` | ✓ | Incoming webhook for the reviewer channel. |
| `REVIEWER_SHARED_SECRET` | recommended | Gates `/reviewer/run`. Generate: `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`. |
| `REVIEWER_LOOKBACK_HOURS` | optional | Default 36. |
| `ORIGAMI_WEBHOOK_SECRET` | ✓ | `whsec_...` from Origami's dashboard reveal modal (see "Webhook setup" below). Without it, `POST /webhooks/origami` returns 500. |
| `DATA_DIR` | ✓ (Railway) | Filesystem path for the reply-event SQLite DB. `./data` locally; `/data` on Railway with a mounted volume. |

## Local dev

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# One-shot test:
python -m pytest tests/ -v

# Live run:
cp .env.example .env         # then edit
uvicorn app:app --reload --port 8000
curl -X POST "http://localhost:8000/reviewer/run?secret=$REVIEWER_SHARED_SECRET"
```

If everything's wired, the endpoint returns `{"ok": true, "sections": {...}}`
and a digest lands in Slack.

## Deploy to Railway

1. Push this repo to GitHub.
2. In Railway → **New Project → Deploy from GitHub**, pick the repo.
3. **Variables** tab: paste every var from `.env.example` (set `DATA_DIR=/data`).
4. **Settings → Volumes** → attach a new volume, mount at `/data` (5 GB free tier
   is plenty — the DB is a few KB per reply).
5. Railway detects the Dockerfile and deploys. Note the public URL.
6. Follow **Webhook setup** below to point Origami at the deployed
   `/webhooks/origami` endpoint.
7. In Origami, create a scheduled agent (daily 5pm ET) whose only instruction
   is: `POST <railway-url>/reviewer/run?secret=<REVIEWER_SHARED_SECRET>`.
8. Trigger it once from Origami's dashboard to smoke-test.

## Webhook setup (Origami → `/webhooks/origami`)

1. Origami dashboard → **Settings → Developers → Webhooks → New endpoint**.
2. **URL** = `https://<railway-url>/webhooks/origami`.
3. **Event types** — subscribe to at least `sequence.reply.received`. (The
   receiver also accepts `webhook.test` for the dashboard's test button.)
4. Save. A reveal modal shows the `whsec_...` secret **once** — copy it and
   paste into Railway as `ORIGAMI_WEBHOOK_SECRET`. Redeploy.
5. Back in the dashboard, click **Test endpoint**. The delivery log should go
   green and Railway logs should show `[origami-webhook] test event received`.
6. From this moment forward, every reply Origami sees is captured in the
   SQLite store and picked up by the next daily digest.

Historical replies received before this point are not backfilled.

## Plusvibe (open dependency)

`collect_plusvibe()` returns empty and the digest shows a "Plusvibe read source
not connected yet" banner. The REST API doesn't currently expose an
inbox/replies-with-labels endpoint and Plusvibe's MCP isn't reachable from a
headless service. When a read source is resolved (direct REST route,
mailbox-forwarding to a Gmail Railway can poll, etc.), populate
`collect_plusvibe()` — the digest and reconciliation code are already generic.

## Listing your workspaces (for reference)

```bash
curl -H "Authorization: Bearer $ORIGAMI_API_KEY" \
  https://origami.chat/api/v2/workspaces | jq '.items[] | {id, name}'
```

You don't need to pick one — leave `ORIGAMI_WORKSPACE_ID` empty and the reviewer
scans them all. Set it only if you want to narrow the review to a single
workspace.
