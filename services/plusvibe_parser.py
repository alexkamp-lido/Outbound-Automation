"""
Parse Plusvibe reply notifications posted to Slack.

Plusvibe's Slack integration posts bot messages with a structured Block Kit
payload — header carries the campaign name + label, section fields carry
email/status/label/workspace. Reply text isn't included in the notification,
so `snippet` stays empty. `slack_ts` is the dedup key.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

# Slack mailto link: <mailto:foo@x.co|foo@x.co>
_MAILTO_RE = re.compile(r"<mailto:([^|>]+)(?:\|[^>]*)?>")

# Header format: "<campaign name> - Lead Marked As <label>"
_HEADER_SPLIT = " - Lead Marked As "

_LABEL_OOO = {"ooo", "out of office", "out-of-office"}


def _unescape_slack(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def parse_plusvibe_message(message: dict) -> Optional[dict]:
    """
    Convert a Slack `bot_message` posted by Plusvibe into a normalized reply row.
    Returns None if the message doesn't look like a Plusvibe reply notification.
    """
    if message.get("subtype") != "bot_message":
        return None
    if (message.get("username") or "").lower() != "plusvibe":
        return None
    blocks = message.get("blocks") or []
    if not blocks:
        return None

    header_text = ""
    fields: dict[str, str] = {}
    for b in blocks:
        btype = b.get("type")
        if btype == "header":
            header_text = ((b.get("text") or {}).get("text") or "").strip()
        elif btype == "section":
            for f in b.get("fields") or []:
                raw = (f.get("text") or "").strip()
                # Slack field pattern: "*Label:*\nValue"
                if raw.startswith("*"):
                    label, sep, value = raw.partition("*\n")
                    if sep:
                        label = label.strip("*").strip(":").strip()
                        fields[label] = value.strip()

    if (fields.get("Status") or "").upper() != "REPLIED":
        return None

    campaign_name = _unescape_slack(header_text.split(_HEADER_SPLIT, 1)[0].strip())
    if not campaign_name:
        return None

    email_raw = fields.get("Email", "")
    m = _MAILTO_RE.search(email_raw)
    email = (m.group(1) if m else email_raw).strip().lower()
    if not email or "@" not in email:
        return None

    label = fields.get("Label", "").strip()

    ts = message.get("ts", "")
    try:
        received_at = datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None

    return {
        "slack_ts": ts,
        "received_at": received_at,
        "campaign_name": campaign_name,
        "recipient": email,
        "label": label,
        "workspace": fields.get("Workspace", ""),
        "webhook_name": fields.get("Webhook Name", ""),
        "is_ooo": label.lower() in _LABEL_OOO,
    }
