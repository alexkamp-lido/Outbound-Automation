"""Unit tests for the Sequence Reviewer digest builder + cross-reference logic."""

from unittest.mock import patch

from services.origami_client import OrigamiCampaign, OrigamiClient
from services.sequence_reviewer import (
    ActivePerson,
    DigestData,
    Reply,
    ReconciliationRow,
    build_digest,
    compute_reconciliations,
)


class TestReconciliations:
    def test_origami_reply_flags_active_plusvibe(self):
        origami_replies = [
            Reply(
                platform="origami",
                recipient="pat@acme.com",
                campaign_name="Origami Q3",
                campaign_id="c1",
                sequence_id="seq-1",
            )
        ]
        plusvibe_active = [
            ActivePerson(
                platform="plusvibe",
                recipient="pat@acme.com",
                campaign_name="Plusvibe Winter",
                campaign_id="p1",
            )
        ]
        rows = compute_reconciliations(origami_replies, [], [], plusvibe_active)
        assert len(rows) == 1
        assert rows[0].recipient == "pat@acme.com"
        assert rows[0].replied_on == "origami"
        assert rows[0].still_active_on == "plusvibe"
        assert rows[0].active_campaign_name == "Plusvibe Winter"

    def test_plusvibe_reply_flags_active_origami(self):
        plusvibe_replies = [
            Reply(
                platform="plusvibe",
                recipient="sam@beta.io",
                campaign_name="Cold Email A",
                campaign_id="p2",
                is_ooo=False,
            )
        ]
        origami_active = [
            ActivePerson(
                platform="origami",
                recipient="sam@beta.io",
                campaign_name="LinkedIn Push",
                campaign_id="c2",
                sequence_id="seq-42",
            )
        ]
        rows = compute_reconciliations([], plusvibe_replies, origami_active, [])
        assert len(rows) == 1
        assert rows[0].replied_on == "plusvibe"
        assert rows[0].still_active_on == "origami"
        assert rows[0].active_sequence_id == "seq-42"

    def test_ooo_plusvibe_reply_does_not_flag_origami(self):
        plusvibe_replies = [
            Reply(
                platform="plusvibe",
                recipient="ooo@corp.com",
                campaign_name="Cold Email A",
                campaign_id="p2",
                is_ooo=True,
            )
        ]
        origami_active = [
            ActivePerson(
                platform="origami",
                recipient="ooo@corp.com",
                campaign_name="LinkedIn Push",
                campaign_id="c2",
                sequence_id="seq-99",
            )
        ]
        rows = compute_reconciliations([], plusvibe_replies, origami_active, [])
        assert rows == []

    def test_no_overlap_returns_empty(self):
        origami_replies = [
            Reply(
                platform="origami",
                recipient="alone@x.com",
                campaign_name="X",
                campaign_id="c1",
            )
        ]
        plusvibe_active = [
            ActivePerson(
                platform="plusvibe",
                recipient="different@y.com",
                campaign_name="Y",
                campaign_id="p1",
            )
        ]
        assert compute_reconciliations(origami_replies, [], [], plusvibe_active) == []


class TestBuildDigest:
    def test_empty_day_renders_no_replies_placeholders(self):
        blocks = build_digest(
            DigestData(plusvibe_connected=True, generated_at="2026-08-18 00:00 UTC")
        )
        text = _flatten_blocks(blocks)
        assert "Sequence Reviewer" in text
        assert "No cross-platform overlap today" in text
        assert "No Origami replies" in text
        assert "No Plusvibe replies" in text

    def test_plusvibe_stub_shows_disconnected_banner(self):
        blocks = build_digest(DigestData(plusvibe_connected=False))
        text = _flatten_blocks(blocks)
        assert "Plusvibe read source not connected yet" in text
        assert "out-of-office" not in text

    def test_reconciliation_rows_render(self):
        data = DigestData(
            plusvibe_connected=True,
            reconciliations=[
                ReconciliationRow(
                    recipient="dana@lead.co",
                    replied_on="origami",
                    still_active_on="plusvibe",
                    active_campaign_name="Cold Q3 Batch",
                ),
                ReconciliationRow(
                    recipient="rick@lead.co",
                    replied_on="plusvibe",
                    still_active_on="origami",
                    active_campaign_name="LinkedIn Warmup",
                    active_sequence_id="seq-77",
                ),
            ],
        )
        blocks = build_digest(data)
        text = _flatten_blocks(blocks)
        assert "Stop in Plusvibe" in text
        assert "Stop in Origami" in text
        assert "sequence_id=seq-77" in text

    def test_origami_replies_render_snippet(self):
        data = DigestData(
            plusvibe_connected=True,
            origami_replies=[
                Reply(
                    platform="origami",
                    recipient="lee@target.com",
                    campaign_name="EBP Push",
                    campaign_id="c1",
                    channel="linkedin",
                    snippet="Thanks, would love a chat next week if you can",
                )
            ],
        )
        blocks = build_digest(data)
        text = _flatten_blocks(blocks)
        assert "lee@target.com" in text
        assert "linkedin" in text
        assert "Thanks, would love a chat" in text

    def test_ooo_count_hidden_summary(self):
        data = DigestData(
            plusvibe_connected=True,
            plusvibe_replies=[
                Reply(platform="plusvibe", recipient="a@b.com", campaign_name="c", campaign_id="p1", is_ooo=True),
                Reply(platform="plusvibe", recipient="c@d.com", campaign_name="c", campaign_id="p1", is_ooo=True),
            ],
            ooo_count=2,
        )
        blocks = build_digest(data)
        text = _flatten_blocks(blocks)
        assert "2 out-of-office" in text
        assert "a@b.com" not in text
        assert "c@d.com" not in text

    def test_all_blocks_have_valid_shape(self):
        blocks = build_digest(DigestData(plusvibe_connected=True))
        assert isinstance(blocks, list)
        for b in blocks:
            assert "type" in b
            assert b["type"] in {"header", "section", "divider", "context"}


class TestDiscoverCampaigns:
    def test_workspace_id_short_circuits(self):
        client = OrigamiClient(api_key="test", workspace_id="ws-1")
        with patch.object(client, "list_workspace_campaigns") as list_ws, \
             patch.object(client, "get_campaign") as get_c:
            list_ws.return_value = [_campaign("c1")]
            result = client.discover_campaigns()
            list_ws.assert_called_once()
            get_c.assert_not_called()
            assert [c.id for c in result] == ["c1"]

    def test_bootstrap_discovers_workspace_then_lists(self):
        client = OrigamiClient(api_key="test", bootstrap_campaign_id="boot-99")
        with patch.object(client, "get_campaign") as get_c, \
             patch.object(client, "list_workspace_campaigns") as list_ws:
            get_c.return_value = _campaign("boot-99", workspace_id="ws-42")
            list_ws.return_value = [_campaign("c1"), _campaign("c2")]
            result = client.discover_campaigns()
            get_c.assert_called_once_with("boot-99")
            list_ws.assert_called_once()
            assert client.workspace_id == "ws-42"  # cached for future calls
            assert {c.id for c in result} == {"c1", "c2"}

    def test_manual_campaign_ids_fallback(self):
        client = OrigamiClient(api_key="test", campaign_ids=["a", "b"])
        with patch.object(client, "get_campaign") as get_c:
            get_c.side_effect = [_campaign("a"), _campaign("b")]
            result = client.discover_campaigns()
            assert [c.id for c in result] == ["a", "b"]
            assert get_c.call_count == 2

    def test_no_config_raises(self):
        from services.origami_client import OrigamiAPIError

        client = OrigamiClient(api_key="test")
        # ensure no env leakage into fallback
        client.campaign_ids = []
        try:
            client.discover_campaigns()
        except OrigamiAPIError as e:
            assert "No campaigns to review" in str(e)
        else:
            raise AssertionError("expected OrigamiAPIError")


def _campaign(cid: str, workspace_id=None) -> OrigamiCampaign:
    return OrigamiCampaign(
        id=cid,
        slug=cid,
        name=f"Campaign {cid}",
        status="active",
        workspace_id=workspace_id,
    )


def _flatten_blocks(blocks):
    parts = []
    for b in blocks:
        if b["type"] in ("header", "section"):
            parts.append(b["text"]["text"])
        elif b["type"] == "context":
            for el in b.get("elements", []):
                parts.append(el.get("text", ""))
    return "\n".join(parts)
