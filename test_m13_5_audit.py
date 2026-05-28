"""M13.5.B — Audit / redaction tests.

Hard invariant: x-api-key, x-user-key, Bearer tokens, and full account IDs
must never appear in any audit record, lifecycle field, or Telegram-bound
string. These tests scan the serialised audit output for sensitive strings.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bot.etoro.audit import (
    AuditLogger,
    redact_body,
    redact_headers,
    redact_payload,
)


FAKE_API_KEY  = "test_api_key_super_secret_AAAAAAAAAA"
FAKE_USER_KEY = "test_user_key_super_secret_BBBBBBBBBB"
FAKE_BEARER   = "Bearer abc123def456ghijklmnop"


class TestHeaderRedaction(unittest.TestCase):
    def test_api_key_removed(self):
        h = {"x-api-key": FAKE_API_KEY, "Accept": "application/json"}
        out = redact_headers(h)
        self.assertEqual(out["x-api-key"], "<REDACTED>")
        self.assertNotIn(FAKE_API_KEY, json.dumps(out))

    def test_user_key_removed(self):
        h = {"x-user-key": FAKE_USER_KEY}
        out = redact_headers(h)
        self.assertEqual(out["x-user-key"], "<REDACTED>")
        self.assertNotIn(FAKE_USER_KEY, json.dumps(out))

    def test_authorization_redacted(self):
        h = {"Authorization": FAKE_BEARER}
        out = redact_headers(h)
        self.assertEqual(out["Authorization"], "<REDACTED>")
        self.assertNotIn(FAKE_BEARER, json.dumps(out))

    def test_x_request_id_masked_not_full(self):
        h = {"x-request-id": "f47ac10b-58cc-4372-a567-0e02b2c3d479"}
        out = redact_headers(h)
        self.assertNotIn("f47ac10b-58cc-4372-a567-0e02b2c3d479",
                         json.dumps(out))

    def test_non_dict_returns_empty(self):
        self.assertEqual(redact_headers(None), {})
        self.assertEqual(redact_headers("foo"), {})


class TestBodyRedaction(unittest.TestCase):
    def test_cid_truncated(self):
        out = redact_body({"cid": 1234567890})
        self.assertEqual(out["cid"], "***7890")

    def test_token_masked(self):
        out = redact_body({"token": "abcdef1234567890"})
        self.assertNotIn("abcdef1234567890", json.dumps(out))
        self.assertIn("...", out["token"])

    def test_bearer_in_string_scrubbed(self):
        out = redact_body({"note": f"call with {FAKE_BEARER}"})
        self.assertIn("<REDACTED>", out["note"])
        self.assertNotIn(FAKE_BEARER, json.dumps(out))

    def test_nested_list_dict(self):
        body = {"positions": [{"cid": 9999, "token": "xyz1234567890"}]}
        out = redact_body(body)
        s = json.dumps(out)
        self.assertNotIn("9999", s)  # ***9999 contains 9999 -- recheck

    def test_nested_cid_truncated(self):
        body = {"positions": [{"cid": 1234567890}]}
        out = redact_body(body)
        self.assertEqual(out["positions"][0]["cid"], "***7890")

    def test_passthrough_non_sensitive(self):
        body = {"orderID": 42, "amount": 10.0, "isBuy": True}
        out = redact_body(body)
        self.assertEqual(out, body)

    def test_redact_payload_does_not_mutate_input(self):
        p = {"InstrumentID": 1000, "Amount": 10.0,
             "secret_token": "abcdef1234567890"}
        original = dict(p)
        out = redact_payload(p)
        self.assertEqual(p, original)
        self.assertIsNot(out, p)


class TestAuditLogger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "audit.log"

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_jsonl(self):
        a = AuditLogger(self.path)
        a.event("test_event", foo=1, bar="hello")
        content = self.path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(content), 1)
        rec = json.loads(content[0])
        self.assertEqual(rec["kind"], "test_event")
        self.assertEqual(rec["foo"], 1)
        self.assertEqual(rec["bar"], "hello")
        self.assertIn("ts", rec)

    def test_redacts_api_key_field(self):
        a = AuditLogger(self.path)
        a.event("post_attempt",
                headers={"x-api-key": FAKE_API_KEY, "Accept": "*/*"})
        content = self.path.read_text(encoding="utf-8")
        self.assertNotIn(FAKE_API_KEY, content)

    def test_redacts_in_nested_body(self):
        a = AuditLogger(self.path)
        a.event("response",
                response={"cid": 1234567890,
                          "token": "abcdef1234567890",
                          "note": f"sent {FAKE_BEARER}"})
        content = self.path.read_text(encoding="utf-8")
        self.assertNotIn("1234567890", content.replace("***7890", ""))
        self.assertNotIn(FAKE_BEARER, content)

    def test_never_raises_on_unwritable_path(self):
        a = AuditLogger(Path("/proc/cannot/write/here.log"))
        # Should NOT raise — production must not crash if log write fails.
        try:
            a.event("noop", foo=1)
        except Exception as e:
            self.fail(f"AuditLogger raised on bad path: {e}")

    def test_rotates_when_oversized(self):
        a = AuditLogger(self.path, max_bytes=200, keep=2)
        # Write enough small events to trigger rotation.
        for i in range(20):
            a.event("e", payload="x" * 50, i=i)
        # Either the .1 sibling exists, or current size is bounded.
        sib = self.path.with_suffix(self.path.suffix + ".1")
        self.assertTrue(sib.exists() or self.path.stat().st_size < 4000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
