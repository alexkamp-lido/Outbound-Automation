"""Signature verification tests for the Origami webhook receiver."""

import time

from services.origami_webhook import sign_test_payload, verify_origami_webhook

SECRET = "whsec_" + "A" * 43 + "="  # 32 raw bytes base64-encoded, base64 length 44 incl padding


def _fresh_ts() -> str:
    return str(int(time.time()))


class TestVerifyOrigamiWebhook:
    def test_valid_signature_accepted(self):
        body = b'{"type":"webhook.test","id":"01H"}'
        webhook_id = "wh-1"
        webhook_ts = _fresh_ts()
        sig = sign_test_payload(
            raw_body=body,
            webhook_id=webhook_id,
            webhook_timestamp=webhook_ts,
            secret=SECRET,
        )
        assert verify_origami_webhook(
            raw_body=body,
            webhook_id=webhook_id,
            webhook_timestamp=webhook_ts,
            webhook_signature=sig,
            secret=SECRET,
        )

    def test_bad_signature_rejected(self):
        body = b'{"type":"webhook.test"}'
        assert not verify_origami_webhook(
            raw_body=body,
            webhook_id="wh-1",
            webhook_timestamp=_fresh_ts(),
            webhook_signature="v1,not-a-real-signature",
            secret=SECRET,
        )

    def test_body_tampered_rejected(self):
        body = b'{"type":"webhook.test"}'
        sig = sign_test_payload(
            raw_body=body, webhook_id="wh-1",
            webhook_timestamp=_fresh_ts(), secret=SECRET,
        )
        assert not verify_origami_webhook(
            raw_body=b'{"type":"tampered"}',
            webhook_id="wh-1",
            webhook_timestamp=_fresh_ts(),
            webhook_signature=sig,
            secret=SECRET,
        )

    def test_timestamp_outside_replay_window_rejected(self):
        body = b'{"type":"webhook.test"}'
        old_ts = str(int(time.time()) - 3600)  # 1 hour ago
        sig = sign_test_payload(
            raw_body=body, webhook_id="wh-1",
            webhook_timestamp=old_ts, secret=SECRET,
        )
        assert not verify_origami_webhook(
            raw_body=body,
            webhook_id="wh-1",
            webhook_timestamp=old_ts,
            webhook_signature=sig,
            secret=SECRET,
        )

    def test_non_numeric_timestamp_rejected(self):
        assert not verify_origami_webhook(
            raw_body=b'',
            webhook_id="wh-1",
            webhook_timestamp="not-a-number",
            webhook_signature="v1,abc",
            secret=SECRET,
        )

    def test_missing_signature_header_rejected(self):
        assert not verify_origami_webhook(
            raw_body=b'',
            webhook_id="wh-1",
            webhook_timestamp=_fresh_ts(),
            webhook_signature="",
            secret=SECRET,
        )

    def test_multiple_signatures_any_match_accepts(self):
        body = b'{"ok":true}'
        webhook_id = "wh-9"
        webhook_ts = _fresh_ts()
        good = sign_test_payload(
            raw_body=body, webhook_id=webhook_id,
            webhook_timestamp=webhook_ts, secret=SECRET,
        )
        header = f"v1,notthisone v1,alsonot {good}"
        assert verify_origami_webhook(
            raw_body=body,
            webhook_id=webhook_id,
            webhook_timestamp=webhook_ts,
            webhook_signature=header,
            secret=SECRET,
        )
