"""M13.5.B — EtoroLiveBroker tests.

Covers:
  * submit() (BrokerAdapter interface) raises OperatorConfirmationRequired
    — defends scanner isolation.
  * Constructor requires env_live_enabled=True.
  * preflight() validates the policy first (ChatGPT audit), then runs the
    ordered gates; each gate failure raises the right typed error.
  * submit_live() happy path with a mocked transport (NO network).
  * submit_live() HTTP error mapping (401/403/404/429/4xx/5xx).
  * submit_live() requires a valid nonce.
  * fetch_order_info() GET helper parses JSON.

No real eToro endpoint is contacted. The transport is always injected.
"""
from __future__ import annotations

import copy
import json
import unittest

from bot.broker_allocation import DEFAULT_POLICY
from bot.etoro.nonce import NonceStore
from bot.etoro.live_broker import (
    EtoroLiveBroker,
    LiveWriteContext,
    OperatorConfirmationRequired,
    PreflightOk,
    AmountTooSmall,
    BrokerDisabled,
    BrokerKillSwitch,
    BrokerNotAllowed,
    DailyLossBreached,
    DailyLossUnknown,
    EtoroLiveDisabled,
    EtoroLiveDisabledEnv,
    ExceedsBrokerCapital,
    ExceedsGlobalCapital,
    ExceedsOpenPositions,
    ExceedsSingleTrade,
    GlobalDisabled,
    GlobalKillSwitch,
    MarketClosed,
    PolicyInvalid,
    PolicyMissing,
    SpreadTooWide,
    StaleQuote,
)
from bot.etoro.errors import (
    EtoroAuthError,
    EtoroRateLimitError,
    EtoroRouteError,
    EtoroTransientError,
    EtoroValidationError,
)


def _live_policy():
    """A fully live-enabled, valid policy for etoro_real."""
    p = copy.deepcopy(DEFAULT_POLICY)
    p["global"]["auto_trading_enabled"] = True
    p["global"]["max_auto_trading_capital"] = 1000.0
    p["global"]["kill_switch"] = False
    p["etoro"]["auto_trading_enabled"] = True
    p["etoro"]["max_auto_trading_capital"] = 500.0
    p["etoro"]["max_single_trade_amount"] = 100.0
    p["etoro"]["max_daily_loss"] = 200.0
    p["etoro"]["max_open_positions"] = 5
    p["etoro"]["kill_switch"] = False
    p["routing"]["allowed_brokers"] = ["paper", "ibkr_paper", "ibkr_live",
                                       "etoro_paper", "etoro_real"]
    p["routing"]["etoro_live_enabled"] = True
    return p


def _payload(amount=10.0, instr=1000):
    return {"InstrumentID": instr, "IsBuy": True, "Leverage": 1,
            "Amount": amount, "IsNoStopLoss": True, "IsNoTakeProfit": True}


def _ctx(policy=None, payload=None, **overrides):
    base = dict(
        policy=policy if policy is not None else _live_policy(),
        payload=payload if payload is not None else _payload(),
        env_live_enabled=True,
        open_positions_count=0,
        realised_daily_loss=0.0,
        market_open=True,
        quote_age_sec=1.0,
        quote_max_age_sec=30.0,
        spread_bps=5.0,
        spread_max_bps=50.0,
        amount_min=10.0,
    )
    base.update(overrides)
    return LiveWriteContext(**base)


def _broker(transport=None, nonce_store=None):
    return EtoroLiveBroker(
        api_key="test-api-key",
        user_key="test-user-key",
        env_live_enabled=True,
        nonce_store=nonce_store,
        audit=None,
        transport=transport or (lambda *a, **k: (200, {}, b"{}")),
        base_url="https://public-api.etoro.example",  # never contacted
    )


def _open_ok_body(order_id=10001, status_id=0):
    return {
        "orderForOpen": {
            "instrumentID": 1000, "amount": 10, "isBuy": True,
            "leverage": 1, "orderID": order_id, "statusID": status_id,
            "openDateTime": "2026-05-28T10:00:00Z",
            "lastUpdate": "2026-05-28T10:00:00Z",
        },
        "token": "tok",
    }


class TestConstruction(unittest.TestCase):
    def test_requires_env_live_enabled(self):
        with self.assertRaises(EtoroLiveDisabledEnv):
            EtoroLiveBroker(api_key="k", user_key="u", env_live_enabled=False)

    def test_requires_keys(self):
        with self.assertRaises(ValueError):
            EtoroLiveBroker(api_key="", user_key="u", env_live_enabled=True)
        with self.assertRaises(ValueError):
            EtoroLiveBroker(api_key="k", user_key="", env_live_enabled=True)

    def test_name_and_is_live(self):
        b = _broker()
        self.assertEqual(b.name, "etoro_real")
        self.assertTrue(b.is_live)


class TestSubmitRaises(unittest.TestCase):
    def test_submit_raises_operator_confirmation_required(self):
        b = _broker()

        class _Intent:
            symbol = "SPY"; direction = "long"
        with self.assertRaises(OperatorConfirmationRequired):
            b.submit(_Intent())


class TestPreflightGates(unittest.TestCase):
    def test_happy_path_returns_ok(self):
        b = _broker()
        ok = b.preflight(_ctx())
        self.assertIsInstance(ok, PreflightOk)
        self.assertIn("etoro", ok.policy_snapshot)

    def test_policy_missing(self):
        b = _broker()
        with self.assertRaises(PolicyMissing):
            b.preflight(_ctx(policy="not a dict"))

    def test_policy_invalid(self):
        b = _broker()
        bad = _live_policy()
        bad["etoro"]["max_daily_loss"] = -5.0   # invalid
        with self.assertRaises(PolicyInvalid):
            b.preflight(_ctx(policy=bad))

    def test_global_kill_switch(self):
        b = _broker()
        p = _live_policy(); p["global"]["kill_switch"] = True
        with self.assertRaises(GlobalKillSwitch):
            b.preflight(_ctx(policy=p))

    def test_global_disabled(self):
        b = _broker()
        p = _live_policy(); p["global"]["auto_trading_enabled"] = False
        with self.assertRaises(GlobalDisabled):
            b.preflight(_ctx(policy=p))

    def test_broker_kill_switch(self):
        b = _broker()
        p = _live_policy(); p["etoro"]["kill_switch"] = True
        with self.assertRaises(BrokerKillSwitch):
            b.preflight(_ctx(policy=p))

    def test_broker_disabled(self):
        b = _broker()
        p = _live_policy(); p["etoro"]["auto_trading_enabled"] = False
        with self.assertRaises(BrokerDisabled):
            b.preflight(_ctx(policy=p))

    def test_broker_not_allowed(self):
        b = _broker()
        p = _live_policy()
        # Remove etoro_real from allowed_brokers AND clear the route
        # overrides that would otherwise orphan-fail validation, so the
        # policy is valid but etoro_real is simply not allowed.
        p["routing"]["allowed_brokers"] = ["paper", "etoro_paper"]
        p["routing"]["route_overrides"] = {}
        p["routing"]["default_broker"] = "paper"
        with self.assertRaises(BrokerNotAllowed):
            b.preflight(_ctx(policy=p))

    def test_etoro_live_disabled_policy(self):
        b = _broker()
        p = _live_policy(); p["routing"]["etoro_live_enabled"] = False
        with self.assertRaises(EtoroLiveDisabled):
            b.preflight(_ctx(policy=p))

    def test_etoro_live_enabled_truthy_not_true_is_rejected(self):
        # Strict identity: a truthy-but-not-True value must be rejected.
        b = _broker()
        p = _live_policy(); p["routing"]["etoro_live_enabled"] = 1  # truthy
        # validate_policy will reject non-bool first (PolicyInvalid),
        # which still blocks the live write — assert it does not pass.
        with self.assertRaises((EtoroLiveDisabled, PolicyInvalid)):
            b.preflight(_ctx(policy=p))

    def test_env_live_disabled(self):
        b = _broker()
        with self.assertRaises(EtoroLiveDisabledEnv):
            b.preflight(_ctx(env_live_enabled=False))

    def test_amount_too_small(self):
        b = _broker()
        with self.assertRaises(AmountTooSmall):
            b.preflight(_ctx(payload=_payload(amount=5.0), amount_min=10.0))

    def test_exceeds_single_trade(self):
        b = _broker()
        with self.assertRaises(ExceedsSingleTrade):
            b.preflight(_ctx(payload=_payload(amount=150.0)))  # cap 100

    def test_exceeds_broker_capital_branch_is_defensive(self):
        # The validator enforces single <= broker <= global capital, so an
        # Amount that exceeds the broker cap will ALWAYS be caught by the
        # single-trade gate first (single <= broker). This documents that
        # the ExceedsBrokerCapital branch is defensive-only and that an
        # over-broker-cap amount is rejected (as ExceedsSingleTrade).
        b = _broker()
        p = _live_policy()
        p["etoro"]["max_single_trade_amount"] = 50.0
        p["etoro"]["max_auto_trading_capital"] = 50.0
        with self.assertRaises(ExceedsSingleTrade):
            b.preflight(_ctx(policy=p, payload=_payload(amount=80.0)))

    def test_exceeds_global_capital_branch_is_defensive(self):
        # Likewise, global cap >= broker cap >= single cap is enforced by
        # the validator, so an over-global-cap amount is caught earlier.
        # This asserts the over-cap amount is rejected (single gate).
        b = _broker()
        p = _live_policy()
        p["global"]["max_auto_trading_capital"] = 40.0
        p["etoro"]["max_auto_trading_capital"] = 40.0
        p["etoro"]["max_single_trade_amount"] = 40.0
        with self.assertRaises(ExceedsSingleTrade):
            b.preflight(_ctx(policy=p, payload=_payload(amount=80.0)))

    def test_exceeds_open_positions(self):
        b = _broker()
        with self.assertRaises(ExceedsOpenPositions):
            b.preflight(_ctx(open_positions_count=5))  # cap 5

    def test_daily_loss_unknown_fails_closed(self):
        b = _broker()
        with self.assertRaises(DailyLossUnknown):
            b.preflight(_ctx(realised_daily_loss=None))

    def test_daily_loss_breached(self):
        b = _broker()
        with self.assertRaises(DailyLossBreached):
            b.preflight(_ctx(realised_daily_loss=200.0))  # cap 200

    def test_market_closed(self):
        b = _broker()
        with self.assertRaises(MarketClosed):
            b.preflight(_ctx(market_open=False))

    def test_stale_quote(self):
        b = _broker()
        with self.assertRaises(StaleQuote):
            b.preflight(_ctx(quote_age_sec=60.0, quote_max_age_sec=30.0))

    def test_stale_quote_none(self):
        b = _broker()
        with self.assertRaises(StaleQuote):
            b.preflight(_ctx(quote_age_sec=None))

    def test_spread_too_wide(self):
        b = _broker()
        with self.assertRaises(SpreadTooWide):
            b.preflight(_ctx(spread_bps=100.0, spread_max_bps=50.0))


class TestSubmitLive(unittest.TestCase):
    def _prep_nonce(self, payload):
        store = NonceStore()
        rec = store.issue(payload, ttl_seconds=300)
        return store, f"CONFIRM {rec.digest}"

    def test_happy_path(self):
        payload = _payload()
        store, confirm = self._prep_nonce(payload)
        calls = []

        def transport(url, method, headers, body, timeout):
            calls.append((url, method, headers, body))
            return 200, {}, json.dumps(_open_ok_body()).encode("utf-8")

        b = _broker(transport=transport, nonce_store=store)
        parsed, audit_record = b.submit_live(payload, _ctx(payload=payload),
                                              confirm)
        self.assertEqual(parsed.order_id, 10001)
        self.assertEqual(len(calls), 1)               # exactly one POST
        self.assertEqual(calls[0][1], "POST")
        # x-request-id is a fresh UUID in headers
        self.assertIn("x-request-id", calls[0][2])
        self.assertEqual(audit_record["http_status"], 200)

    def test_no_nonce_store_refuses(self):
        payload = _payload()
        b = _broker(transport=lambda *a, **k: (200, {}, b"{}"),
                    nonce_store=None)
        with self.assertRaises(OperatorConfirmationRequired):
            b.submit_live(payload, _ctx(payload=payload), "CONFIRM deadbeef")

    def test_bad_confirmation_refuses(self):
        payload = _payload()
        store, _ = self._prep_nonce(payload)
        b = _broker(transport=lambda *a, **k: (200, {}, b"{}"),
                    nonce_store=store)
        with self.assertRaises(OperatorConfirmationRequired):
            b.submit_live(payload, _ctx(payload=payload), "CONFIRM 00000000")

    def test_preflight_runs_before_post(self):
        # If preflight fails, transport must never be called.
        payload = _payload(amount=5.0)  # too small
        store, confirm = self._prep_nonce(payload)
        called = []
        b = _broker(transport=lambda *a, **k: called.append(1) or (200, {}, b"{}"),
                    nonce_store=store)
        with self.assertRaises(AmountTooSmall):
            b.submit_live(payload, _ctx(payload=payload, amount_min=10.0),
                          confirm)
        self.assertEqual(called, [])

    def _error_case(self, status, exc, body=b"{}"):
        payload = _payload()
        store, confirm = self._prep_nonce(payload)
        b = _broker(transport=lambda *a, **k: (status, {}, body),
                    nonce_store=store)
        with self.assertRaises(exc):
            b.submit_live(payload, _ctx(payload=payload), confirm)

    def test_401_auth_error(self):
        self._error_case(401, EtoroAuthError)

    def test_403_auth_error(self):
        self._error_case(403, EtoroAuthError)

    def test_404_route_error(self):
        self._error_case(404, EtoroRouteError)

    def test_429_rate_limited(self):
        self._error_case(429, EtoroRateLimitError)

    def test_422_validation_error(self):
        self._error_case(422, EtoroValidationError)

    def test_500_transient_error(self):
        self._error_case(500, EtoroTransientError)

    def test_no_retry_on_500(self):
        # A single POST, then raise — no second attempt.
        payload = _payload()
        store, confirm = self._prep_nonce(payload)
        calls = []
        b = _broker(transport=lambda *a, **k: calls.append(1) or (500, {}, b"{}"),
                    nonce_store=store)
        with self.assertRaises(EtoroTransientError):
            b.submit_live(payload, _ctx(payload=payload), confirm)
        self.assertEqual(len(calls), 1)


class TestFetchOrderInfo(unittest.TestCase):
    def test_get_parses_json(self):
        body = {"orderID": 1, "statusID": 0, "positions": []}
        b = _broker(transport=lambda *a, **k: (200, {}, json.dumps(body).encode()))
        out = b.fetch_order_info(1)
        self.assertEqual(out["orderID"], 1)

    def test_get_auth_error(self):
        b = _broker(transport=lambda *a, **k: (401, {}, b"{}"))
        with self.assertRaises(EtoroAuthError):
            b.fetch_order_info(1)

    def test_get_invalid_order_id(self):
        b = _broker()
        with self.assertRaises(ValueError):
            b.fetch_order_info(0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
