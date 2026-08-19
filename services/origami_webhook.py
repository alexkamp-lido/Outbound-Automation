"""
Origami webhook signature verification (Standard Webhooks / HMAC-SHA256).

Ported verbatim from ~/.claude/skills/origami-webhooks/SKILL.md (lines 213-248).
Pure — no HTTP concerns.
"""

from __future__ import annotations

import base64
import hmac
import re
import time
from hashlib import sha256

PREFIX = "whsec_"
REPLAY_WINDOW_SECONDS = 300  # ±5 minutes, spec-recommended


def key_from_secret(secret: str) -> bytes:
    body = secret[len(PREFIX):] if secret.startswith(PREFIX) else secret
    return base64.b64decode(body)


def verify_origami_webhook(
    *,
    raw_body: bytes,
    webhook_id: str,
    webhook_timestamp: str,
    webhook_signature: str,
    secret: str,
    now: float | None = None,
) -> bool:
    """Return True iff the signature matches AND the dispatch timestamp is within ±5 min."""
    try:
        ts = int(webhook_timestamp)
    except (ValueError, TypeError):
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > REPLAY_WINDOW_SECONDS:
        return False
    signed = f"{webhook_id}.{webhook_timestamp}.".encode() + raw_body
    expected = base64.b64encode(
        hmac.new(key_from_secret(secret), signed, sha256).digest()
    ).decode()
    for m in re.finditer(r"v1,([^\s,]+)", webhook_signature or ""):
        candidate = m.group(1)
        if len(candidate) != len(expected):
            continue
        if hmac.compare_digest(candidate, expected):
            return True
    return False


def sign_test_payload(
    *,
    raw_body: bytes,
    webhook_id: str,
    webhook_timestamp: str,
    secret: str,
) -> str:
    """Produce a valid `v1,<base64>` signature for a raw body — used by tests only."""
    signed = f"{webhook_id}.{webhook_timestamp}.".encode() + raw_body
    digest = base64.b64encode(
        hmac.new(key_from_secret(secret), signed, sha256).digest()
    ).decode()
    return f"v1,{digest}"
