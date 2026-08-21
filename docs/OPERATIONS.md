# Operations — Sequence Reviewer

Ops runbook, endpoint reference, and troubleshooting playbook. Assume the reader
is picking this up cold with no other context.

## System at a glance

- **Production URL:** `https://outbound-automation-production-f19d.up.railway.app`
- **Railway project:** `andrestrylido's Projects → accurate-luck → Outbound-Automation` (production environment)
- **Volume:** `outbound-automation-volume` mounted at `/data`, holds `reviewer.sqlite`
- **Repo:** `github.com/alexkamp-lido/Outbound-Automation`
- **Digest channel:** Slack `#sequence-review` (`C0BQL4YUPU7`)
- **Plusvibe notification channel:** Slack `#outbound-plusvibe-replies` (`C0BSFFCFJ64`)
- **Cron:** GitHub Actions `.github/workflows/daily-digest.yml`, 21:00 UTC daily (5pm ET during EDT, 4pm ET during EST)

## Endpoint reference

All endpoints run on the production URL above. Only `/health` is unauthenticated.

### `GET /health`
Liveness check. Returns `{"ok": true, "service": "sequence-reviewer"}`.

### `POST /reviewer/run?secret=<REVIEWER_SHARED_SECRET>`
Build today's digest and post to Slack. Response body carries section counts.
Called by the GitHub Actions cron. Safe to invoke manually.

```bash
curl -X POST "https://outbound-automation-production-f19d.up.railway.app/reviewer/run?secret=$REVIEWER_SHARED_SECRET"
```

### `POST /webhooks/origami`
Receives Origami's `sequence.reply.received` and `webhook.test` events. Verifies
HMAC-SHA256 per Standard Webhooks. Dedupes on the `webhook-id` header. Persists
to `origami_reply_events` (PK = `webhook_id`). Registered in Origami dashboard →
Settings → Developers → Webhooks.

### `POST /webhooks/plusvibe`
Receives Slack Events API deliveries when Plusvibe posts to
`#outbound-plusvibe-replies`. Handles the one-time `url_verification` handshake
before signature checks. Parses each `event_callback → message` via
`plusvibe_parser`, persists to `plusvibe_reply_events` (PK = `slack_ts`).
Registered in the Slack app dashboard → Event Subscriptions.

### `POST /backfill/plusvibe?secret=<REVIEWER_SHARED_SECRET>&hours=N`
Pulls Slack `conversations.history` for `PLUSVIBE_SLACK_CHANNEL_ID`, parses each
Plusvibe bot message, inserts new rows into `plusvibe_reply_events`. Idempotent
(dedups on `slack_ts`). Use to catch up if the Events API pipe was down or if
messages preceded the Slack app being installed.

```bash
curl -X POST "https://outbound-automation-production-f19d.up.railway.app/backfill/plusvibe?secret=$REVIEWER_SHARED_SECRET&hours=24"
# → {"ok":true,"hours":24,"raw_messages":8,"parsed":4,"inserted":4}
```

## Data model

Two tables in `${DATA_DIR}/reviewer.sqlite`. Both prune rows older than 30 days
on each insert.

### `origami_reply_events`

| column | source | notes |
|--------|--------|-------|
| `webhook_id` (PK) | Origami `webhook-id` header | Dedup key |
| `event_id` | envelope `id` | |
| `event_timestamp` | envelope `timestamp` | When Origami published |
| `received_at` | `data.reply_message.received_at` | **The reply's real time** |
| `channel` | `data.channel` | `email` or `linkedin` |
| `sequence_id` | `data.sequence_id` | Resolved to campaign name at digest time |
| `recipient` | `outreach_target.email` or `.linkedin_slug` | lowercased |
| `subject`, `snippet`, `sender_display_name`, `newly_stopped` | reply message fields | |

### `plusvibe_reply_events`

| column | source | notes |
|--------|--------|-------|
| `slack_ts` (PK) | Slack `ts` | Dedup key |
| `received_at` | derived from `ts` | ISO 8601 UTC |
| `campaign_name` | header text before `" - Lead Marked As "` | Slack entities un-escaped |
| `recipient` | Email field mailto | lowercased |
| `label` | Label field | e.g. `Not Interested`, `Interested`, `OOO` |
| `is_ooo` | `label ∈ {ooo, out of office, out-of-office}` | Excluded from reconciliation |
| `workspace`, `webhook_name` | Plusvibe metadata | |

Plusvibe notifications do NOT include reply body — `snippet` stays empty in the
digest for those rows.

## Runbook

### Trigger the digest manually
```bash
curl -X POST "$URL/reviewer/run?secret=$REVIEWER_SHARED_SECRET"
```
Or from GitHub: `gh workflow run daily-digest.yml --repo alexkamp-lido/Outbound-Automation`

### Backfill Plusvibe from Slack
```bash
curl -X POST "$URL/backfill/plusvibe?secret=$REVIEWER_SHARED_SECRET&hours=24"
```

### Change the cron time
Edit `cron:` line in `.github/workflows/daily-digest.yml`, commit, push. Format
is UTC. Example: `0 22 * * *` = 22:00 UTC (5pm ET winter / 6pm ET summer).

### Add a new Origami workspace
Nothing. `ORIGAMI_WORKSPACE_ID` empty → reviewer calls `GET /api/v2/workspaces`
on every run, enumerating all workspaces the API key can see. New workspaces
appear automatically.

### Narrow to specific Origami campaigns
Set `ORIGAMI_CAMPAIGN_IDS=<uuid>,<uuid>,<uuid>` in Railway variables. Then only
those campaigns are scanned. New campaigns require adding IDs by hand.

### Rotate secrets

Any of these can be rotated independently:

- `REVIEWER_SHARED_SECRET`: generate new (`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`), set in Railway variables, update GitHub repo secret `REVIEWER_SHARED_SECRET`. No redeploy needed.
- `ORIGAMI_WEBHOOK_SECRET`: Origami dashboard → Settings → Developers → Webhooks → the endpoint → **Rotate secret**. 24-hour grace period during rollover. Update Railway var.
- `PLUSVIBE_SLACK_SIGNING_SECRET`: Slack app → Basic Information → **Regenerate Signing Secret** → paste into Railway.
- `SLACK_USER_TOKEN` / `SLACK_BOT_TOKEN`: Slack app → OAuth & Permissions → **Reinstall to Workspace** to force new tokens → paste into Railway.
- `ORIGAMI_API_KEY`: Origami dashboard → Settings → Developers → API Keys → rotate.

### Deploy new code

```bash
railway link -w "andrestrylido's Projects" -p accurate-luck -e production -s Outbound-Automation
railway up --detach
```

**Important:** `railway up` uploads the local working tree, not a git snapshot.
Uncommitted local changes ship. Make sure `git status` is clean or intentional.

Verify: hit `/openapi.json` and confirm `info.version` and `paths` match what
you shipped. Bump `version=` in `app.py` for meaningful deploys — it doubles as
a "did the deploy actually land" marker.

### Add/change Slack destinations

- **Digest channel:** update `SLACK_WEBHOOK_URL` in Railway to the new incoming webhook URL.
- **Plusvibe notification channel:** requires (a) new incoming webhook URL from Slack pasted into Plusvibe's Slack integration, (b) inviting the Slack app (`Plusvibe Reply Relay`) to the new channel, (c) updating `PLUSVIBE_SLACK_CHANNEL_ID` in Railway to the new channel ID.

## Troubleshooting

### Symptom: digest shows 0 Origami replies

1. Is the Origami webhook still delivering? Origami dashboard → Settings → Developers → Webhooks → the endpoint → **Deliveries** log. Green = arriving. Red = check signature or endpoint.
2. Are events landing in SQLite? Curl `/reviewer/run` and check response counts. Or `railway ssh` into the container and `sqlite3 /data/reviewer.sqlite "SELECT count(*) FROM origami_reply_events WHERE received_at >= datetime('now','-24 hours');"` (if railway ssh is available).
3. `ORIGAMI_WEBHOOK_SECRET` set correctly? If missing, `/webhooks/origami` returns 500 and Origami's log will show it.
4. Lookback window too tight? Default is 36h — bump `REVIEWER_LOOKBACK_HOURS`.

### Symptom: digest shows 0 Plusvibe replies

1. Are new notifications landing in `#outbound-plusvibe-replies`? If not, the Plusvibe integration itself is broken — check Plusvibe's Slack integration config.
2. Is the Slack Events API delivering? Slack app → Event Subscriptions → look at recent delivery attempts and their status.
3. Is `PLUSVIBE_SLACK_SIGNING_SECRET` correct? Signature mismatch returns 401.
4. Try backfill: `POST /backfill/plusvibe?hours=48` — if that returns >0 inserted, the parser works and the Events API pipe is the issue.
5. Backfill returns 0 raw messages? Wrong `PLUSVIBE_SLACK_CHANNEL_ID`, or the user token lost `channels:history` scope.

### Symptom: deploy went through but URL still serves old code

We hit this — was the root cause of a long debug session. The Hobby-tier
service was orphaned but still owned the URL; new deploys landed on a
sibling Pro service that had no public domain.

Diagnostic recipe:
1. Bump `version=` in `app.py`, commit, `railway up --detach`.
2. Poll `/openapi.json` and check `info.version`. If it stays on the old version, `railway up` isn't landing on the service that owns the URL.
3. Check which service owns the URL: `railway status` from the linked project. Every service has its own auto-assigned subdomain.
4. If the URL is owned by a different service, either (a) generate a new domain on the target service (`railway domain`) and migrate all external references, or (b) delete the wrong service.

Also: `railway up` uploads local working tree state. Uncommitted changes ship;
committed changes not present locally do NOT ship. Verify with `git status` +
`git diff HEAD`.

### Symptom: Slack Events API URL verification fails

1. Is the target URL actually alive? `curl -X POST "$URL/webhooks/plusvibe" -H "Content-Type: application/json" -d '{"type":"url_verification","challenge":"probe"}'` — should return `{"challenge":"probe"}` with 200.
2. If 404: endpoint isn't deployed. See "deploy went through but URL still serves old code" above.
3. If 401: signing secret mismatch — but url_verification is unsigned per our code, so this shouldn't happen unless the payload is malformed.
4. If Slack sees red-line-strike-through on "Verified", click **Retry** after fixing.

### Symptom: `railway link` prompts even with all flags set

The Railway CLI version matters. `2.1.0` (from `brew install railwayapp/railway/railway`) is stale — API endpoints return 404 on auth. Use `5.x+` via `npm install -g @railway/cli`.

### Symptom: cron job silently fails

Check GitHub Actions:
```bash
gh run list --workflow=daily-digest.yml --repo alexkamp-lido/Outbound-Automation --limit 5
gh run view <run-id> --repo alexkamp-lido/Outbound-Automation --log-failed
```

Common causes:
- `REVIEWER_URL` secret has stale domain (must be `-f19d`, not `-b9ab`)
- `REVIEWER_SHARED_SECRET` mismatch between GitHub Secret and Railway env

## Architecture decisions

Why the shape looks this way — records so future changes don't re-litigate.

### Why webhooks for Origami replies (not polling)
Polling `GET /campaigns/:id/people?status=replied` and filtering by `addedAt`
misses everyone added to a campaign more than the lookback window ago —
`addedAt` is when the recipient was added, not when they replied. There is no
reply-time field on the campaign_person payload. Webhooks carry
`data.reply_message.received_at`, which is the real reply timestamp.

### Why Slack channel scraping for Plusvibe (not REST)
Plusvibe's REST API doesn't expose a replies-with-labels endpoint reachable
from a headless service, and their MCP isn't callable from Railway. Their
Slack integration DOES emit structured Block Kit messages with recipient email
+ campaign name + label. We piggyback on that instead of waiting for a REST
route. Slack becomes the canonical read source for Plusvibe replies.

### Why SQLite (not Postgres)
Reply volume is small (dozens/day). Single-writer, single-reader. Persistence
requirement is "survive redeploys" — Railway volume solves that. Postgres
addon costs and complexity for zero-benefit at this scale.

### Why GitHub Actions cron (not Origami scheduled agents)
Origami's Scheduled Agents run Origami instructions, not arbitrary HTTP
requests. They can't POST to our reviewer endpoint. GitHub Actions is free,
already tied to the repo, and stores the secret via `GITHUB_SECRETS` rather
than a URL that gets logged.

### Why one-way reconciliation (only "Plusvibe reply → still active in Origami")
We can enumerate Origami's still-active roster via `GET
/campaigns/:id/people?status=sent` filtered on `sendStatus="sent" and not
stopReason`. Plusvibe has no equivalent read source. Once we resolve that
(their REST, mailbox forwarding to a poll target, another Slack channel with
active-lead notifications, etc.), populate the parallel `plusvibe_active`
collector — reconciliation code is already generic.

### Why per-service Railway URLs matter
Railway auto-assigns `<service>-<env>-<hash>.up.railway.app` when a service
is created. The hash is per-service and can't be reassigned. If you migrate to
a new service (workspace transfer, fresh deploy, etc.), external references
(Origami webhook URL, Slack Events API Request URL, GitHub secret
`REVIEWER_URL`) must be updated. Custom domains via `railway domain add
<name>` are the only stable option across service moves.

### Why the digest is read-only
Automatic termination on the wrong platform is high-consequence: stopping a
sequence for someone who replied "yes, let's talk" would be catastrophic. The
digest surfaces suggestions; the operator confirms and clicks stop in the
platform UI. Once we're confident in label parsing quality across both
platforms, this could relax — but not before.
