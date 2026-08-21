"""
Backfill Plusvibe reply events from Slack channel history.

Reads `conversations.history` for the Plusvibe-notification channel, parses each
bot message via `plusvibe_parser`, and inserts new rows into the SQLite event
store. Idempotent (slack_ts is PK, dups return False).

Usage:
    python -m services.plusvibe_backfill --hours 24

Env vars used when flags are omitted:
    SLACK_USER_TOKEN (or bot token with channels:history/groups:history)
    PLUSVIBE_SLACK_CHANNEL_ID
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

from .event_store import insert_plusvibe_reply, open_store
from .plusvibe_parser import parse_plusvibe_message

logger = logging.getLogger(__name__)

SLACK_HISTORY_URL = "https://slack.com/api/conversations.history"


def backfill(*, token: str, channel_id: str, hours: int) -> tuple[int, int, int]:
    """
    Fetch messages within the lookback window and insert Plusvibe replies.

    Returns (raw_message_count, parsed_count, inserted_count).
    """
    conn = open_store()
    raw = 0
    parsed_count = 0
    inserted = 0
    oldest_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    oldest = f"{oldest_dt.timestamp():.6f}"
    cursor: str | None = None
    headers = {"Authorization": f"Bearer {token}"}
    try:
        while True:
            params = {"channel": channel_id, "limit": "200", "oldest": oldest}
            if cursor:
                params["cursor"] = cursor
            r = requests.get(SLACK_HISTORY_URL, headers=headers, params=params, timeout=30)
            r.raise_for_status()
            body = r.json()
            if not body.get("ok"):
                raise RuntimeError(f"Slack API error: {body.get('error')} ({body})")
            for m in body.get("messages", []) or []:
                raw += 1
                p = parse_plusvibe_message(m)
                if p is None:
                    continue
                parsed_count += 1
                if insert_plusvibe_reply(conn, p):
                    inserted += 1
            cursor = (body.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
    finally:
        conn.close()
    return raw, parsed_count, inserted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--channel-id", default=os.getenv("PLUSVIBE_SLACK_CHANNEL_ID"))
    ap.add_argument("--token", default=os.getenv("SLACK_USER_TOKEN") or os.getenv("SLACK_BOT_TOKEN"))
    args = ap.parse_args()
    if not args.channel_id or not args.token:
        print(
            "ERROR: need --channel-id (or PLUSVIBE_SLACK_CHANNEL_ID) and --token "
            "(or SLACK_USER_TOKEN / SLACK_BOT_TOKEN)",
            file=sys.stderr,
        )
        return 2
    raw, parsed_count, inserted = backfill(token=args.token, channel_id=args.channel_id, hours=args.hours)
    print(f"raw_messages={raw} parsed_plusvibe_replies={parsed_count} inserted={inserted}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    raise SystemExit(main())
