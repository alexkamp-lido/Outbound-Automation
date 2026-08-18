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
