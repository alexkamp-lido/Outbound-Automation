"""
SQLite event store for Origami reply webhooks.

Schema is one table with `webhook_id` as the primary key (idempotency).
Reads for the digest go through `list_recent_replies`. Retention prunes
rows older than 30 days on each insert.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = "./data"
DB_FILENAME = "reviewer.sqlite"
RETENTION_DAYS = 30
SNIPPET_MAX_CHARS = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS origami_reply_events (
  webhook_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL,
  event_timestamp TEXT NOT NULL,
  received_at TEXT NOT NULL,
  channel TEXT NOT NULL,
  sequence_id TEXT NOT NULL,
  recipient TEXT NOT NULL,
  sender_display_name TEXT,
  subject TEXT,
  snippet TEXT,
  newly_stopped INTEGER,
  inserted_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_reply_received_at ON origami_reply_events(received_at DESC);
"""


@dataclass
class StoredReply:
    webhook_id: str
    event_id: str
    event_timestamp: str
    received_at: str
    channel: str
    sequence_id: str
    recipient: str
    sender_display_name: Optional[str]
    subject: Optional[str]
    snippet: Optional[str]
    newly_stopped: bool


def _resolve_db_path(data_dir: Optional[str] = None) -> Path:
    d = data_dir or os.getenv("DATA_DIR") or DEFAULT_DATA_DIR
    path = Path(d)
    path.mkdir(parents=True, exist_ok=True)
    return path / DB_FILENAME


def open_store(data_dir: Optional[str] = None) -> sqlite3.Connection:
    """Open (creating if missing) the reviewer SQLite DB and ensure schema."""
    db_path = _resolve_db_path(data_dir)
    conn = sqlite3.connect(db_path, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def insert_reply_event(
    conn: sqlite3.Connection,
    envelope: dict,
    webhook_id: str,
) -> bool:
    """
    Insert one `sequence.reply.received` envelope. Returns False if `webhook_id`
    already existed (dedup). Also prunes rows older than RETENTION_DAYS on success.
    """
    data = envelope.get("data") or {}
    reply = data.get("reply_message") or {}
    outreach = data.get("outreach_target") or {}
    sender = data.get("sender") or {}

    channel = data.get("channel", "")
    if channel == "email":
        recipient = (outreach.get("email") or "").strip().lower()
    else:
        recipient = (outreach.get("linkedin_slug") or "").strip().lower()

    body = reply.get("body") or ""
    snippet = body[:SNIPPET_MAX_CHARS]

    try:
        conn.execute(
            """
            INSERT INTO origami_reply_events (
                webhook_id, event_id, event_timestamp, received_at,
                channel, sequence_id, recipient, sender_display_name,
                subject, snippet, newly_stopped
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                webhook_id,
                envelope.get("id", ""),
                envelope.get("timestamp", ""),
                reply.get("received_at", ""),
                channel,
                data.get("sequence_id", ""),
                recipient,
                sender.get("display_name"),
                reply.get("subject"),
                snippet,
                1 if data.get("newly_stopped") else 0,
            ),
        )
    except sqlite3.IntegrityError:
        return False

    conn.execute(
        "DELETE FROM origami_reply_events WHERE received_at < datetime('now', ?)",
        (f"-{RETENTION_DAYS} days",),
    )
    return True


def list_recent_replies(conn: sqlite3.Connection, hours: int) -> list[StoredReply]:
    """Return all rows with received_at within the last `hours`, newest first."""
    rows = conn.execute(
        """
        SELECT * FROM origami_reply_events
        WHERE received_at >= datetime('now', ?)
        ORDER BY received_at DESC
        """,
        (f"-{hours} hours",),
    ).fetchall()
    return [
        StoredReply(
            webhook_id=r["webhook_id"],
            event_id=r["event_id"],
            event_timestamp=r["event_timestamp"],
            received_at=r["received_at"],
            channel=r["channel"],
            sequence_id=r["sequence_id"],
            recipient=r["recipient"],
            sender_display_name=r["sender_display_name"],
            subject=r["subject"],
            snippet=r["snippet"],
            newly_stopped=bool(r["newly_stopped"]),
        )
        for r in rows
    ]
