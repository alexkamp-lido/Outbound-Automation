"""Parser tests against real Plusvibe Slack notification shapes."""

from services.plusvibe_parser import parse_plusvibe_message


def _msg(header: str, fields: list[tuple[str, str]], *, ts: str = "1787329404.951679",
         subtype: str = "bot_message", username: str = "PlusVibe") -> dict:
    field_blocks = [{"type": "mrkdwn", "text": f"*{label}:*\n{value}"} for label, value in fields]
    return {
        "subtype": subtype,
        "text": "Notification",
        "username": username,
        "ts": ts,
        "type": "message",
        "bot_id": "B0BSFURCBDE",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": header}},
            {"type": "section", "fields": field_blocks},
        ],
    }


class TestPlusvibeParser:
    def test_replied_email_extracted(self):
        m = _msg(
            "Medical Billing &lt;15 Emp - Lead Marked As Not Interested",
            [
                ("Webhook Name", "Plusvibe Reconciliation via Slack"),
                ("Workspace", "Andres's Workspace"),
                ("Email", "<mailto:chris@salsburyandco.com|chris@salsburyandco.com>"),
                ("Status", "REPLIED"),
                ("Label", "Not Interested"),
            ],
        )
        p = parse_plusvibe_message(m)
        assert p is not None
        assert p["recipient"] == "chris@salsburyandco.com"
        assert p["campaign_name"] == "Medical Billing <15 Emp"
        assert p["label"] == "Not Interested"
        assert p["is_ooo"] is False
        assert p["slack_ts"] == "1787329404.951679"
        assert p["received_at"].startswith("20")  # ISO date

    def test_ooo_flag(self):
        m = _msg(
            "Cold Outreach - Lead Marked As OOO",
            [
                ("Email", "<mailto:a@b.co|a@b.co>"),
                ("Status", "REPLIED"),
                ("Label", "OOO"),
            ],
        )
        p = parse_plusvibe_message(m)
        assert p["is_ooo"] is True

    def test_non_replied_status_returns_none(self):
        m = _msg(
            "Cold Outreach - Lead Marked As Not Interested",
            [("Email", "<mailto:a@b.co|a@b.co>"), ("Status", "SENT"), ("Label", "-")],
        )
        assert parse_plusvibe_message(m) is None

    def test_non_bot_message_returns_none(self):
        m = _msg("X - Lead Marked As Y", [("Email", "<mailto:a@b.co|a@b.co>"), ("Status", "REPLIED"), ("Label", "L")], subtype="channel_join")
        assert parse_plusvibe_message(m) is None

    def test_wrong_username_returns_none(self):
        m = _msg("X - Lead Marked As Y", [("Email", "<mailto:a@b.co|a@b.co>"), ("Status", "REPLIED"), ("Label", "L")], username="Other")
        assert parse_plusvibe_message(m) is None

    def test_missing_email_returns_none(self):
        m = _msg("X - Lead Marked As Y", [("Status", "REPLIED"), ("Label", "L")])
        assert parse_plusvibe_message(m) is None

    def test_bad_timestamp_returns_none(self):
        m = _msg("X - Lead Marked As Y", [("Email", "<mailto:a@b.co|a@b.co>"), ("Status", "REPLIED"), ("Label", "L")], ts="not-a-number")
        assert parse_plusvibe_message(m) is None

    def test_campaign_name_slack_entities_unescaped(self):
        m = _msg(
            "&amp;More &gt; Less - Lead Marked As Interested",
            [("Email", "<mailto:a@b.co|a@b.co>"), ("Status", "REPLIED"), ("Label", "Interested")],
        )
        p = parse_plusvibe_message(m)
        assert p["campaign_name"] == "&More > Less"
