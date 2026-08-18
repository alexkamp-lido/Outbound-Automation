"""Outbound Slack notifications via an incoming webhook."""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class SlackNotifierError(Exception):
    pass


def post_digest(
    blocks: list[dict],
    text_fallback: str = "Sequence Reviewer digest",
    webhook_url: Optional[str] = None,
) -> None:
    """
    Post a Block Kit digest to Slack via an incoming webhook.

    Raises SlackNotifierError if the URL is missing or Slack returns non-2xx.
    """
    url = webhook_url or os.getenv("SLACK_WEBHOOK_URL")
    if not url:
        raise SlackNotifierError(
            "SLACK_WEBHOOK_URL not set; cannot post reviewer digest."
        )
    try:
        r = requests.post(url, json={"text": text_fallback, "blocks": blocks}, timeout=15)
    except requests.RequestException as e:
        raise SlackNotifierError(f"Network error posting to Slack: {e}") from e
    if not (200 <= r.status_code < 300):
        raise SlackNotifierError(
            f"Slack webhook returned {r.status_code}: {r.text[:200]}"
        )
    logger.info("Posted digest to Slack (%d blocks)", len(blocks))
