"""test_audit_p1_data_rate_limit.py — audit-P1-data-rate-limit-fix.

Verifies the OLD yfinance provider path (used by the live scanner,
bot/backtest_v2, bot/backtest, and ml_build_dataset) now detects
swallowed yfinance rate-limit responses by inspecting
yf.shared._ERRORS after an empty DataFrame, mirroring the M16 fix
already in bot/historical/providers_yfinance.py.

Bug being fixed
───────────────
yfinance >= 0.2 catches per-symbol exceptions (including
YFRateLimitError) internally and stashes them in
yf.shared._ERRORS[symbol] while returning an empty DataFrame.
The pre-fix code only inspected str(exc) on raised exceptions, so:

* `_fetch_one` returned None on swallowed RL → caller treated as
  no_data → consec_rl counter never incremented → cache-only
  safety mode never engaged.
* `fetch_bars_range` returned ('empty_response') on swallowed RL →
  no retry-with-backoff → silent skip.
* `bot/backtest.py:_fetch_yf_single` had the same shape: returned
  ('empty_response') instead of retrying.

The fix mirrors the M16 pattern: clear _ERRORS before the call,
scan it after an empty DataFrame, route detected RL to the same
retry / classification path the raised-exception case uses.

Tests are no-network: yfinance.Ticker and the shared module are
mocked end-to-end.
"""
from __future__ import annotations

import datetime as _dt
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

from bot.providers.yfinance_provider import (
    RateLimitError,
    YFinanceProvider,
    _clear_yf_errors,
    _is_rate_limit_exception,
    _is_rate_limit_signal,
    _scan_yf_errors_for_other_error,
    _scan_yf_errors_for_rate_limit,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _empty_df() -> pd.DataFrame:
    """An empty DataFrame shaped like yfinance returns on RL."""
    return pd.DataFrame()


def _ok_df(n: int = 50) -> pd.DataFrame:
    """A minimum-viable OHLCV DataFrame with n rows."""
    idx = pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "Open":   [100.0] * n,
            "High":   [101.0] * n,
            "Low":    [ 99.0] * n,
            "Close":  [100.5] * n,
            "Volume": [1000]  * n,
        },
        index=idx,
    )


class _FakeShared:
    """A stand-in for `yfinance.shared` with a mutable `_ERRORS` dict."""
    def __init__(self):
        self._ERRORS = {}


class _FakeTicker:
    """Minimal yfinance.Ticker stub. .history() is a MagicMock so
    individual tests can set return_value / side_effect."""
    def __init__(self, sym, session=None):
        self.sym = sym
        self.history = MagicMock()


# ─────────────────────────────────────────────────────────────────────────────
# G1 — _fetch_one (live scanner path)
# ─────────────────────────────────────────────────────────────────────────────

class TestFetchOneScannerPath(unittest.TestCase):

    def setUp(self):
        self.prov = YFinanceProvider()

    def test_fetch_one_raises_rate_limit_on_isinstance_match(self):
        """yfinance raises a fake YFRateLimitError → RateLimitError."""
        class FakeYFRateLimitError(Exception):
            pass
        FakeYFRateLimitError.__name__ = "YFRateLimitError"

        ticker = _FakeTicker("AAPL")
        ticker.history.side_effect = FakeYFRateLimitError(
            "Too Many Requests. Rate limited. Try after a while.")

        fake_shared = _FakeShared()
        with patch("bot.providers.yfinance_provider.yf") as fake_yf:
            fake_yf.Ticker = MagicMock(return_value=ticker)
            fake_yf.shared = fake_shared
            with self.assertRaises(RateLimitError):
                self.prov._fetch_one("AAPL", "60d", "1d")

    def test_fetch_one_detects_rate_limit_in_yf_shared_errors_after_empty_df(self):
        """yfinance swallows RL into _ERRORS + returns empty df →
        RateLimitError raised. SMOKING-GUN for the audit-P1 bug.

        Before this fix, the provider returned None and the caller
        treated it as no_data → consec_rl counter never incremented
        → cache-only safety mode never engaged.
        """
        fake_shared = _FakeShared()

        # Simulate yfinance's actual behaviour: when .history() is
        # called, yfinance internally catches the rate-limit
        # exception, stashes it in shared._ERRORS, and returns an
        # empty DataFrame. The provider clears _ERRORS BEFORE the
        # call, so we must populate it AT call time via side_effect.
        def history_impl(*args, **kwargs):
            fake_shared._ERRORS["AAPL"] = (
                "YFRateLimitError('Too Many Requests. Rate limited.')"
            )
            return _empty_df()

        ticker = _FakeTicker("AAPL")
        ticker.history.side_effect = history_impl

        with patch("bot.providers.yfinance_provider.yf") as fake_yf:
            fake_yf.Ticker = MagicMock(return_value=ticker)
            fake_yf.shared = fake_shared
            with self.assertRaises(RateLimitError) as ctx:
                self.prov._fetch_one("AAPL", "60d", "1d")
            self.assertIn("Rate", str(ctx.exception))

    def test_fetch_one_returns_none_for_genuinely_empty_df(self):
        """Empty df + clean _ERRORS → None (no_data). No false positive."""
        ticker = _FakeTicker("AAPL")
        ticker.history.return_value = _empty_df()

        fake_shared = _FakeShared()  # _ERRORS = {}

        with patch("bot.providers.yfinance_provider.yf") as fake_yf:
            fake_yf.Ticker = MagicMock(return_value=ticker)
            fake_yf.shared = fake_shared
            result = self.prov._fetch_one("AAPL", "60d", "1d")
            self.assertIsNone(result)

    def test_fetch_one_clears_yf_errors_before_call(self):
        """Pre-seeded _ERRORS from a prior call must NOT leak into
        this call's classification — the provider must clear it
        first. We seed _ERRORS with a stale RL entry; then mock
        .history() to return a valid df (no RL on this call). The
        method must return the df (NOT raise), proving the stale
        entry was cleared before scanning."""
        ticker = _FakeTicker("AAPL")
        ticker.history.return_value = _ok_df(50)

        fake_shared = _FakeShared()
        fake_shared._ERRORS = {
            "AAPL": "YFRateLimitError('stale entry from a prior call')"
        }

        with patch("bot.providers.yfinance_provider.yf") as fake_yf:
            fake_yf.Ticker = MagicMock(return_value=ticker)
            fake_yf.shared = fake_shared
            result = self.prov._fetch_one("AAPL", "60d", "1d")
            self.assertIsNotNone(result)
            self.assertEqual(len(result), 50)
            # After the call, _ERRORS was cleared (then .history()
            # didn't repopulate it).
            self.assertEqual(fake_shared._ERRORS, {})


# ─────────────────────────────────────────────────────────────────────────────
# G2 — fetch_bars_range (backtest_v2 path)
# ─────────────────────────────────────────────────────────────────────────────

class TestFetchBarsRangeBacktestPath(unittest.TestCase):

    def setUp(self):
        self.prov = YFinanceProvider()
        self.start = _dt.date(2026, 1, 1)
        self.end   = _dt.date(2026, 2, 1)

    def _patch_yf_with_history_side_effects(self, side_effects, errors_per_call):
        """Helper: yields a context that patches `yf` so each call to
        .history() returns the next item from `side_effects` and
        sets shared._ERRORS to errors_per_call[i] before that return.

        `side_effects[i]` is either a DataFrame or an Exception
        instance (to be raised).
        """
        ticker = _FakeTicker("AAPL")
        fake_shared = _FakeShared()
        call_count = {"n": 0}

        def history_impl(*args, **kwargs):
            i = call_count["n"]
            call_count["n"] += 1
            # The provider clears _ERRORS before each call.
            # Simulate yfinance populating _ERRORS during the call.
            errs = errors_per_call[i] if i < len(errors_per_call) else {}
            fake_shared._ERRORS.update(errs)
            r = side_effects[i] if i < len(side_effects) else _empty_df()
            if isinstance(r, BaseException):
                raise r
            return r

        ticker.history.side_effect = history_impl

        patcher = patch("bot.providers.yfinance_provider.yf")
        fake_yf = patcher.start()
        fake_yf.Ticker = MagicMock(return_value=ticker)
        fake_yf.shared = fake_shared
        return patcher, fake_yf, fake_shared, call_count

    def test_fetch_bars_range_retries_on_swallowed_rate_limit(self):
        """Attempt 1: empty df + _ERRORS has RL → must retry, not
        return 'empty_response' early. Attempt 2: same again.
        Attempt 3: succeed."""
        patcher, fake_yf, fake_shared, call_count = (
            self._patch_yf_with_history_side_effects(
                side_effects=[_empty_df(), _empty_df(), _ok_df(50)],
                errors_per_call=[
                    {"AAPL": "YFRateLimitError(...)"},
                    {"AAPL": "YFRateLimitError(...)"},
                    {},
                ],
            )
        )
        try:
            # Patch time.sleep to keep the 12s/24s backoffs from
            # actually blocking the test.
            with patch("bot.providers.yfinance_provider.time.sleep"):
                df, status = self.prov.fetch_bars_range(
                    "AAPL", "1d", self.start, self.end)
            self.assertEqual(status, "ok")
            self.assertIsNotNone(df)
            self.assertEqual(call_count["n"], 3,
                             "must have retried twice before succeeding")
        finally:
            patcher.stop()

    def test_fetch_bars_range_returns_rate_limited_after_3_swallowed_rl(self):
        """All 3 attempts hit swallowed RL → (None, 'rate_limited').
        Before the fix this returned ('empty_response') on the
        first attempt with no retry."""
        patcher, fake_yf, fake_shared, call_count = (
            self._patch_yf_with_history_side_effects(
                side_effects=[_empty_df()] * 3,
                errors_per_call=[
                    {"AAPL": "YFRateLimitError(...)"},
                    {"AAPL": "YFRateLimitError(...)"},
                    {"AAPL": "YFRateLimitError(...)"},
                ],
            )
        )
        try:
            with patch("bot.providers.yfinance_provider.time.sleep"):
                df, status = self.prov.fetch_bars_range(
                    "AAPL", "1d", self.start, self.end)
            self.assertIsNone(df)
            self.assertEqual(status, "rate_limited")
            self.assertEqual(call_count["n"], 3)
        finally:
            patcher.stop()

    def test_fetch_bars_range_still_returns_empty_response_for_clean_empty_df(self):
        """Empty df + clean _ERRORS → ('empty_response'). No false
        positive: genuinely-empty must not be reclassified as RL."""
        patcher, fake_yf, fake_shared, call_count = (
            self._patch_yf_with_history_side_effects(
                side_effects=[_empty_df()],
                errors_per_call=[{}],
            )
        )
        try:
            with patch("bot.providers.yfinance_provider.time.sleep"):
                df, status = self.prov.fetch_bars_range(
                    "AAPL", "1d", self.start, self.end)
            self.assertIsNone(df)
            self.assertEqual(status, "empty_response")
            # No retry — empty_response returns on the first attempt.
            self.assertEqual(call_count["n"], 1)
        finally:
            patcher.stop()

    def test_fetch_bars_range_raised_rate_limit_layered_detection(self):
        """The raised-exception path uses the same layered detection.
        A fake YFRateLimitError-typed exception → retries + ultimately
        returns 'rate_limited'."""
        class FakeYFRateLimitError(Exception):
            pass
        FakeYFRateLimitError.__name__ = "YFRateLimitError"

        patcher, fake_yf, fake_shared, call_count = (
            self._patch_yf_with_history_side_effects(
                side_effects=[
                    FakeYFRateLimitError("Too Many Requests"),
                    FakeYFRateLimitError("Too Many Requests"),
                    FakeYFRateLimitError("Too Many Requests"),
                ],
                errors_per_call=[{}, {}, {}],
            )
        )
        try:
            with patch("bot.providers.yfinance_provider.time.sleep"):
                df, status = self.prov.fetch_bars_range(
                    "AAPL", "1d", self.start, self.end)
            self.assertIsNone(df)
            self.assertEqual(status, "rate_limited")
            self.assertEqual(call_count["n"], 3)
        finally:
            patcher.stop()


# ─────────────────────────────────────────────────────────────────────────────
# G3 — bot/backtest.py:_fetch_yf_single
# ─────────────────────────────────────────────────────────────────────────────

class TestBacktestFetchYFHistory(unittest.TestCase):
    """The bot/backtest.py path imports yfinance directly (bypassing
    the provider abstraction) and is patched in-place using the same
    shared helpers."""

    def setUp(self):
        self.start = _dt.date(2026, 1, 1)
        self.end   = _dt.date(2026, 2, 1)

    def _run(self, history_returns, errors_per_call):
        """Invoke bot.backtest._fetch_yf_single with mocked yfinance.

        _fetch_yf_single does `import yfinance as yf` INSIDE the
        function body (not at module-level), so patching
        bot.backtest.yf has no effect. We patch sys.modules['yfinance']
        so that local import returns our fake. We also patch
        bot.providers.yfinance_provider.yf because the helpers
        imported inside _fetch_yf_single read yf.shared through that
        module's `yf` binding.
        """
        import sys
        import bot.backtest as bt

        ticker = MagicMock()
        fake_shared = _FakeShared()
        call_count = {"n": 0}

        def history_impl(*args, **kwargs):
            i = call_count["n"]
            call_count["n"] += 1
            errs = errors_per_call[i] if i < len(errors_per_call) else {}
            fake_shared._ERRORS.update(errs)
            r = history_returns[i] if i < len(history_returns) else _empty_df()
            if isinstance(r, BaseException):
                raise r
            return r

        ticker.history.side_effect = history_impl

        fake_yf = MagicMock()
        fake_yf.Ticker = MagicMock(return_value=ticker)
        fake_yf.shared = fake_shared

        with patch.dict(sys.modules, {"yfinance": fake_yf}), \
             patch("bot.providers.yfinance_provider.yf", fake_yf), \
             patch("bot.backtest.time.sleep"), \
             patch("bot.backtest._bt_cache_load", return_value=None), \
             patch("bot.backtest._live_cache_load", return_value=None), \
             patch("bot.backtest._bt_cache_save"):
            df, status = bt._fetch_yf_single(
                "AAPL", self.start, self.end, "1d",
                progress_cb=None, token=None,
            )
        return df, status, call_count["n"]

    def test_backtest_fetch_retries_on_swallowed_rate_limit(self):
        df, status, n_calls = self._run(
            history_returns=[_empty_df(), _empty_df(), _ok_df(150)],
            errors_per_call=[
                {"AAPL": "YFRateLimitError(...)"},
                {"AAPL": "YFRateLimitError(...)"},
                {},
            ],
        )
        self.assertEqual(status, "ok")
        self.assertIsNotNone(df)
        self.assertEqual(n_calls, 3)

    def test_backtest_fetch_classifies_swallowed_rl_as_rate_limited(self):
        """After 3 attempts of swallowed RL → (None, 'rate_limited').
        Pre-fix would have returned (None, 'empty_response') on the
        first attempt with NO retry."""
        df, status, n_calls = self._run(
            history_returns=[_empty_df()] * 3,
            errors_per_call=[
                {"AAPL": "YFRateLimitError(...)"},
                {"AAPL": "YFRateLimitError(...)"},
                {"AAPL": "YFRateLimitError(...)"},
            ],
        )
        self.assertIsNone(df)
        self.assertEqual(status, "rate_limited")
        self.assertEqual(n_calls, 3)

    def test_backtest_fetch_still_returns_empty_response_for_clean_empty_df(self):
        df, status, n_calls = self._run(
            history_returns=[_empty_df()],
            errors_per_call=[{}],
        )
        self.assertIsNone(df)
        self.assertEqual(status, "empty_response")
        self.assertEqual(n_calls, 1)


# ─────────────────────────────────────────────────────────────────────────────
# G4 — Shared helper unit tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSharedHelpers(unittest.TestCase):

    def test_is_rate_limit_signal_matches_documented_tokens(self):
        for s in ("YFRateLimitError(...)", "Too Many Requests",
                   "rate limit hit", "rate-limit", "rate limited",
                   "429 Too Many", "yfratelimiterror"):
            self.assertTrue(_is_rate_limit_signal(s),
                            f"expected match: {s!r}")

    def test_is_rate_limit_signal_rejects_benign_strings(self):
        for s in ("", None, "connection refused", "timeout",
                   "no data", "200 OK", "value error"):
            self.assertFalse(_is_rate_limit_signal(s),
                             f"expected non-match: {s!r}")

    def test_is_rate_limit_signal_handles_non_string_input(self):
        # A non-stringifiable object must not raise.
        class Weird:
            def __str__(self):
                raise RuntimeError("nope")
        try:
            result = _is_rate_limit_signal(Weird())
        except Exception as e:
            self.fail(f"_is_rate_limit_signal raised: {e}")
        self.assertFalse(result)

    def test_scan_yf_errors_returns_none_for_clean_errors(self):
        fake_yf = SimpleNamespace(shared=SimpleNamespace(_ERRORS={}))
        self.assertIsNone(_scan_yf_errors_for_rate_limit(fake_yf, "AAPL"))

    def test_scan_yf_errors_finds_rate_limit_for_target_symbol(self):
        fake_yf = SimpleNamespace(shared=SimpleNamespace(_ERRORS={
            "AAPL": "YFRateLimitError('boom')",
        }))
        msg = _scan_yf_errors_for_rate_limit(fake_yf, "AAPL")
        self.assertIsNotNone(msg)
        self.assertIn("Rate", msg)

    def test_scan_yf_errors_finds_rate_limit_for_other_symbol(self):
        """Registry is global; an entry for OTHER (since we cleared
        before THIS call) still belongs to this call."""
        fake_yf = SimpleNamespace(shared=SimpleNamespace(_ERRORS={
            "OTHER": "Too Many Requests",
        }))
        msg = _scan_yf_errors_for_rate_limit(fake_yf, "AAPL")
        self.assertIsNotNone(msg)

    def test_scan_yf_errors_for_other_error_ignores_rate_limit(self):
        fake_yf = SimpleNamespace(shared=SimpleNamespace(_ERRORS={
            "AAPL": "YFRateLimitError(...)",
        }))
        self.assertIsNone(
            _scan_yf_errors_for_other_error(fake_yf, "AAPL"))

    def test_scan_yf_errors_for_other_error_returns_non_rl(self):
        fake_yf = SimpleNamespace(shared=SimpleNamespace(_ERRORS={
            "AAPL": "ConnectionError: name resolution failed",
        }))
        msg = _scan_yf_errors_for_other_error(fake_yf, "AAPL")
        self.assertIsNotNone(msg)
        self.assertIn("Connection", msg)

    def test_clear_yf_errors_is_idempotent_and_defensive(self):
        # Module without `shared` — no raise.
        bare = SimpleNamespace()
        _clear_yf_errors(bare)

        # `shared` without `_ERRORS` — no raise.
        shared_only = SimpleNamespace(shared=SimpleNamespace())
        _clear_yf_errors(shared_only)

        # Normal case clears.
        fake = SimpleNamespace(shared=SimpleNamespace(_ERRORS={"X": "y"}))
        _clear_yf_errors(fake)
        self.assertEqual(fake.shared._ERRORS, {})

        # Double-clear is safe.
        _clear_yf_errors(fake)
        self.assertEqual(fake.shared._ERRORS, {})

    def test_is_rate_limit_exception_isinstance_path(self):
        class FakeRL(Exception):
            pass
        FakeRL.__name__ = "YFRateLimitError"
        self.assertTrue(_is_rate_limit_exception(FakeRL("boom")))

    def test_is_rate_limit_exception_substring_path(self):
        self.assertTrue(_is_rate_limit_exception(
            ValueError("yfinance returned 429 Too Many Requests")))

    def test_is_rate_limit_exception_rejects_benign(self):
        self.assertFalse(_is_rate_limit_exception(
            ConnectionError("name resolution failed")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
