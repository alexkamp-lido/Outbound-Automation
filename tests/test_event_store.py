"""SQLite event store tests — dedup, insert/read, lookback window."""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.event_store import (
    insert_reply_event,
    list_recent_replies,
    open_store,
)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _envelope(*, webhook_id: str, received_at: str, channel: str = "email",
              email: str | None = None, linkedin_slug: str | None = None,
              sequence_id: str = "seq-1", body: str = "hi") -> dict:
    return {
        "type": "sequence.reply.received",
        "id": webhook_id,
        "timestamp": received_at,
        "data": {
            "channel": channel,
            "sequence_id": sequence_id,
            "newly_stopped": True,
            "outreach_target": {
                "email": email,
                "linkedin_slug": linkedin_slug,
                "linkedin_urn": None,
                "table_id": None,
                "row_id": None,
                "column_id": None,
            },
            "sender": {"email": "me@x.co", "linkedin_slug": None, "display_name": "Me"},
            "reply_message": {
                "subject": "re: hi",
                "body": body,
                "body_truncated": False,
                "received_at": received_at,
                "thread_id": None,
                "provider_id": None,
            },
        },
    }


class TestEventStore:
    def _tmp_store(self):
        tmp = tempfile.mkdtemp()
        return open_store(data_dir=tmp), tmp

    def test_insert_and_read_back(self):
        conn, _ = self._tmp_store()
        env = _envelope(
            webhook_id="wh-1",
            received_at=_iso(datetime.now(timezone.utc)),
            email="pat@acme.com",
        )
        assert insert_reply_event(conn, env, webhook_id="wh-1") is True
        rows = list_recent_replies(conn, hours=1)
        assert len(rows) == 1
        assert rows[0].recipient == "pat@acme.com"
        assert rows[0].sequence_id == "seq-1"
        assert rows[0].channel == "email"
        assert rows[0].snippet == "hi"

    def test_dedup_on_webhook_id(self):
        conn, _ = self._tmp_store()
        env = _envelope(
            webhook_id="wh-2",
            received_at=_iso(datetime.now(timezone.utc)),
            email="a@b.com",
        )
        assert insert_reply_event(conn, env, webhook_id="wh-2") is True
        assert insert_reply_event(conn, env, webhook_id="wh-2") is False
        assert len(list_recent_replies(conn, hours=1)) == 1

    def test_lookback_window_filters_old_rows(self):
        conn, _ = self._tmp_store()
        now = datetime.now(timezone.utc)
        recent = _envelope(
            webhook_id="wh-new", received_at=_iso(now - timedelta(hours=2)),
            email="new@x.co",
        )
        old = _envelope(
            webhook_id="wh-old", received_at=_iso(now - timedelta(days=5)),
            email="old@x.co",
        )
        assert insert_reply_event(conn, recent, "wh-new")
        assert insert_reply_event(conn, old, "wh-old")
        rows_36h = list_recent_replies(conn, hours=36)
        assert [r.recipient for r in rows_36h] == ["new@x.co"]
        rows_all = list_recent_replies(conn, hours=24 * 30)
        assert {r.recipient for r in rows_all} == {"new@x.co", "old@x.co"}

    def test_linkedin_recipient_pulled_from_slug(self):
        conn, _ = self._tmp_store()
        env = _envelope(
            webhook_id="wh-li",
            received_at=_iso(datetime.now(timezone.utc)),
            channel="linkedin",
            linkedin_slug="jane-doe-123",
        )
        assert insert_reply_event(conn, env, "wh-li")
        rows = list_recent_replies(conn, hours=1)
        assert rows[0].recipient == "jane-doe-123"
        assert rows[0].channel == "linkedin"

    def test_retention_prune_drops_ancient_rows(self):
        conn, _ = self._tmp_store()
        now = datetime.now(timezone.utc)
        ancient = _envelope(
            webhook_id="wh-anc", received_at=_iso(now - timedelta(days=60)),
            email="ancient@x.co",
        )
        assert insert_reply_event(conn, ancient, "wh-anc")
        # A second, recent insert triggers the retention prune.
        recent = _envelope(
            webhook_id="wh-recent", received_at=_iso(now),
            email="recent@x.co",
        )
        assert insert_reply_event(conn, recent, "wh-recent")
        rows_all = list_recent_replies(conn, hours=24 * 365)
        assert [r.recipient for r in rows_all] == ["recent@x.co"]

    def test_default_data_dir_can_be_created(self):
        tmp = Path(tempfile.mkdtemp()) / "nested" / "data"
        conn = open_store(data_dir=str(tmp))
        assert tmp.exists()
        conn.close()
