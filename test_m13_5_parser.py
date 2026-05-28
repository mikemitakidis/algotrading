"""M13.5.B — Response parser tests against documented OpenAPI shapes."""
from __future__ import annotations

import unittest

from bot.etoro.response_parser import (
    KNOWN_STATUS_IDS,
    ParserError,
    parse_error,
    parse_open_response,
    parse_order_info,
)


def _open_body(order_id=10001, status_id=0):
    return {
        "orderForOpen": {
            "instrumentID":       1000,
            "amount":             10,
            "isBuy":              True,
            "leverage":           1,
            "stopLossRate":       0,
            "takeProfitRate":     0,
            "isTslEnabled":       False,
            "mirrorID":           0,
            "totalExternalCosts": 0,
            "orderID":            order_id,
            "orderType":          1,
            "statusID":           status_id,
            "CID":                1234567890,
            "openDateTime":       "2026-05-28T10:00:00Z",
            "lastUpdate":         "2026-05-28T10:00:00Z",
        },
        "token": "abc-tracking-token",
    }


def _info_body(order_id=10001, status_id=1, positions=None, error_code=None):
    return {
        "token":           "track-xyz",
        "orderID":         order_id,
        "cid":             1234567890,
        "referenceID":     "ref-1",
        "statusID":        status_id,
        "orderType":       1,
        "openActionType":  0,
        "errorCode":       error_code,
        "errorMessage":    None,
        "instrumentID":    1000,
        "amount":          10,
        "units":           0.05,
        "requestOccurred": "2026-05-28T10:00:00Z",
        "positions":       positions or [],
    }


def _position(position_id=999, rate=200.0, units=0.05, conv=1.0):
    return {
        "positionID":     position_id,
        "orderType":      1,
        "occurred":       "2026-05-28T10:00:01Z",
        "rate":           rate,
        "units":          units,
        "conversionRate": conv,
        "amount":         10.0,
        "isOpen":         True,
    }


class TestOpenResponse(unittest.TestCase):
    def test_success_parse(self):
        r = parse_open_response(_open_body())
        self.assertEqual(r.order_id, 10001)
        self.assertEqual(r.status_id, 0)
        self.assertEqual(r.internal_status, "submitted")
        self.assertEqual(r.amount, 10.0)
        self.assertTrue(r.is_buy)
        self.assertEqual(r.leverage, 1)

    def test_missing_orderForOpen_rejected(self):
        with self.assertRaises(ParserError):
            parse_open_response({"token": "x"})

    def test_unknown_status_rejected(self):
        b = _open_body()
        b["orderForOpen"]["statusID"] = 99
        with self.assertRaises(ParserError):
            parse_open_response(b)

    def test_non_dict_rejected(self):
        for x in (None, [], "x", 1):
            with self.assertRaises(ParserError):
                parse_open_response(x)


class TestOrderInfo(unittest.TestCase):
    def test_executed_with_position(self):
        r = parse_order_info(_info_body(status_id=1,
                                        positions=[_position()]))
        self.assertEqual(r.internal_status, "filled")
        self.assertTrue(r.has_positions)
        self.assertEqual(r.first_position_id, 999)
        self.assertEqual(r.first_position_rate, 200.0)
        self.assertEqual(r.first_position_units, 0.05)
        self.assertEqual(r.first_position_conversion_rate, 1.0)

    def test_pending_no_positions(self):
        r = parse_order_info(_info_body(status_id=0))
        self.assertEqual(r.internal_status, "submitted")
        self.assertFalse(r.has_positions)
        self.assertIsNone(r.first_position_id)

    def test_cancelled(self):
        r = parse_order_info(_info_body(status_id=2))
        self.assertEqual(r.internal_status, "cancelled")

    def test_rejected(self):
        r = parse_order_info(_info_body(status_id=3))
        self.assertEqual(r.internal_status, "broker_rejected")

    def test_partial_executed(self):
        r = parse_order_info(_info_body(status_id=4))
        self.assertEqual(r.internal_status, "submitted")

    def test_error_code_forces_broker_rejected(self):
        # statusID says pending but errorCode is set -> broker_rejected.
        r = parse_order_info(_info_body(status_id=0, error_code=12345))
        self.assertEqual(r.internal_status, "broker_rejected")
        self.assertEqual(r.error_code, 12345)

    def test_unknown_status_rejected(self):
        with self.assertRaises(ParserError):
            parse_order_info(_info_body(status_id=999))

    def test_positions_must_be_list(self):
        b = _info_body()
        b["positions"] = {"oops": True}
        with self.assertRaises(ParserError):
            parse_order_info(b)


class TestErrorParser(unittest.TestCase):
    def test_dict_with_errorCode(self):
        out = parse_error(400, {"errorCode": 100, "errorMessage": "bad"})
        self.assertEqual(out["http_status"], 400)
        self.assertEqual(out["errorCode"], 100)
        self.assertEqual(out["errorMessage"], "bad")

    def test_nested_error_object(self):
        out = parse_error(500,
                          {"error": {"code": 1, "message": "x", "type": "y"}})
        self.assertEqual(out["error_code"], 1)
        self.assertEqual(out["error_message"], "x")

    def test_text_body_capped(self):
        out = parse_error(503, "x" * 1000)
        self.assertEqual(out["http_status"], 503)
        self.assertIn("raw_text", out)
        self.assertLessEqual(len(out["raw_text"]), 500)

    def test_empty_body(self):
        out = parse_error(429, None)
        self.assertEqual(out["http_status"], 429)


class TestVocabularyCompleteness(unittest.TestCase):
    def test_all_documented_status_ids(self):
        # From OpenAPI: 0=Pending, 1=Executed, 2=Cancelled, 3=Rejected,
        # 4=Partially Executed.
        self.assertEqual(KNOWN_STATUS_IDS, {0, 1, 2, 3, 4})


if __name__ == "__main__":
    unittest.main(verbosity=2)
