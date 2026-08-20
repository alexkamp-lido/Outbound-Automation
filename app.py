"""
Sequence Reviewer — FastAPI service.

Single endpoint: POST /reviewer/run
  Called nightly by an Origami scheduled agent. Collects today's replies from
  Origami (Plusvibe is stubbed), builds a Slack Block Kit digest, and posts it
  to SLACK_WEBHOOK_URL.

Also exposes GET /health for platform liveness checks.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from services.event_store import insert_reply_event, open_store
from services.origami_webhook import verify_origami_webhook
from services.plusvibe_slack_webhook import verify_slack_signature
from services.sequence_reviewer import run_reviewer
from services.slack_notifier import post_digest, SlackNotifierError

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Sequence Reviewer", version="0.1.0")


@app.get("/health")
async def health():
    return {"ok": True, "service": "sequence-reviewer"}


@app.post("/reviewer/run")
async def reviewer_run(request: Request):
    """
    Build today's digest and post it to Slack.

    Gate: if REVIEWER_SHARED_SECRET is set, the caller must include
    ?secret=<value> matching that env var. Returns 401 otherwise.

    Response body carries counts so the caller's run log is meaningful.
    """
    expected = os.getenv("REVIEWER_SHARED_SECRET")
    if expected:
        supplied = request.query_params.get("secret", "")
        if supplied != expected:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing secret",
            )

    try:
        blocks, data = run_reviewer()
    except Exception as e:
        logger.exception("Reviewer collection failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Reviewer collection failed: {e}",
        )

    try:
        post_digest(blocks, text_fallback="Sequence Reviewer — daily rundown")
    except SlackNotifierError as e:
        logger.error("Slack post failed: %s", e)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "ok": False,
                "error": str(e),
                "sections": _sections(data),
            },
        )

    return {
        "ok": True,
        "generated_at": data.generated_at,
        "sections": _sections(data),
    }


@app.post("/webhooks/origami")
async def origami_webhook(request: Request):
    """
    Receive Origami's sequence.reply.received (+ webhook.test) events.

    Verifies HMAC-SHA256 per the Standard Webhooks spec, dedupes on webhook-id,
    persists sequence.reply.received envelopes to the SQLite event store.
    """
    secret = os.getenv("ORIGAMI_WEBHOOK_SECRET")
    if not secret:
        logger.error("ORIGAMI_WEBHOOK_SECRET not set; refusing webhook")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="webhook secret not configured",
        )

    raw = await request.body()
    webhook_id = request.headers.get("webhook-id", "")
    webhook_timestamp = request.headers.get("webhook-timestamp", "")
    webhook_signature = request.headers.get("webhook-signature", "")

    if not verify_origami_webhook(
        raw_body=raw,
        webhook_id=webhook_id,
        webhook_timestamp=webhook_timestamp,
        webhook_signature=webhook_signature,
        secret=secret,
    ):
        logger.warning("Webhook signature invalid (id=%s)", webhook_id)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid signature")

    import json as _json

    try:
        envelope = _json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        logger.warning("Webhook body not valid JSON (id=%s): %s", webhook_id, e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="body is not JSON")

    event_type = envelope.get("type", "")

    if event_type == "webhook.test":
        logger.info("[origami-webhook] test event received (id=%s)", webhook_id)
        return {"ok": True, "type": event_type}

    if event_type != "sequence.reply.received":
        # Accept but ignore any subscribed event we don't process yet.
        logger.info("[origami-webhook] ignoring event type=%s id=%s", event_type, webhook_id)
        return {"ok": True, "type": event_type, "stored": False}

    conn = open_store()
    try:
        inserted = insert_reply_event(conn, envelope, webhook_id)
    finally:
        conn.close()

    if inserted:
        logger.info(
            "[origami-webhook] stored reply (id=%s recipient=%s)",
            webhook_id,
            (envelope.get("data") or {}).get("outreach_target", {}).get("email")
            or (envelope.get("data") or {}).get("outreach_target", {}).get("linkedin_slug"),
        )
    else:
        logger.info("[origami-webhook] duplicate reply, ignored (id=%s)", webhook_id)
    return {"ok": True, "type": event_type, "stored": inserted}


@app.post("/webhooks/plusvibe")
async def plusvibe_webhook(request: Request):
    """
    Scaffold receiver for Plusvibe reply notifications forwarded from Slack.

    Accepts either a legacy Slack outgoing webhook (form-encoded, with a `token`
    field) or a Slack Events API POST (JSON). Handles the Events API
    `url_verification` handshake. Logs the full payload so we can inspect the
    Plusvibe notification format and write a parser against real data. No
    persistence yet — parser + storage land after we see one real message.
    """
    import json as _json

    content_type = request.headers.get("content-type", "").lower()
    raw = await request.body()

    if content_type.startswith("application/json"):
        try:
            payload = _json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            logger.warning("[plusvibe-webhook] non-JSON body on json content-type")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="body is not JSON")

        if payload.get("type") == "url_verification":
            challenge = payload.get("challenge", "")
            logger.info("[plusvibe-webhook] url_verification handshake ok")
            return {"challenge": challenge}

        signing_secret = os.getenv("PLUSVIBE_SLACK_SIGNING_SECRET")
        if signing_secret:
            timestamp = request.headers.get("x-slack-request-timestamp", "")
            signature = request.headers.get("x-slack-signature", "")
            if not verify_slack_signature(
                raw_body=raw,
                timestamp=timestamp,
                signature=signature,
                signing_secret=signing_secret,
            ):
                logger.warning("[plusvibe-webhook] slack signature invalid")
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid signature")

        logger.info("[plusvibe-webhook] json payload received: %s", _json.dumps(payload)[:2000])
        return {"ok": True}

    form = await request.form()
    payload = {k: form.get(k) for k in form.keys()}

    expected_token = os.getenv("PLUSVIBE_SLACK_TOKEN")
    supplied_token = payload.get("token", "")
    if expected_token and supplied_token != expected_token:
        logger.warning("[plusvibe-webhook] token mismatch")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")

    logger.info(
        "[plusvibe-webhook] slack outgoing webhook received: channel=%s user=%s text=%r",
        payload.get("channel_name"),
        payload.get("user_name"),
        (payload.get("text") or "")[:2000],
    )
    return {}


def _sections(data) -> dict:
    return {
        "origami_replies": len(data.origami_replies),
        "plusvibe_replies": len(data.plusvibe_replies),
        "reconciliations": len(data.reconciliations),
        "ooo_count": data.ooo_count,
        "plusvibe_connected": data.plusvibe_connected,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
