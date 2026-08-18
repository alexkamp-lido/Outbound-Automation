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
Origami scheduled agent (5pm ET, daily)
   │
   ▼
POST https://<railway-url>/reviewer/run?secret=<REVIEWER_SHARED_SECRET>
   │
   ▼
collect_origami() ── walks each campaign, gathers replies + still-active roster
collect_plusvibe() ── stub until a Plusvibe read source is wired
compute_reconciliations() ── cross-joins replies vs. still-active by email
build_digest() ── Slack Block Kit
   │
   ▼
POST SLACK_WEBHOOK_URL → digest lands in Slack
```

## Files

| File | Purpose |
|------|---------|
| `app.py` | FastAPI app: `POST /reviewer/run`, `GET /health`. |
| `services/origami_client.py` | Origami v2 REST client — `discover_campaigns()`, `iter_people()`, `stop_sequence()`. |
| `services/sequence_reviewer.py` | Collectors, reconciliation join, pure `build_digest()`, `run_reviewer()`. |
| `services/slack_notifier.py` | `post_digest(blocks)` → `SLACK_WEBHOOK_URL`. |
| `tests/test_sequence_reviewer.py` | Unit tests for the join + digest shape. |

## Configuration

Copy `.env.example` → `.env` and fill in:

| Var | Required | Notes |
|-----|----------|-------|
| `ORIGAMI_API_KEY` | ✓ | `og_live_...` bearer token. |
| `ORIGAMI_CAMPAIGN_IDS` | ✓ (if no workspace) | Comma-separated campaign IDs. New campaigns must be added manually. |
| `ORIGAMI_WORKSPACE_ID` | optional | If present, replaces the manual list — enumerates every campaign in the workspace. |
| `SLACK_WEBHOOK_URL` | ✓ | Incoming webhook for the reviewer channel. |
| `REVIEWER_SHARED_SECRET` | recommended | Gates `/reviewer/run`. Generate: `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`. |
| `REVIEWER_LOOKBACK_HOURS` | optional | Default 36. |

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
3. **Variables** tab: paste every var from `.env.example`.
4. Railway detects the Dockerfile and deploys. Note the public URL.
5. In Origami, create a scheduled agent (daily 5pm ET) whose only instruction
   is: `POST <railway-url>/reviewer/run?secret=<REVIEWER_SHARED_SECRET>`.
6. Trigger it once from Origami's dashboard to smoke-test.

## Plusvibe (open dependency)

`collect_plusvibe()` returns empty and the digest shows a "Plusvibe read source
not connected yet" banner. The REST API doesn't currently expose an
inbox/replies-with-labels endpoint and Plusvibe's MCP isn't reachable from a
headless service. When a read source is resolved (direct REST route,
mailbox-forwarding to a Gmail Railway can poll, etc.), populate
`collect_plusvibe()` — the digest and reconciliation code are already generic.

## Recovering the Origami workspace ID

If you want to switch off the manual campaign list, any single campaign detail
call carries `workspaceId`:

```bash
curl -H "Authorization: Bearer $ORIGAMI_API_KEY" \
  https://origami.chat/api/v2/campaigns/<any-campaign-id> | jq .workspaceId
```

Set the returned value as `ORIGAMI_WORKSPACE_ID` and remove
`ORIGAMI_CAMPAIGN_IDS`; the client prefers workspace enumeration when both are
set.
