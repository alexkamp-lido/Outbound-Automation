# Sequence Reviewer

A FastAPI service that posts a daily Slack digest of outbound-sequence activity
across **Origami** (sequencer at origami.chat) and **Plusvibe** (cold email).
Purpose:

1. Show today's replies from both platforms in one place.
2. Flag prospects who replied on one platform but are still active on the other,
   so they can be stopped manually.

No automated terminations. No interactive buttons. Read-only surface.

For ops, troubleshooting, and architecture decisions, see
[`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Architecture

```
Origami reply happens ──► POST /webhooks/origami  (HMAC-SHA256, Standard Webhooks)
                              │
Plusvibe reply happens ──► Plusvibe posts to      │
                          #outbound-plusvibe-replies (Slack)
                              │                   │
                              ▼                   ▼
                       Slack Events API           │
                              │                   │
                              └───► POST /webhooks/plusvibe (Slack signing secret)
                                    │             │
                                    ▼             ▼
                       SQLite event store  (${DATA_DIR}/reviewer.sqlite)
                       (origami_reply_events + plusvibe_reply_events)
                                    ▲
                                    │
GitHub Actions cron (21:00 UTC daily)
   │
   ▼
POST /reviewer/run?secret=<REVIEWER_SHARED_SECRET>
   │
   ▼
collect_origami_active()   ── walks Origami campaigns for still-in-sequence roster
collect_origami_replies()  ── reads reply events from SQLite within lookback
collect_plusvibe_replies() ── reads Plusvibe reply events from SQLite
compute_reconciliations()  ── cross-joins replies vs. still-active by email
build_digest()             ── Slack Block Kit
   │
   ▼
POST SLACK_WEBHOOK_URL → digest lands in #sequence-review
```

Origami replies flow through webhooks. Plusvibe replies flow through its Slack
integration → Slack Events API → our receiver — the parser normalizes both into
the same `Reply` shape so the digest and reconciliation are platform-agnostic.

Reconciliation today fires one direction: **Plusvibe reply → still-active in
Origami**. The reverse direction awaits a Plusvibe active-roster read source.

## Files

| File | Purpose |
|------|---------|
| `app.py` | FastAPI app: `/health`, `/reviewer/run`, `/webhooks/origami`, `/webhooks/plusvibe`, `/backfill/plusvibe`. |
| `services/origami_client.py` | Origami v2 REST client — `discover_campaigns()`, `list_workspaces()`, `iter_people()`, `get_sequence()`, `get_campaign()`. |
| `services/origami_webhook.py` | HMAC-SHA256 signature verifier (Standard Webhooks). |
| `services/plusvibe_slack_webhook.py` | HMAC-SHA256 verifier for Slack Events API (`v0=<hex>` scheme). |
| `services/plusvibe_parser.py` | Parses Plusvibe's Block Kit bot messages into a normalized reply row. |
| `services/plusvibe_backfill.py` | CLI + library that pulls Slack channel history and inserts Plusvibe replies. |
| `services/event_store.py` | SQLite wrapper. Tables: `origami_reply_events`, `plusvibe_reply_events`. |
| `services/sequence_reviewer.py` | Collectors, reconciliation join, pure `build_digest()`, `run_reviewer()`. |
| `services/slack_notifier.py` | `post_digest(blocks)` → `SLACK_WEBHOOK_URL`. |
| `tests/` | Signature verify, event store, parser, join + digest shape. |
| `.github/workflows/daily-digest.yml` | GitHub Actions cron (21:00 UTC) → `POST /reviewer/run`. |

## Configuration

Copy `.env.example` → `.env` and fill in:

| Var | Required | Notes |
|-----|----------|-------|
| `ORIGAMI_API_KEY` | ✓ | `og_live_...` bearer token. Enough by itself — reviewer enumerates every workspace via `GET /workspaces`. |
| `ORIGAMI_WORKSPACE_ID` | optional | Narrows the reviewer to one workspace. Leave empty to scan all. |
| `ORIGAMI_CAMPAIGN_IDS` | optional | Comma-separated. Manual pin — new campaigns must be added by hand. Leave empty to auto-include. |
| `SLACK_WEBHOOK_URL` | ✓ | Incoming webhook for the digest channel (`#sequence-review`). |
| `REVIEWER_SHARED_SECRET` | ✓ | Gates `/reviewer/run` and `/backfill/plusvibe`. Generate: `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`. |
| `REVIEWER_LOOKBACK_HOURS` | optional | Default 36. Also used by GitHub Actions cron. |
| `ORIGAMI_WEBHOOK_SECRET` | ✓ | `whsec_...` from Origami's dashboard reveal modal. Without it, `/webhooks/origami` returns 500. |
| `DATA_DIR` | ✓ (Railway) | Filesystem path for the reply-event SQLite DB. `./data` locally; `/data` on Railway with mounted volume. |
| `PLUSVIBE_SLACK_SIGNING_SECRET` | ✓ | Slack app's Signing Secret. Used to verify Events API deliveries at `/webhooks/plusvibe`. |
| `SLACK_USER_TOKEN` | ✓ (backfill) | `xoxp-...` User OAuth Token. Used by `/backfill/plusvibe` to read channel history. |
| `SLACK_BOT_TOKEN` | optional | `xoxb-...` fallback if user token isn't available. |
| `PLUSVIBE_SLACK_CHANNEL_ID` | ✓ (backfill) | Channel ID (starts with `C…`) for `#outbound-plusvibe-replies`. |

## Local dev

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python -m pytest tests/ -v

cp .env.example .env   # then edit
uvicorn app:app --reload --port 8000
curl -X POST "http://localhost:8000/reviewer/run?secret=$REVIEWER_SHARED_SECRET"
```

## Deploy (Railway)

The production service runs on Railway with a mounted volume at `/data` for
SQLite persistence. Deploys go via the Railway CLI:

```bash
railway link -w "andrestrylido's Projects" -p accurate-luck -e production -s Outbound-Automation
railway up --detach
```

For everything else — webhook setup, secret rotation, adding workspaces,
troubleshooting failed deploys — see [`docs/OPERATIONS.md`](docs/OPERATIONS.md).
