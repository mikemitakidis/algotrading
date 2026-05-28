"""M13.5.B — Order poller tests. No network."""
from __future__ import annotations

import unittest

from bot.etoro.order_poller import poll_until_terminal


class FakeClock:
    def __init__(self, start=0.0):
        self.t = float(start)
    def __call__(self):
        return self.t


class FakeSleeper:
    def __init__(self):
        self.calls = []
    def __call__(self, sec):
        self.calls.append(sec)


def _info(status_id, positions=None, error_code=None):
    return {
        "orderID": 10001,
        "statusID": status_id,
        "errorCode": error_code,
        "errorMessage": None,
        "instrumentID": 1000,
        "amount": 10,
        "units": 0.05,
        "requestOccurred": "2026-05-28T10:00:00Z",
        "positions": positions or [],
    }


def _position():
    return {
        "positionID": 999, "orderType": 1,
        "occurred": "2026-05-28T10:00:00Z",
        "rate": 200.0, "units": 0.05, "conversionRate": 1.0,
        "amount": 10.0, "isOpen": True,
    }


class TestPoller(unittest.TestCase):
    def test_immediate_fill(self):
        bodies = iter([_info(1, positions=[_position()])])
        reader = lambda oid: next(bodies)
        sleeper = FakeSleeper()
        r = poll_until_terminal(reader, 10001,
                                sleeper=sleeper, clock=FakeClock())
        self.assertEqual(r.status, "filled")
        self.assertEqual(r.attempts, 1)
        self.assertEqual(sleeper.calls, [])  # no sleep when first attempt wins

    def test_fill_on_third_attempt(self):
        bodies = iter([_info(0), _info(0), _info(1, positions=[_position()])])
        reader = lambda oid: next(bodies)
        sleeper = FakeSleeper()
        r = poll_until_terminal(reader, 10001,
                                sleeper=sleeper, clock=FakeClock())
        self.assertEqual(r.status, "filled")
        self.assertEqual(r.attempts, 3)
        # Sleep called between attempts 1->2 and 2->3
        self.assertEqual(sleeper.calls, [2.0, 2.0])

    def test_pending_status1_without_positions_is_not_filled(self):
        # statusID=1 (Executed) but positions[] empty -> keep polling.
        bodies = iter([_info(1, positions=[]), _info(1, positions=[_position()])])
        reader = lambda oid: next(bodies)
        sleeper = FakeSleeper()
        r = poll_until_terminal(reader, 10001,
                                sleeper=sleeper, clock=FakeClock())
        self.assertEqual(r.status, "filled")
        self.assertEqual(r.attempts, 2)

    def test_rejected_terminal(self):
        bodies = iter([_info(3)])
        reader = lambda oid: next(bodies)
        r = poll_until_terminal(reader, 10001,
                                sleeper=FakeSleeper(), clock=FakeClock())
        self.assertEqual(r.status, "broker_rejected")

    def test_cancelled_terminal(self):
        bodies = iter([_info(2)])
        reader = lambda oid: next(bodies)
        r = poll_until_terminal(reader, 10001,
                                sleeper=FakeSleeper(), clock=FakeClock())
        self.assertEqual(r.status, "cancelled")

    def test_exhaustion_returns_unverified(self):
        bodies = iter([_info(0), _info(0), _info(0), _info(0), _info(0)])
        reader = lambda oid: next(bodies)
        sleeper = FakeSleeper()
        r = poll_until_terminal(reader, 10001, max_attempts=5,
                                sleeper=sleeper, clock=FakeClock())
        self.assertEqual(r.status, "unverified")
        self.assertEqual(r.attempts, 5)
        self.assertEqual(len(sleeper.calls), 4)  # sleeps between 5 attempts

    def test_no_second_post_on_unverified(self):
        # Sanity: poller has no POST capability at all — it only takes
        # a reader callable. This test exists to lock in that contract.
        reader = lambda oid: _info(0)
        r = poll_until_terminal(reader, 10001, max_attempts=2,
                                sleeper=FakeSleeper(), clock=FakeClock())
        self.assertEqual(r.status, "unverified")

    def test_reader_exception_counts_as_attempt(self):
        attempts = [0]
        def reader(oid):
            attempts[0] += 1
            raise RuntimeError("network down")
        r = poll_until_terminal(reader, 10001, max_attempts=3,
                                sleeper=FakeSleeper(), clock=FakeClock())
        self.assertEqual(r.status, "unverified")
        self.assertEqual(r.attempts, 3)
        self.assertEqual(attempts[0], 3)
        self.assertIn("reader_exception", r.last_error)

    def test_parser_error_treated_as_attempt(self):
        # Reader returns garbage that parser will reject.
        reader = lambda oid: {"not": "valid"}
        r = poll_until_terminal(reader, 10001, max_attempts=2,
                                sleeper=FakeSleeper(), clock=FakeClock())
        self.assertEqual(r.status, "unverified")
        self.assertIn("parser_error", r.last_error)

    def test_zero_max_attempts_rejected(self):
        with self.assertRaises(ValueError):
            poll_until_terminal(lambda o: None, 10001, max_attempts=0)

    def test_invalid_order_id_rejected(self):
        with self.assertRaises(ValueError):
            poll_until_terminal(lambda o: None, -1)
        with self.assertRaises(ValueError):
            poll_until_terminal(lambda o: None, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
