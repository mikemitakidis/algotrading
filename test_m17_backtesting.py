"""test_m17_backtesting.py — M17.A backtesting engine tests.

Single test file for the entire M17 sub-milestone. Grown incrementally
phase by phase. Groups:

  G1  — Config validation (Phase 1)
  G2  — Data loader (Phase 2)
  G3  — Indicators (Phase 3)
  G4  — Strategy + look-ahead protection (Phase 4)
  G5  — Execution timing (Phase 5)
  G6  — Stop loss / take profit (Phase 5)
  G7  — Position sizing (Phase 5)
  G8  — Metrics (Phase 6)
  G9  — Output reproducibility + golden-path E2E (Phases 7+8)
  G10 — Hygiene / AST / no-network / protected-files (Phase 9)

All tests are no-network: nothing touches yfinance, no socket calls,
no broker imports, no order paths. AST-asserted in G10.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from bot.backtesting import ENGINE_VERSION
from bot.backtesting.config import (
    BacktestConfig,
    BacktestRequest,
    DataConfig,
    ExecutionConfig,
    StrategyConfig,
    config_hash,
    config_to_dict,
    parse_config_dict,
    parse_config_file,
)
from bot.backtesting.errors import (
    BacktestError,
    ConfigError,
    MissingDataError,
    StrategyError,
)


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────

def _good_config_dict() -> dict:
    return {
        "request": {
            "symbol":    "AAPL",
            "timeframe": "1D",
            "start":     "2024-01-01",
            "end":       "2024-12-31",
        },
        "data": {"adjusted": True, "provider": "yfinance"},
        "strategy": {
            "name":   "sma_crossover",
            "params": {"fast_window": 20, "slow_window": 50},
        },
        "execution": {
            "initial_equity":     10000.0,
            "fee_bps":            5,
            "slippage_bps":       5,
            "stop_loss_pct":      0.03,
            "take_profit_pct":    0.06,
            "risk_per_trade_pct": 0.01,
            "max_position_pct":   0.25,
            "allow_short":        False,
        },
    }


# ─────────────────────────────────────────────────────────────────────
# G1 — Config validation
# ─────────────────────────────────────────────────────────────────────

class G1_ConfigValidation(unittest.TestCase):
    """Group 1: config validation. Every required field, every type
    check, every range check, every error message."""

    # ---- happy path -------------------------------------------------

    def test_good_dict_parses_to_BacktestConfig(self):
        cfg = parse_config_dict(_good_config_dict())
        self.assertIsInstance(cfg, BacktestConfig)
        self.assertEqual(cfg.request.symbol, "AAPL")
        self.assertEqual(cfg.request.timeframe, "1D")
        self.assertEqual(cfg.request.start, date(2024, 1, 1))
        self.assertEqual(cfg.request.end,   date(2024, 12, 31))
        self.assertTrue(cfg.data.adjusted)
        self.assertEqual(cfg.data.provider, "yfinance")
        self.assertEqual(cfg.strategy.name, "sma_crossover")
        self.assertEqual(cfg.strategy.params["fast_window"], 20)
        self.assertEqual(cfg.execution.initial_equity, 10000.0)
        self.assertFalse(cfg.execution.allow_short)

    def test_symbol_is_uppercased_and_stripped(self):
        d = _good_config_dict()
        d["request"]["symbol"] = "  aapl  "
        cfg = parse_config_dict(d)
        self.assertEqual(cfg.request.symbol, "AAPL")

    def test_data_block_defaults_when_absent(self):
        d = _good_config_dict()
        del d["data"]
        cfg = parse_config_dict(d)
        self.assertTrue(cfg.data.adjusted)
        self.assertEqual(cfg.data.provider, "yfinance")

    def test_execution_defaults_when_minimal(self):
        d = _good_config_dict()
        d["execution"] = {}
        cfg = parse_config_dict(d)
        self.assertEqual(cfg.execution.initial_equity, 10000.0)
        self.assertEqual(cfg.execution.fee_bps,        5.0)
        self.assertEqual(cfg.execution.slippage_bps,   5.0)
        self.assertEqual(cfg.execution.risk_per_trade_pct, 0.01)
        self.assertEqual(cfg.execution.max_position_pct,   0.25)
        self.assertIsNone(cfg.execution.stop_loss_pct)
        self.assertIsNone(cfg.execution.take_profit_pct)

    # ---- required-field omissions ------------------------------------

    def test_missing_request_raises(self):
        d = _good_config_dict(); del d["request"]
        with self.assertRaises(ConfigError) as ctx:
            parse_config_dict(d)
        self.assertIn("request", str(ctx.exception))

    def test_missing_symbol_raises(self):
        d = _good_config_dict(); del d["request"]["symbol"]
        with self.assertRaises(ConfigError) as ctx:
            parse_config_dict(d)
        self.assertIn("symbol", str(ctx.exception))

    def test_missing_timeframe_raises(self):
        d = _good_config_dict(); del d["request"]["timeframe"]
        with self.assertRaises(ConfigError):
            parse_config_dict(d)

    def test_missing_start_raises(self):
        d = _good_config_dict(); del d["request"]["start"]
        with self.assertRaises(ConfigError):
            parse_config_dict(d)

    def test_missing_strategy_raises(self):
        d = _good_config_dict(); del d["strategy"]
        with self.assertRaises(ConfigError):
            parse_config_dict(d)

    def test_missing_strategy_name_raises(self):
        d = _good_config_dict(); del d["strategy"]["name"]
        with self.assertRaises(ConfigError):
            parse_config_dict(d)

    # ---- type / range checks ----------------------------------------

    def test_invalid_timeframe_rejected(self):
        d = _good_config_dict()
        d["request"]["timeframe"] = "5m"  # not in (1D, 4H, 1H, 15m)
        with self.assertRaises(ConfigError) as ctx:
            parse_config_dict(d)
        self.assertIn("timeframe", str(ctx.exception))

    def test_unknown_strategy_rejected_with_helpful_message(self):
        d = _good_config_dict()
        d["strategy"]["name"] = "scanner_replica"  # M17.B, not M17.A
        with self.assertRaises(ConfigError) as ctx:
            parse_config_dict(d)
        msg = str(ctx.exception)
        self.assertIn("scanner_replica", msg)
        self.assertIn("M17.B", msg)

    def test_end_before_start_rejected(self):
        d = _good_config_dict()
        d["request"]["start"] = "2024-12-31"
        d["request"]["end"]   = "2024-01-01"
        with self.assertRaises(ConfigError):
            parse_config_dict(d)

    def test_bad_date_string_rejected(self):
        d = _good_config_dict()
        d["request"]["start"] = "not-a-date"
        with self.assertRaises(ConfigError):
            parse_config_dict(d)

    def test_allow_short_true_rejected_in_M17_A(self):
        d = _good_config_dict()
        d["execution"]["allow_short"] = True
        with self.assertRaises(ConfigError) as ctx:
            parse_config_dict(d)
        self.assertIn("short", str(ctx.exception).lower())

    def test_negative_fee_rejected(self):
        d = _good_config_dict()
        d["execution"]["fee_bps"] = -1
        with self.assertRaises(ConfigError):
            parse_config_dict(d)

    def test_risk_per_trade_zero_rejected(self):
        d = _good_config_dict()
        d["execution"]["risk_per_trade_pct"] = 0
        with self.assertRaises(ConfigError):
            parse_config_dict(d)

    def test_risk_per_trade_above_one_rejected(self):
        d = _good_config_dict()
        d["execution"]["risk_per_trade_pct"] = 1.5
        with self.assertRaises(ConfigError):
            parse_config_dict(d)

    def test_strategy_params_must_be_dict(self):
        d = _good_config_dict()
        d["strategy"]["params"] = "not a dict"
        with self.assertRaises(ConfigError):
            parse_config_dict(d)

    def test_bool_as_number_rejected(self):
        """Python bools are ints; we explicitly reject True as fee_bps=1."""
        d = _good_config_dict()
        d["execution"]["fee_bps"] = True
        with self.assertRaises(ConfigError):
            parse_config_dict(d)

    # ---- file loading -----------------------------------------------

    def test_parse_config_file_happy_path(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                            delete=False) as f:
            json.dump(_good_config_dict(), f)
            tmppath = f.name
        try:
            cfg = parse_config_file(tmppath)
            self.assertEqual(cfg.request.symbol, "AAPL")
        finally:
            Path(tmppath).unlink()

    def test_parse_config_file_missing_file_raises(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_config_file("/nonexistent/path/config.json")
        self.assertIn("not found", str(ctx.exception))

    def test_parse_config_file_malformed_json_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                            delete=False) as f:
            f.write("{ not valid json")
            tmppath = f.name
        try:
            with self.assertRaises(ConfigError) as ctx:
                parse_config_file(tmppath)
            self.assertIn("JSON", str(ctx.exception))
        finally:
            Path(tmppath).unlink()

    # ---- config_hash determinism ------------------------------------

    def test_config_hash_is_deterministic(self):
        cfg1 = parse_config_dict(_good_config_dict())
        cfg2 = parse_config_dict(_good_config_dict())
        self.assertEqual(config_hash(cfg1), config_hash(cfg2))

    def test_config_hash_changes_with_params(self):
        d1 = _good_config_dict()
        d2 = _good_config_dict()
        d2["strategy"]["params"]["fast_window"] = 21  # different
        cfg1 = parse_config_dict(d1)
        cfg2 = parse_config_dict(d2)
        self.assertNotEqual(config_hash(cfg1), config_hash(cfg2))

    def test_config_hash_length(self):
        cfg = parse_config_dict(_good_config_dict())
        h = config_hash(cfg)
        self.assertEqual(len(h), 12)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))

    def test_config_to_dict_round_trips(self):
        cfg = parse_config_dict(_good_config_dict())
        d   = config_to_dict(cfg)
        cfg2 = parse_config_dict(d)
        self.assertEqual(config_hash(cfg), config_hash(cfg2))

    # ---- error class hierarchy ---------------------------------------

    def test_error_classes_inherit_from_BacktestError(self):
        self.assertTrue(issubclass(ConfigError, BacktestError))
        self.assertTrue(issubclass(MissingDataError, BacktestError))
        self.assertTrue(issubclass(StrategyError, BacktestError))

    # ---- engine version constant ------------------------------------

    def test_engine_version_present(self):
        self.assertIsInstance(ENGINE_VERSION, str)
        self.assertTrue(ENGINE_VERSION.startswith("M17"))


# ─────────────────────────────────────────────────────────────────────
# G2 — Data loader (Phase 2)
# ─────────────────────────────────────────────────────────────────────

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd

from bot.backtesting import data_loader


def _good_coverage(start="2023-01-01", end="2025-01-01",
                    missing_count=0, quality_status="clean",
                    freshness_status="fresh"):
    return {
        "symbol":            "AAPL",
        "timeframe":         "1D",
        "provider":          "yfinance",
        "first_ts_utc":      pd.Timestamp(start, tz="UTC"),
        "last_ts_utc":       pd.Timestamp(end,   tz="UTC"),
        "bar_count":         500,
        "missing_count":     missing_count,
        "duplicate_count":   0,
        "quality_status":    quality_status,
        "freshness_status":  freshness_status,
        "last_refresh_at_utc": pd.Timestamp("2025-01-02", tz="UTC"),
        "last_refresh_id":   "abc123",
        "provider_limit_note": None,
        "source_timeframe":  "1D",
        "derivation_method": "native",
        "resample_rule_version": None,
    }


def _good_bars(n=300, start_date=date(2024, 1, 1)):
    """Build n synthetic daily bars starting from start_date.
    All columns present, no NaN, no duplicates, sorted ascending."""
    rng = pd.date_range(start=pd.Timestamp(start_date, tz="UTC"),
                          periods=n, freq="D")
    return pd.DataFrame({
        "ts_utc": rng,
        "open":   [100.0 + i * 0.1 for i in range(n)],
        "high":   [101.0 + i * 0.1 for i in range(n)],
        "low":    [ 99.0 + i * 0.1 for i in range(n)],
        "close":  [100.5 + i * 0.1 for i in range(n)],
        "volume": [1_000_000] * n,
        "quality_flags": [0] * n,
    })


class G2_DataLoader(unittest.TestCase):
    """Group 2: data loader. M16 coverage gate enforcement + bar
    integrity checks. M16 store is mocked end-to-end; no real file
    access."""

    def _patched(self, *, coverage, bars):
        """Helper: patch the M16 store so get_coverage returns
        `coverage` and get_bars returns `bars`."""
        fake = MagicMock()
        fake.get_coverage = MagicMock(return_value=coverage)
        fake.get_bars     = MagicMock(return_value=bars)
        return patch.object(data_loader, "_m16_store", fake)

    def test_happy_path_returns_bars_coverage_warnings(self):
        cfg = parse_config_dict(_good_config_dict())
        cov = _good_coverage()
        bars = _good_bars(n=400, start_date=date(2023, 12, 1))
        with self._patched(coverage=cov, bars=bars):
            df, returned_cov, warnings = data_loader.load_backtest_bars(cfg)
        self.assertEqual(len(df), 400)
        self.assertEqual(returned_cov["symbol"], "AAPL")
        self.assertEqual(warnings, [])

    def test_no_coverage_row_raises_with_refresh_command(self):
        cfg = parse_config_dict(_good_config_dict())
        with self._patched(coverage=None, bars=pd.DataFrame()):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        msg = str(ctx.exception)
        self.assertIn("python -m bot.historical.cli backfill", msg)
        self.assertIn("--symbols AAPL",  msg)
        self.assertIn("--timeframes 1D", msg)

    def test_coverage_starts_too_late_raises(self):
        cfg = parse_config_dict(_good_config_dict())
        # request start 2024-01-01, but coverage starts 2024-06-01
        cov = _good_coverage(start="2024-06-01")
        with self._patched(coverage=cov, bars=_good_bars()):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        self.assertIn("starts at",  str(ctx.exception))
        self.assertIn("after requested start", str(ctx.exception))

    def test_coverage_ends_too_early_raises(self):
        cfg = parse_config_dict(_good_config_dict())
        # request end 2024-12-31, but coverage ends 2024-06-01
        cov = _good_coverage(end="2024-06-01")
        with self._patched(coverage=cov, bars=_good_bars()):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        self.assertIn("ends at", str(ctx.exception))
        self.assertIn("before requested end", str(ctx.exception))

    def test_missing_count_gt_zero_raises(self):
        cfg = parse_config_dict(_good_config_dict())
        cov = _good_coverage(missing_count=5)
        with self._patched(coverage=cov, bars=_good_bars()):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        self.assertIn("missing_count=5", str(ctx.exception))

    def test_quality_status_error_raises(self):
        cfg = parse_config_dict(_good_config_dict())
        cov = _good_coverage(quality_status="error")
        with self._patched(coverage=cov, bars=_good_bars()):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        self.assertIn("quality_status='error'", str(ctx.exception))

    def test_quality_status_warn_records_warning_but_continues(self):
        cfg = parse_config_dict(_good_config_dict())
        cov = _good_coverage(quality_status="warn")
        with self._patched(coverage=cov, bars=_good_bars()):
            df, _, warnings = data_loader.load_backtest_bars(cfg)
        self.assertGreater(len(df), 0)
        codes = [w.code for w in warnings]
        self.assertIn("m16_quality_warn", codes)

    def test_freshness_status_stale_records_warning_but_continues(self):
        cfg = parse_config_dict(_good_config_dict())
        cov = _good_coverage(freshness_status="stale")
        with self._patched(coverage=cov, bars=_good_bars()):
            df, _, warnings = data_loader.load_backtest_bars(cfg)
        codes = [w.code for w in warnings]
        self.assertIn("m16_freshness_warn", codes)

    def test_empty_bars_after_load_raises(self):
        cfg = parse_config_dict(_good_config_dict())
        cov = _good_coverage()
        empty = pd.DataFrame(columns=[
            "ts_utc", "open", "high", "low", "close",
            "volume", "quality_flags"])
        with self._patched(coverage=cov, bars=empty):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        self.assertIn("0 bars", str(ctx.exception))

    def test_nan_ohlc_in_bars_raises(self):
        cfg = parse_config_dict(_good_config_dict())
        cov = _good_coverage()
        bars = _good_bars(n=100)
        bars.loc[50, "close"] = float("nan")
        with self._patched(coverage=cov, bars=bars):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        self.assertIn("NaN OHLC", str(ctx.exception))

    def test_duplicate_timestamps_in_bars_raises(self):
        cfg = parse_config_dict(_good_config_dict())
        cov = _good_coverage()
        bars = _good_bars(n=100)
        bars.loc[50, "ts_utc"] = bars.loc[51, "ts_utc"]  # duplicate
        with self._patched(coverage=cov, bars=bars):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        self.assertIn("duplicate timestamps", str(ctx.exception))

    def test_date_boundary_inclusive_to_exclusive_conversion(self):
        """The CLI passes inclusive end '2024-12-31'; M16 must be
        called with exclusive end '2025-01-01' (next day 00:00 UTC)
        so the bar at ts_utc=2024-12-31 00:00 UTC is included."""
        cfg = parse_config_dict(_good_config_dict())
        cov = _good_coverage()
        bars = _good_bars(n=400, start_date=date(2023, 12, 1))

        with self._patched(coverage=cov, bars=bars) as p:
            data_loader.load_backtest_bars(cfg)

            # Inspect the call to get_bars.
            data_loader._m16_store.get_bars.assert_called_once()
            kw = data_loader._m16_store.get_bars.call_args.kwargs
            self.assertEqual(kw["start_utc"],
                              datetime(2024, 1, 1, tzinfo=timezone.utc))
            self.assertEqual(kw["end_utc"],
                              datetime(2025, 1, 1, tzinfo=timezone.utc))
            self.assertEqual(kw["adjusted"], True)
            self.assertEqual(kw["provider"], "yfinance")


# ─────────────────────────────────────────────────────────────────────
# G3 — Indicators (Phase 3)
# ─────────────────────────────────────────────────────────────────────

import math

import numpy as np

from bot.backtesting import indicators as ind


class G3_Indicators(unittest.TestCase):
    """Group 3: vectorized indicators. Hand-computed reference values,
    NaN-at-warmup behaviour, no-look-ahead AST patterns."""

    # ---- SMA ---------------------------------------------------------

    def test_sma_known_values(self):
        # window=3, hand-computed
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        out = ind.sma(s, window=3)
        # NaN, NaN, (1+2+3)/3, (2+3+4)/3, (3+4+5)/3, (4+5+6)/3
        expected = [math.nan, math.nan, 2.0, 3.0, 4.0, 5.0]
        for i, exp in enumerate(expected):
            if math.isnan(exp):
                self.assertTrue(math.isnan(out.iloc[i]))
            else:
                self.assertAlmostEqual(out.iloc[i], exp, places=9)

    def test_sma_warmup_is_nan(self):
        s = pd.Series([1.0] * 10)
        out = ind.sma(s, window=5)
        for i in range(4):
            self.assertTrue(math.isnan(out.iloc[i]))
        for i in range(4, 10):
            self.assertEqual(out.iloc[i], 1.0)

    def test_sma_rejects_invalid_window(self):
        s = pd.Series([1.0, 2.0, 3.0])
        with self.assertRaises(ValueError):
            ind.sma(s, window=0)
        with self.assertRaises(ValueError):
            ind.sma(s, window=-3)

    # ---- EMA ---------------------------------------------------------

    def test_ema_known_values(self):
        # EMA(span=3, adjust=False, min_periods=3):
        #   alpha = 2 / (3+1) = 0.5
        #   ema[2] = (1 + 2 + 3) / 3 = 2.0   (seed = SMA(window) at first valid)
        # Pandas' ewm seeds differently:
        #   ema[0] = NaN, ema[1] = NaN (min_periods),
        #   ema[2] = recursive: 0.25*x[0] + 0.25*x[1] + 0.5*x[2]
        #         = 0.25*1 + 0.25*2 + 0.5*3 = 0.25 + 0.5 + 1.5 = 2.25
        # We test pandas' behaviour directly, not Wilder seed.
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        out = ind.ema(s, window=3)
        self.assertTrue(math.isnan(out.iloc[0]))
        self.assertTrue(math.isnan(out.iloc[1]))
        # Pandas ewm(span=3, adjust=False) starting from x[0]:
        # y[0] = 1.0
        # y[1] = 0.5*1 + 0.5*2 = 1.5
        # y[2] = 0.5*1.5 + 0.5*3 = 2.25
        # y[3] = 0.5*2.25 + 0.5*4 = 3.125
        # y[4] = 0.5*3.125 + 0.5*5 = 4.0625
        # min_periods=3 masks indices 0..1; index 2 onward visible.
        self.assertAlmostEqual(out.iloc[2], 2.25,   places=6)
        self.assertAlmostEqual(out.iloc[3], 3.125,  places=6)
        self.assertAlmostEqual(out.iloc[4], 4.0625, places=6)

    # ---- RSI ---------------------------------------------------------

    def test_rsi_all_up_is_100(self):
        # Continuously rising series -> all gains, no losses -> RSI = 100
        s = pd.Series([float(i) for i in range(1, 50)])
        out = ind.rsi(s, period=14)
        # After warmup, RSI should be 100.
        for i in range(20, len(out)):
            self.assertEqual(out.iloc[i], 100.0,
                              f"index {i}: expected 100, got {out.iloc[i]}")

    def test_rsi_all_down_is_zero(self):
        s = pd.Series([float(i) for i in range(50, 1, -1)])
        out = ind.rsi(s, period=14)
        for i in range(20, len(out)):
            self.assertAlmostEqual(out.iloc[i], 0.0, places=6)

    def test_rsi_warmup_is_nan(self):
        s = pd.Series([1.0, 2.0, 3.0])
        out = ind.rsi(s, period=14)
        # Too few values: all NaN.
        for v in out:
            self.assertTrue(math.isnan(v))

    # ---- MACD --------------------------------------------------------

    def test_macd_columns_present(self):
        s = pd.Series([float(i) for i in range(1, 50)])
        out = ind.macd(s, fast=12, slow=26, signal=9)
        self.assertEqual(set(out.columns), {"macd", "signal", "hist"})
        # Last row should not be NaN (we have enough warmup).
        self.assertFalse(math.isnan(out["macd"].iloc[-1]))
        self.assertFalse(math.isnan(out["signal"].iloc[-1]))
        self.assertFalse(math.isnan(out["hist"].iloc[-1]))

    def test_macd_hist_equals_macd_minus_signal(self):
        s = pd.Series(np.random.RandomState(42).randn(100).cumsum() + 100)
        out = ind.macd(s, fast=12, slow=26, signal=9)
        diff = out["macd"] - out["signal"]
        last = out["hist"].dropna().tail(30)
        for i in last.index:
            self.assertAlmostEqual(out["hist"].loc[i],
                                      diff.loc[i], places=9)

    def test_macd_rejects_fast_gte_slow(self):
        s = pd.Series([1.0, 2.0, 3.0])
        with self.assertRaises(ValueError):
            ind.macd(s, fast=12, slow=12, signal=9)

    # ---- ATR ---------------------------------------------------------

    def test_atr_basic_shape(self):
        h = pd.Series([10.0, 11.0, 12.0, 11.5, 12.5, 13.0] * 5)
        l = pd.Series([ 9.0,  9.5, 10.0, 10.0, 11.0, 11.5] * 5)
        c = pd.Series([ 9.5, 10.5, 11.0, 11.0, 12.0, 12.5] * 5)
        out = ind.atr(h, l, c, period=14)
        # Last value finite, > 0
        self.assertFalse(math.isnan(out.iloc[-1]))
        self.assertGreater(out.iloc[-1], 0)

    def test_atr_warmup_is_nan(self):
        h = pd.Series([10.0, 11.0, 12.0])
        l = pd.Series([ 9.0,  9.5, 10.0])
        c = pd.Series([ 9.5, 10.5, 11.0])
        out = ind.atr(h, l, c, period=14)
        for v in out:
            self.assertTrue(math.isnan(v))

    # ---- Bollinger ---------------------------------------------------

    def test_bollinger_at_constant_series(self):
        s = pd.Series([100.0] * 30)
        out = ind.bollinger(s, window=20, num_std=2.0)
        # std is 0 everywhere; bands collapse to middle; pct_b NaN
        self.assertEqual(out["middle"].iloc[-1], 100.0)
        self.assertEqual(out["upper"].iloc[-1],  100.0)
        self.assertEqual(out["lower"].iloc[-1],  100.0)
        self.assertTrue(math.isnan(out["pct_b"].iloc[-1]))

    # ---- volume_avg / volume_ratio -----------------------------------

    def test_volume_ratio_above_below_average(self):
        v = pd.Series([100.0] * 19 + [200.0])
        avg = ind.volume_avg(v, window=20)
        ratio = ind.volume_ratio(v, window=20)
        # last bar's avg includes the spike: (19*100 + 200)/20 = 105
        self.assertAlmostEqual(avg.iloc[-1], 105.0, places=6)
        self.assertAlmostEqual(ratio.iloc[-1], 200.0 / 105.0, places=6)

    # ---- AST: no centered windows, no negative shifts ---------------

    def test_indicators_have_no_centered_or_forward_indexing(self):
        """AST scan: ensure no rolling(..., center=True) or shift(-N)."""
        import ast
        with open("bot/backtesting/indicators.py") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            # rolling(..., center=True)
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "rolling"):
                for kw in node.keywords:
                    if kw.arg == "center":
                        # If center is set, it must be False (or absent)
                        if isinstance(kw.value, ast.Constant):
                            self.assertFalse(
                                kw.value.value,
                                "rolling(center=True) creates look-ahead")
            # shift(-N)
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "shift"):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
                        self.assertGreaterEqual(
                            arg.value, 0,
                            f"shift({arg.value}) is forward-looking")
                    elif isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub):
                        self.fail("shift(-N) is forward-looking")


# ─────────────────────────────────────────────────────────────────────
# G4 — Strategy + look-ahead protection (Phase 4)
# ─────────────────────────────────────────────────────────────────────

from bot.backtesting import strategy as strat


class G4_StrategyAndLookahead(unittest.TestCase):
    """Group 4: strategy contract + SmaCrossoverStrategy + look-ahead
    protection (the scramble-future-bars test)."""

    def _bars_with_known_crossover(self):
        """Hand-crafted bars: prices rise for 60 bars, then fall.
        With fast=5, slow=20: a clear up-cross around bar ~20-25 and
        a down-cross around bar ~75."""
        prices = list(range(100, 160)) + list(range(160, 100, -1))
        n = len(prices)
        return pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC"),
            "open":   prices,
            "high":   [p + 1 for p in prices],
            "low":    [p - 1 for p in prices],
            "close":  prices,
            "volume": [1_000_000] * n,
            "quality_flags": [0] * n,
        })

    # ---- happy path -------------------------------------------------

    def test_sma_crossover_produces_known_entry_and_exit(self):
        bars = self._bars_with_known_crossover()
        s = strat.SmaCrossoverStrategy({"fast_window": 5, "slow_window": 20})
        sig = s.run(bars)
        # Must produce at least one entry and one exit
        entries = (sig["signal"] == strat.SIG_ENTRY).sum()
        exits   = (sig["signal"] == strat.SIG_EXIT).sum()
        self.assertGreaterEqual(entries, 1)
        self.assertGreaterEqual(exits,   1)
        # On entry bars, direction must be 'long'
        for i in sig.index[sig["signal"] == strat.SIG_ENTRY]:
            self.assertEqual(sig.at[i, "direction"], "long")

    def test_warmup_bars_emit_flat_only(self):
        bars = self._bars_with_known_crossover()
        s = strat.SmaCrossoverStrategy({"fast_window": 5, "slow_window": 20})
        sig = s.run(bars)
        # First 19 bars (slow_window-1) must all be flat (signal=0).
        for i in range(19):
            self.assertEqual(sig.iloc[i]["signal"], strat.SIG_FLAT)
            self.assertEqual(sig.iloc[i]["direction"], "flat")

    # ---- contract enforcement ---------------------------------------

    def test_strategy_output_has_required_columns(self):
        bars = self._bars_with_known_crossover()
        s = strat.SmaCrossoverStrategy({"fast_window": 5, "slow_window": 20})
        sig = s.run(bars)
        for col in strat.SIGNAL_COLUMNS:
            self.assertIn(col, sig.columns)
        self.assertEqual(len(sig), len(bars))

    def test_get_strategy_returns_registered_class(self):
        s = strat.get_strategy("sma_crossover",
                                  {"fast_window": 5, "slow_window": 20})
        self.assertIsInstance(s, strat.SmaCrossoverStrategy)

    def test_get_strategy_unknown_name_raises(self):
        with self.assertRaises(ConfigError):
            strat.get_strategy("not_a_strategy", {})

    # ---- param validation ------------------------------------------

    def test_sma_rejects_fast_gte_slow(self):
        with self.assertRaises(ConfigError):
            strat.SmaCrossoverStrategy({"fast_window": 50, "slow_window": 20})

    def test_sma_rejects_non_int_window(self):
        with self.assertRaises(ConfigError):
            strat.SmaCrossoverStrategy({"fast_window": 5.5, "slow_window": 20})

    # ---- look-ahead protection -------------------------------------

    def test_signal_does_not_depend_on_future_bars(self):
        """Scramble future bars (i+1..N) and assert decision at bar i
        is unchanged. This is the headline look-ahead-protection test:
        if any strategy code reads bars[i+1:], this test fails."""
        bars = self._bars_with_known_crossover()
        s1 = strat.SmaCrossoverStrategy({"fast_window": 5, "slow_window": 20})
        sig_original = s1.run(bars)

        # Pick a checkpoint bar past warmup
        check_bar = 35

        # Build a scrambled bars frame: bars 0..check_bar identical,
        # bars check_bar+1..end randomly reshuffled (excluding ts_utc).
        scrambled = bars.copy()
        future_ohlcv = scrambled.iloc[check_bar + 1:][
            ["open", "high", "low", "close", "volume", "quality_flags"]
        ].copy()
        # Randomly reshuffle OHLCV rows; ts_utc column stays put so the
        # date sequence stays monotone (strategy sorts by ts_utc).
        shuffled = future_ohlcv.sample(
            frac=1.0, random_state=42).reset_index(drop=True)
        for col in ("open", "high", "low", "close", "volume",
                     "quality_flags"):
            scrambled.loc[scrambled.index[check_bar + 1:], col] = (
                shuffled[col].values)

        s2 = strat.SmaCrossoverStrategy({"fast_window": 5, "slow_window": 20})
        sig_scrambled = s2.run(scrambled)

        # At bar `check_bar`, signal MUST be the same.
        self.assertEqual(
            sig_original.iloc[check_bar]["signal"],
            sig_scrambled.iloc[check_bar]["signal"],
            "Strategy signal at bar i changed when bars[i+1:] was "
            "scrambled — LOOK-AHEAD BIAS PRESENT")
        # And all bars [0..check_bar] should also be identical.
        for i in range(check_bar + 1):
            self.assertEqual(
                sig_original.iloc[i]["signal"],
                sig_scrambled.iloc[i]["signal"],
                f"Signal at bar {i} changed when future bars were "
                f"scrambled — look-ahead bias")

    def test_strategy_module_has_no_negative_shift_or_forward_indexing(self):
        """AST scan on strategy.py — no shift(-N), no ranges with negative
        step that walk forward."""
        import ast
        with open("bot/backtesting/strategy.py") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "shift"):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
                        self.assertGreaterEqual(
                            arg.value, 0,
                            f"strategy.py: shift({arg.value}) is forward-looking")
                    elif isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub):
                        self.fail("strategy.py: shift(-N) is forward-looking")


# ─────────────────────────────────────────────────────────────────────
# Execution tests: shared fixtures for G5, G6, G7
# ─────────────────────────────────────────────────────────────────────

from bot.backtesting.execution import simulate
from bot.backtesting.ledger import Ledger
from bot.backtesting.portfolio import Portfolio
from bot.backtesting.strategy import SIG_ENTRY, SIG_EXIT, SIG_FLAT


def _make_bars(close_seq, *, open_offset=0.0, high_offset=1.0,
                low_offset=-1.0):
    """Build a bars DataFrame from a close-price sequence."""
    n = len(close_seq)
    return pd.DataFrame({
        "ts_utc": pd.date_range("2024-01-01", periods=n, freq="D",
                                  tz="UTC"),
        "open":   [c + open_offset for c in close_seq],
        "high":   [c + high_offset for c in close_seq],
        "low":    [c + low_offset  for c in close_seq],
        "close":  list(close_seq),
        "volume": [1_000_000] * n,
        "quality_flags": [0] * n,
    })


def _make_signals(n, *, entry_at, exit_at=None):
    """Build a signals DataFrame with one entry signal at index
    `entry_at` and (optionally) one exit signal at `exit_at`."""
    sig = pd.Series(SIG_FLAT, index=range(n), dtype="int64")
    direction = pd.Series("flat", index=range(n), dtype="object")
    if entry_at is not None:
        sig.iloc[entry_at] = SIG_ENTRY
        direction.iloc[entry_at] = "long"
    if exit_at is not None:
        sig.iloc[exit_at] = SIG_EXIT
    return pd.DataFrame({
        "signal": sig,
        "direction": direction,
        "atr_at_signal":    pd.Series([np.nan] * n, dtype="float64"),
        "entry_price_hint": pd.Series([100.0] * n, dtype="float64"),
    })


def _config(symbol="AAPL", initial_equity=10000.0, fee_bps=0,
              slippage_bps=0, stop_loss_pct=None, take_profit_pct=None,
              risk_per_trade_pct=0.01, max_position_pct=1.0):
    """Build a BacktestConfig with knob-able execution settings."""
    return parse_config_dict({
        "request": {
            "symbol": symbol, "timeframe": "1D",
            "start": "2024-01-01", "end": "2024-12-31",
        },
        "strategy": {"name": "sma_crossover",
                       "params": {"fast_window": 5, "slow_window": 20}},
        "execution": {
            "initial_equity":     initial_equity,
            "fee_bps":            fee_bps,
            "slippage_bps":       slippage_bps,
            "stop_loss_pct":      stop_loss_pct,
            "take_profit_pct":    take_profit_pct,
            "risk_per_trade_pct": risk_per_trade_pct,
            "max_position_pct":   max_position_pct,
            "allow_short":        False,
        },
    })


# ─────────────────────────────────────────────────────────────────────
# G5 — Execution timing
# ─────────────────────────────────────────────────────────────────────

class G5_ExecutionTiming(unittest.TestCase):
    """Group 5: signal-to-fill timing. Entry/exit at NEXT bar open."""

    def test_entry_fills_at_next_bar_open(self):
        bars = _make_bars([100, 101, 102, 103, 104],
                            open_offset=0.0, high_offset=0.5, low_offset=-0.5)
        # Signal at index 1 -> entry at index 2 open
        sigs = _make_signals(5, entry_at=1, exit_at=3)
        cfg = _config()
        ledger = Ledger()
        simulate(bars=bars, signals=sigs, cfg=cfg, ledger=ledger)
        self.assertEqual(len(ledger.trades), 1)
        t = ledger.trades[0]
        self.assertEqual(t.entry_ts_utc, bars.iloc[2]["ts_utc"].to_pydatetime())
        # Entry fill = bar 2 open = 102 (slip=0, so identical)
        self.assertAlmostEqual(t.entry_price, 102.0, places=6)

    def test_exit_fills_at_next_bar_open_after_signal(self):
        bars = _make_bars([100, 101, 102, 103, 104, 105])
        sigs = _make_signals(6, entry_at=0, exit_at=3)
        cfg = _config()
        ledger = Ledger()
        simulate(bars=bars, signals=sigs, cfg=cfg, ledger=ledger)
        t = ledger.trades[0]
        # Exit signal at bar 3 -> exit fill at bar 4 open = 104
        self.assertEqual(t.exit_ts_utc, bars.iloc[4]["ts_utc"].to_pydatetime())
        self.assertAlmostEqual(t.exit_price, 104.0, places=6)
        self.assertEqual(t.exit_reason, "signal")

    def test_no_same_bar_entry(self):
        """Signal at bar i can NEVER trigger entry on bar i. Tests that
        a signal at the last bar produces NO trade (no next bar to fill)."""
        bars = _make_bars([100, 101, 102, 103, 104])
        sigs = _make_signals(5, entry_at=4)   # signal at last bar
        cfg = _config()
        ledger = Ledger()
        simulate(bars=bars, signals=sigs, cfg=cfg, ledger=ledger)
        self.assertEqual(len(ledger.trades), 0,
                          "signal on last bar must not produce a trade")

    def test_eod_exit_at_last_close(self):
        bars = _make_bars([100, 101, 102, 103, 104])
        sigs = _make_signals(5, entry_at=1)
        cfg = _config()
        ledger = Ledger()
        simulate(bars=bars, signals=sigs, cfg=cfg, ledger=ledger)
        self.assertEqual(len(ledger.trades), 1)
        t = ledger.trades[0]
        self.assertEqual(t.exit_reason, "eod")
        self.assertAlmostEqual(t.exit_price, 104.0, places=6)

    def test_fees_applied_on_both_sides(self):
        bars = _make_bars([100, 100, 100, 100, 100])
        sigs = _make_signals(5, entry_at=0, exit_at=2)
        # fee_bps=100 = 1% per side
        cfg = _config(fee_bps=100, max_position_pct=1.0)
        ledger = Ledger()
        simulate(bars=bars, signals=sigs, cfg=cfg, ledger=ledger)
        t = ledger.trades[0]
        # Round-trip fee: 1% on entry notional + 1% on exit notional
        # entry_notional = qty * 100
        # 1% = qty (since price=100)
        # exit_notional same -> total = 2*qty
        expected = 2 * t.qty
        self.assertAlmostEqual(t.fees_paid, expected, places=6)

    def test_slippage_applied_on_both_sides_for_long(self):
        bars = _make_bars([100, 100, 100, 100, 100])
        sigs = _make_signals(5, entry_at=0, exit_at=2)
        # slippage_bps=100 = 1% per side
        cfg = _config(slippage_bps=100, max_position_pct=1.0)
        ledger = Ledger()
        simulate(bars=bars, signals=sigs, cfg=cfg, ledger=ledger)
        t = ledger.trades[0]
        # Entry fills 1% above open (long pays more): 100 * 1.01 = 101
        # Exit fills 1% below open (long receives less): 100 * 0.99 = 99
        self.assertAlmostEqual(t.entry_price, 101.0, places=6)
        self.assertAlmostEqual(t.exit_price,  99.0,  places=6)


# ─────────────────────────────────────────────────────────────────────
# G6 — Stop loss / take profit
# ─────────────────────────────────────────────────────────────────────

class G6_StopLossTakeProfit(unittest.TestCase):
    """Group 6: intrabar SL/TP with pessimistic SL-first + gap-aware fills."""

    def test_stop_loss_hit_intrabar(self):
        # Entry at bar 1 open = 100; SL at 100 * 0.95 = 95
        # Bar 2 low = 94 -> SL touched
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC"),
            "open":   [100, 100, 100, 100],
            "high":   [101, 101, 100, 100],
            "low":    [ 99,  99,  94,  99],   # bar 2 low = 94 < 95
            "close":  [100, 100,  96, 100],
            "volume": [1_000_000] * 4,
            "quality_flags": [0] * 4,
        })
        sigs = _make_signals(4, entry_at=0)
        cfg = _config(stop_loss_pct=0.05, max_position_pct=1.0)
        ledger = Ledger()
        simulate(bars=bars, signals=sigs, cfg=cfg, ledger=ledger)
        self.assertEqual(len(ledger.trades), 1)
        t = ledger.trades[0]
        self.assertEqual(t.exit_reason, "stop_loss")
        # Open of bar 2 = 100; stop = 95; since open > stop, fill at stop
        self.assertAlmostEqual(t.exit_price, 95.0, places=6)

    def test_take_profit_hit_intrabar(self):
        # Entry at bar 1 open = 100; TP at 100 * 1.05 = 105
        # Bar 2 high = 106 -> TP touched
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC"),
            "open":   [100, 100, 102, 100],
            "high":   [101, 101, 106, 100],
            "low":    [ 99,  99, 101,  99],
            "close":  [100, 100, 105, 100],
            "volume": [1_000_000] * 4,
            "quality_flags": [0] * 4,
        })
        sigs = _make_signals(4, entry_at=0)
        cfg = _config(take_profit_pct=0.05, max_position_pct=1.0)
        ledger = Ledger()
        simulate(bars=bars, signals=sigs, cfg=cfg, ledger=ledger)
        t = ledger.trades[0]
        self.assertEqual(t.exit_reason, "take_profit")
        self.assertAlmostEqual(t.exit_price, 105.0, places=6)

    def test_sl_and_tp_both_touched_same_bar_pessimistic_sl_first(self):
        # Both SL and TP touched in the same bar -> SL wins.
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC"),
            "open":   [100, 100, 100, 100],
            "high":   [101, 101, 106, 100],   # touches TP at 105
            "low":    [ 99,  99,  94, 100],   # touches SL at 95
            "close":  [100, 100, 100, 100],
            "volume": [1_000_000] * 4,
            "quality_flags": [0] * 4,
        })
        sigs = _make_signals(4, entry_at=0)
        cfg = _config(stop_loss_pct=0.05, take_profit_pct=0.05,
                       max_position_pct=1.0)
        ledger = Ledger()
        simulate(bars=bars, signals=sigs, cfg=cfg, ledger=ledger)
        t = ledger.trades[0]
        self.assertEqual(t.exit_reason, "stop_loss",
                          "both SL and TP touched same bar -> SL wins")

    def test_gap_below_stop_fills_at_open(self):
        # Bar 2 OPENS at 90, below the stop at 95.
        # Pessimistic gap-aware fill: fill at OPEN (90), not at stop (95).
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC"),
            "open":   [100, 100,  90,  92],
            "high":   [101, 101,  92,  93],
            "low":    [ 99,  99,  88,  91],
            "close":  [100, 100,  91,  92],
            "volume": [1_000_000] * 4,
            "quality_flags": [0] * 4,
        })
        sigs = _make_signals(4, entry_at=0)
        cfg = _config(stop_loss_pct=0.05, max_position_pct=1.0)
        ledger = Ledger()
        simulate(bars=bars, signals=sigs, cfg=cfg, ledger=ledger)
        t = ledger.trades[0]
        self.assertEqual(t.exit_reason, "stop_loss")
        # Gap-aware: filled at the BAR OPEN (90), not at the stop (95)
        self.assertAlmostEqual(t.exit_price, 90.0, places=6)

    def test_no_sl_when_disabled(self):
        # Same setup as test_stop_loss_hit_intrabar, but SL=None
        # -> no SL exit; position rides to EOD.
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC"),
            "open":   [100, 100, 100, 100],
            "high":   [101, 101, 100, 100],
            "low":    [ 99,  99,  94,  99],
            "close":  [100, 100,  96, 100],
            "volume": [1_000_000] * 4,
            "quality_flags": [0] * 4,
        })
        sigs = _make_signals(4, entry_at=0)
        cfg = _config(stop_loss_pct=None, take_profit_pct=None,
                       max_position_pct=1.0)
        ledger = Ledger()
        simulate(bars=bars, signals=sigs, cfg=cfg, ledger=ledger)
        t = ledger.trades[0]
        self.assertEqual(t.exit_reason, "eod")

    def test_entry_bar_does_not_check_sl_tp(self):
        """SL/TP are NOT checked on the same bar as entry (entry bar's
        OHLC is the entry bar; engine waits for the NEXT bar to enable
        intrabar checking)."""
        # Entry at bar 1 open = 100. Bar 1 ALSO has low=94 (would touch SL).
        # If engine wrongly checked SL on the entry bar, exit_reason
        # would be stop_loss. Correct behaviour: no exit on entry bar.
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC"),
            "open":   [100, 100, 100, 100],
            "high":   [101, 102, 100, 100],
            "low":    [ 99,  94,  99,  99],   # bar 1 low = 94 (entry bar)
            "close":  [100, 101, 100, 100],
            "volume": [1_000_000] * 4,
            "quality_flags": [0] * 4,
        })
        sigs = _make_signals(4, entry_at=0)
        cfg = _config(stop_loss_pct=0.05, max_position_pct=1.0)
        ledger = Ledger()
        simulate(bars=bars, signals=sigs, cfg=cfg, ledger=ledger)
        # Position survives bar 1 (entry bar). EOD exit at last bar close.
        t = ledger.trades[0]
        self.assertEqual(t.exit_reason, "eod")


# ─────────────────────────────────────────────────────────────────────
# G7 — Position sizing
# ─────────────────────────────────────────────────────────────────────

class G7_PositionSizing(unittest.TestCase):
    """Group 7: fixed-risk sizing with max-position cap, zero-size
    rejection, insufficient-cash handling."""

    def test_fixed_risk_size(self):
        """
        equity=10000, risk_per_trade=0.01 -> risk_amount=100
        stop_loss_pct=0.05 at entry_price=100
        stop_price=95 -> risk_per_share = 100-95 = 5
        shares_by_risk = 100/5 = 20
        max_position_pct=1.0 -> shares_by_cap = 10000/100 = 100
        min -> 20 shares
        """
        from bot.backtesting.portfolio import Portfolio
        from bot.backtesting.config import ExecutionConfig
        cfg = ExecutionConfig(
            initial_equity=10000.0, fee_bps=0, slippage_bps=0,
            stop_loss_pct=0.05, take_profit_pct=None,
            risk_per_trade_pct=0.01, max_position_pct=1.0,
            allow_short=False,
        )
        p = Portfolio(cfg)
        qty, warnings = p.compute_size(entry_price=100.0, stop_price=95.0,
                                            mark_equity=10000.0)
        self.assertEqual(qty, 20)
        self.assertEqual(warnings, [])

    def test_max_position_cap_binds(self):
        """Tight stop wants 1000 shares, but cap allows only 50.
        equity=10000, risk_amount=100, stop=99.9, rps=0.1
        shares_by_risk = 100 / 0.1 = 1000
        shares_by_cap  = 10000 * 0.5 / 100 = 50 -> CAP binds
        """
        from bot.backtesting.portfolio import Portfolio
        from bot.backtesting.config import ExecutionConfig
        cfg = ExecutionConfig(
            initial_equity=10000.0, fee_bps=0, slippage_bps=0,
            stop_loss_pct=None, take_profit_pct=None,
            risk_per_trade_pct=0.01, max_position_pct=0.5,
            allow_short=False,
        )
        p = Portfolio(cfg)
        qty, _ = p.compute_size(entry_price=100.0, stop_price=99.9,
                                   mark_equity=10000.0)
        self.assertEqual(qty, 50)

    def test_zero_size_warning(self):
        """equity=100 cash, entry=$1000, cap=0.01 -> cap_shares=0.001
        floor -> 0 shares -> zero-size warning."""
        from bot.backtesting.portfolio import Portfolio
        from bot.backtesting.config import ExecutionConfig
        cfg = ExecutionConfig(
            initial_equity=100.0, fee_bps=0, slippage_bps=0,
            stop_loss_pct=None, take_profit_pct=None,
            risk_per_trade_pct=0.01, max_position_pct=0.01,
            allow_short=False,
        )
        p = Portfolio(cfg)
        qty, warnings = p.compute_size(entry_price=1000.0, stop_price=None,
                                            mark_equity=100.0)
        self.assertEqual(qty, 0)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].code, "zero_size_skipped")

    def test_zero_size_in_simulation_records_warning_no_trade(self):
        bars = _make_bars([1000, 1000, 1000, 1000])
        sigs = _make_signals(4, entry_at=0)
        cfg = _config(initial_equity=100.0, max_position_pct=0.01)
        ledger = Ledger()
        simulate(bars=bars, signals=sigs, cfg=cfg, ledger=ledger)
        # No trade; zero-size warning recorded.
        self.assertEqual(len(ledger.trades), 0)
        codes = [w.code for w in ledger.warnings]
        self.assertIn("zero_size_skipped", codes)

    def test_insufficient_cash_for_cap_falls_back_to_cash(self):
        """Cap says 1000 shares but cash only covers 100 -> 100 shares."""
        from bot.backtesting.portfolio import Portfolio
        from bot.backtesting.config import ExecutionConfig
        cfg = ExecutionConfig(
            initial_equity=10000.0, fee_bps=0, slippage_bps=0,
            stop_loss_pct=None, take_profit_pct=None,
            risk_per_trade_pct=0.01, max_position_pct=1.0,
            allow_short=False,
        )
        p = Portfolio(cfg)
        p.cash = 100.0   # simulate prior loss
        qty, _ = p.compute_size(entry_price=1.0, stop_price=None,
                                   mark_equity=100.0)
        self.assertEqual(qty, 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
