"""M13.5.B — Nonce store tests."""
from __future__ import annotations

import unittest

from bot.etoro.nonce import (
    NonceStore, canonical_payload, compute_digest,
)


def _payload(amount=10.0, instr=1000):
    return {"InstrumentID": instr, "IsBuy": True, "Leverage": 1, "Amount": amount}


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.t = float(start)
    def __call__(self):
        return self.t


class TestCanonicalisation(unittest.TestCase):
    def test_key_order_independence(self):
        a = {"A": 1, "B": 2}
        b = {"B": 2, "A": 1}
        self.assertEqual(canonical_payload(a), canonical_payload(b))

    def test_non_dict_rejected(self):
        for x in ([], "x", 1, None):
            with self.assertRaises(TypeError):
                canonical_payload(x)


class TestDigest(unittest.TestCase):
    def test_digest_is_8_hex(self):
        d = compute_digest(_payload(), 1234)
        self.assertEqual(len(d), 8)
        self.assertTrue(all(c in "0123456789abcdef" for c in d))

    def test_digest_changes_with_payload(self):
        d1 = compute_digest(_payload(amount=10), 1234)
        d2 = compute_digest(_payload(amount=11), 1234)
        self.assertNotEqual(d1, d2)

    def test_digest_changes_with_timestamp(self):
        d1 = compute_digest(_payload(), 1000)
        d2 = compute_digest(_payload(), 2000)
        self.assertNotEqual(d1, d2)

    def test_negative_timestamp_rejected(self):
        with self.assertRaises(ValueError):
            compute_digest(_payload(), -1)

    def test_bool_timestamp_rejected(self):
        with self.assertRaises(TypeError):
            compute_digest(_payload(), True)


class TestNonceStore(unittest.TestCase):
    def test_issue_then_validate_ok(self):
        s = NonceStore(clock=FakeClock(1000.0))
        p = _payload()
        rec = s.issue(p, ttl_seconds=60)
        ok, reason = s.validate(f"CONFIRM {rec.digest}", p)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_single_use(self):
        c = FakeClock(1000.0)
        s = NonceStore(clock=c)
        p = _payload()
        rec = s.issue(p, ttl_seconds=60)
        ok1, _ = s.validate(f"CONFIRM {rec.digest}", p)
        ok2, reason2 = s.validate(f"CONFIRM {rec.digest}", p)
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertEqual(reason2, "nonce_consumed")

    def test_ttl_expiry(self):
        c = FakeClock(1000.0)
        s = NonceStore(clock=c)
        rec = s.issue(_payload(), ttl_seconds=60)
        c.t = 1000.0 + 61  # +61 s
        ok, reason = s.validate(f"CONFIRM {rec.digest}", _payload())
        self.assertFalse(ok)
        self.assertEqual(reason, "nonce_expired")

    def test_payload_mismatch(self):
        s = NonceStore(clock=FakeClock(1000.0))
        rec = s.issue(_payload(amount=10), ttl_seconds=60)
        ok, reason = s.validate(f"CONFIRM {rec.digest}",
                                _payload(amount=11))
        self.assertFalse(ok)
        self.assertEqual(reason, "payload_mismatch")

    def test_format_invalid_no_prefix(self):
        s = NonceStore(clock=FakeClock(1000.0))
        rec = s.issue(_payload(), ttl_seconds=60)
        ok, reason = s.validate(rec.digest, _payload())  # missing CONFIRM
        self.assertFalse(ok)
        self.assertEqual(reason, "format_invalid")

    def test_format_invalid_bad_hex(self):
        s = NonceStore()
        s.issue(_payload(), ttl_seconds=60)
        ok, reason = s.validate("CONFIRM zzzzzzzz", _payload())
        self.assertFalse(ok)
        self.assertEqual(reason, "format_invalid")

    def test_format_invalid_wrong_length(self):
        s = NonceStore()
        s.issue(_payload(), ttl_seconds=60)
        ok, reason = s.validate("CONFIRM 12345", _payload())  # 5 chars
        self.assertFalse(ok)
        self.assertEqual(reason, "format_invalid")

    def test_unknown_digest(self):
        s = NonceStore(clock=FakeClock(1000.0))
        ok, reason = s.validate("CONFIRM deadbeef", _payload())
        self.assertFalse(ok)
        self.assertEqual(reason, "nonce_unknown")

    def test_clock_skew_treated_as_expired(self):
        c = FakeClock(1000.0)
        s = NonceStore(clock=c)
        rec = s.issue(_payload(), ttl_seconds=60)
        c.t = 999.0  # clock went backwards
        ok, reason = s.validate(f"CONFIRM {rec.digest}", _payload())
        self.assertFalse(ok)
        self.assertEqual(reason, "nonce_expired")

    def test_ttl_zero_rejected(self):
        s = NonceStore()
        with self.assertRaises(ValueError):
            s.issue(_payload(), ttl_seconds=0)

    def test_non_string_echo_rejected(self):
        s = NonceStore()
        s.issue(_payload(), ttl_seconds=60)
        ok, reason = s.validate(None, _payload())
        self.assertFalse(ok)
        self.assertEqual(reason, "format_invalid")


if __name__ == "__main__":
    unittest.main(verbosity=2)
