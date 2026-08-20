"""
Slack Events API signature verification for the Plusvibe reply relay.

Slack sends `X-Slack-Signature: v0=<hex>` and `X-Slack-Request-Timestamp: <unix>`.
Sig = HMAC-SHA256(signing_secret, f"v0:{timestamp}:{raw_body}").hexdigest().
See https://api.slack.com/authentication/verifying-requests-from-slack.

Pure — no HTTP concerns.
"""

from __future__ import annotations

import hmac
import time
from hashlib import sha256

REPLAY_WINDOW_SECONDS = 300  # ±5 min per Slack spec


def verify_slack_signature(
    *,
    raw_body: bytes,
    timestamp: str,
    signature: str,
    signing_secret: str,
    now: float | None = None,
) -> bool:
    """Return True iff signature matches AND timestamp is within ±5 min."""
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > REPLAY_WINDOW_SECONDS:
        return False
    signed = f"v0:{timestamp}:".encode() + raw_body
    expected = "v0=" + hmac.new(
        signing_secret.encode(), signed, sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def sign_test_payload(
    *,
    raw_body: bytes,
    timestamp: str,
    signing_secret: str,
) -> str:
    """Produce a valid `v0=<hex>` signature — used by tests only."""
    signed = f"v0:{timestamp}:".encode() + raw_body
    return "v0=" + hmac.new(signing_secret.encode(), signed, sha256).hexdigest()
