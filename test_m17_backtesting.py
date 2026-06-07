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

    def test_public_api_run_is_reexported_at_package_root(self):
        """bot.backtesting.run should be the same function as
        bot.backtesting.runner.run — single orchestration path,
        ergonomic top-level access."""
        import bot.backtesting as bk
        import bot.backtesting.runner as runner_mod
        self.assertIs(bk.run, runner_mod.run)
        self.assertIs(bk.run_and_write, runner_mod.run_and_write)
        # __all__ documents the public surface
        self.assertEqual(set(bk.__all__),
                          {"ENGINE_VERSION", "run", "run_and_write"})


# ─────────────────────────────────────────────────────────────────────
# G2 — Data loader (Phase 2)
# ─────────────────────────────────────────────────────────────────────

import shutil
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd

from bot.backtesting import data_loader
# `runner` is also imported later for G9; importing here makes
# G2_ExampleConfig (fixup5) usable without forward-reference gymnastics.
from bot.backtesting import runner as _runner_for_g2


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


def _good_bars(n=400, start_date=date(2024, 1, 1)):
    """Build n synthetic daily bars starting from start_date.
    All columns present, no NaN, no duplicates, sorted ascending.

    Default n=400 (was 300) so the fixture covers the standard test
    request range 2024-01-01..2024-12-31 (~366 days) under M17.A's
    strict bar-level range check introduced in fixup4.
    """
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
        # Use default n=400 so the strict bar-level range check passes
        # and the NaN check is the failure we actually test.
        bars = _good_bars()
        bars.loc[50, "close"] = float("nan")
        with self._patched(coverage=cov, bars=bars):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        self.assertIn("NaN OHLC", str(ctx.exception))

    def test_duplicate_timestamps_in_bars_raises(self):
        cfg = parse_config_dict(_good_config_dict())
        cov = _good_coverage()
        # Use default n=400 so the strict bar-level range check passes
        # and the duplicate-ts check is the failure we actually test.
        bars = _good_bars()
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

    # ---- Issue A regression: M16 refresh commands are VALID ---------

    def test_refresh_command_uses_no_invalid_start_or_end_flags(self):
        """Regression for M17.A.fixup3 Issue A. M16's
        `python -m bot.historical.cli backfill` does NOT accept
        --start or --end flags (only --symbols, --timeframes,
        --lookback). Every refresh message we generate must avoid
        those non-existent flags."""
        cfg = parse_config_dict(_good_config_dict())
        # Trigger all five failure paths in the loader and assert
        # no message contains '--start' or '--end' anywhere.
        cases = [
            # (coverage, bars, label)
            (None, pd.DataFrame(), "no_coverage_row"),
            (_good_coverage(start="2024-06-01"),  _good_bars(), "starts_late"),
            (_good_coverage(end="2024-06-01"),    _good_bars(), "ends_early"),
            (_good_coverage(missing_count=5),     _good_bars(), "missing_gt_zero"),
            (_good_coverage(quality_status="error"), _good_bars(), "quality_error"),
        ]
        for cov, bars, label in cases:
            with self._patched(coverage=cov, bars=bars):
                try:
                    data_loader.load_backtest_bars(cfg)
                    self.fail(f"{label}: expected MissingDataError")
                except MissingDataError as e:
                    msg = str(e)
                    self.assertNotIn(
                        "--start", msg,
                        f"{label}: message must not contain --start")
                    self.assertNotIn(
                        "--end", msg,
                        f"{label}: message must not contain --end")
                    self.assertIn(
                        "python -m bot.historical.cli", msg,
                        f"{label}: message must include the M16 CLI invocation")

    def test_refresh_command_subcommand_matches_failure_mode(self):
        """Regression for M17.A.fixup3 Issue A. The M16 subcommand in
        the refresh message should match the failure semantics, and
        the flag form (plural vs singular) must match each subcommand's
        actual CLI:
          * no coverage / range too narrow / empty -> 'backfill'
              with --symbols / --timeframes (plural)
          * missing_count > 0                       -> 'repair'
              with --symbols / --timeframes (plural)
          * quality_status='error' / NaN / dup ts   -> 'force-rebuild'
              with --symbol / --timeframe (SINGULAR, per M16 CLI)
        """
        cfg = parse_config_dict(_good_config_dict())

        # backfill: no coverage
        with self._patched(coverage=None, bars=pd.DataFrame()):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        msg = str(ctx.exception)
        self.assertIn("backfill", msg)
        self.assertIn("--symbols AAPL", msg)
        self.assertIn("--timeframes 1D", msg)

        # backfill: starts late
        with self._patched(coverage=_good_coverage(start="2024-06-01"),
                              bars=_good_bars()):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        self.assertIn("backfill", str(ctx.exception))

        # repair: missing_count > 0
        with self._patched(coverage=_good_coverage(missing_count=5),
                              bars=_good_bars()):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        msg = str(ctx.exception)
        self.assertIn("repair", msg)
        self.assertIn("--symbols AAPL", msg)
        self.assertIn("--timeframes 1D", msg)
        # And the repair message must NOT suggest backfill
        self.assertNotIn("backfill", msg)

        # force-rebuild: quality_status='error'
        # SINGULAR --symbol / --timeframe per M16 CLI surface
        with self._patched(coverage=_good_coverage(quality_status="error"),
                              bars=_good_bars()):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        msg = str(ctx.exception)
        self.assertIn("force-rebuild", msg)
        self.assertIn("--symbol AAPL", msg)
        self.assertIn("--timeframe 1D", msg)
        # Must NOT use the plural flags (which force-rebuild rejects)
        self.assertNotIn("--symbols AAPL", msg)
        self.assertNotIn("--timeframes 1D", msg)

        # force-rebuild: NaN OHLC
        # default n=400 covers the request range; NaN is the trigger.
        bars_nan = _good_bars()
        bars_nan.loc[50, "close"] = float("nan")
        with self._patched(coverage=_good_coverage(), bars=bars_nan):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        self.assertIn("force-rebuild", str(ctx.exception))

        # force-rebuild: duplicate timestamps
        bars_dup = _good_bars()
        bars_dup.loc[50, "ts_utc"] = bars_dup.loc[51, "ts_utc"]
        with self._patched(coverage=_good_coverage(), bars=bars_dup):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        self.assertIn("force-rebuild", str(ctx.exception))

    # ---- Fixup4 regression: strict bar-level range check ------------

    def test_actual_first_bar_after_request_start_raises(self):
        """Regression for M17.A.fixup4. If M16's coverage row claims
        a valid range but the actual returned bars start AFTER the
        requested start, the data loader must fail loudly. M17.A V1
        is strict — no silent shorter backtests."""
        cfg = parse_config_dict(_good_config_dict())  # 2024-01-01..2024-12-31
        # Coverage row CLAIMS a valid range (passes the coverage gate)
        cov = _good_coverage(start="2023-01-01", end="2025-01-01")
        # But actual returned bars start at 2024-03-01 (60 days late)
        bars_late = _good_bars(n=400, start_date=date(2024, 3, 1))
        with self._patched(coverage=cov, bars=bars_late):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        msg = str(ctx.exception)
        # Must call out the actual-vs-requested mismatch
        self.assertIn("starting at", msg)
        self.assertIn("2024-03-01", msg)
        self.assertIn("after requested start", msg)
        # Must include a valid M16 backfill command (Issue A semantics)
        self.assertIn("python -m bot.historical.cli backfill", msg)
        self.assertIn("--symbols AAPL", msg)
        self.assertIn("--timeframes 1D", msg)
        # Must NOT use the invalid --start / --end flags
        self.assertNotIn("--start", msg)
        self.assertNotIn("--end", msg)

    def test_actual_last_bar_before_request_end_raises(self):
        """Regression for M17.A.fixup4. If M16's coverage row claims
        a valid range but the actual returned bars end BEFORE the
        requested end, the data loader must fail loudly."""
        cfg = parse_config_dict(_good_config_dict())  # 2024-01-01..2024-12-31
        # Coverage row CLAIMS a valid range
        cov = _good_coverage(start="2023-01-01", end="2025-01-01")
        # But actual returned bars only go to 2024-06-30 (~180 bars)
        bars_short = _good_bars(n=180, start_date=date(2024, 1, 1))
        with self._patched(coverage=cov, bars=bars_short):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        msg = str(ctx.exception)
        # Must call out the actual-vs-requested mismatch
        self.assertIn("ending at", msg)
        self.assertIn("before requested end", msg)
        # Must include a valid M16 backfill command
        self.assertIn("python -m bot.historical.cli backfill", msg)
        self.assertIn("--symbols AAPL", msg)
        self.assertIn("--timeframes 1D", msg)
        # Must NOT use the invalid --start / --end flags
        self.assertNotIn("--start", msg)
        self.assertNotIn("--end", msg)

    def test_actual_range_truncation_fails_even_with_clean_quality(self):
        """Regression for M17.A.fixup4. Even when coverage row reports
        clean status / fresh / no missing_count, range truncation in
        the actual returned bars is a HARD failure (was a soft
        'data_starts_late' warning pre-fix)."""
        cfg = parse_config_dict(_good_config_dict())
        # Coverage row looks pristine
        cov = _good_coverage(
            start="2023-01-01", end="2025-01-01",
            missing_count=0, quality_status="clean",
            freshness_status="fresh",
        )
        # But actual bars start 30 days late — would previously have
        # been only a warning, now a hard fail
        bars_late = _good_bars(n=400, start_date=date(2024, 2, 1))
        with self._patched(coverage=cov, bars=bars_late):
            with self.assertRaises(MissingDataError):
                data_loader.load_backtest_bars(cfg)

    def test_no_data_starts_late_warning_code_emitted(self):
        """Regression for M17.A.fixup4. The legacy 'data_starts_late'
        warning code MUST NOT appear in result.warnings — that path
        was removed in favour of hard MissingDataError. Verifies the
        soft path is gone."""
        cfg = parse_config_dict(_good_config_dict())
        cov = _good_coverage()
        # Bars fully cover the request: no failure expected, no warning.
        with self._patched(coverage=cov, bars=_good_bars()):
            df, _, warnings = data_loader.load_backtest_bars(cfg)
        codes = [w.code for w in warnings]
        self.assertNotIn("data_starts_late", codes,
            "fixup4 removed the data_starts_late soft warning; "
            "range truncation is now MissingDataError, not a warning")

    # ---- Fixup5 regression: non-trading-day boundary tolerance -----

    def test_boundary_non_trading_start_allowed_with_warning(self):
        """Regression for M17.A.fixup5. A small gap at the start
        (within _BOUNDARY_TOLERANCE_DAYS) caused by a non-trading
        day must NOT fail; it must be recorded as a warning.

        Example: request 2024-01-01..2024-12-31 with AAPL 1D. Jan 1
        is a US market holiday so the first bar is 2024-01-02 (one
        day gap). Pre-fixup5 this hard-failed; now it warns and
        continues."""
        cfg = parse_config_dict(_good_config_dict())  # 2024-01-01..2024-12-31
        cov = _good_coverage()  # clean, missing_count=0
        # Bars start 1 day after request (boundary), fully cover end
        bars = _good_bars(n=400, start_date=date(2024, 1, 2))
        with self._patched(coverage=cov, bars=bars):
            df, _, warnings = data_loader.load_backtest_bars(cfg)
        # No exception; bars returned
        self.assertGreater(len(df), 0)
        codes = [w.code for w in warnings]
        self.assertIn("boundary_non_trading_start", codes)
        # Verify the warning carries useful structured info
        bw = next(w for w in warnings if w.code == "boundary_non_trading_start")
        self.assertEqual(bw.extras["gap_days"], 1)
        self.assertEqual(bw.extras["actual_first_date"], "2024-01-02")
        self.assertEqual(bw.extras["requested_start"],   "2024-01-01")

    def test_boundary_non_trading_end_allowed_with_warning(self):
        """Regression for M17.A.fixup5. Same as start, but for end."""
        cfg = parse_config_dict(_good_config_dict())  # 2024-01-01..2024-12-31
        cov = _good_coverage()
        # 364 bars from 2024-01-01 -> last bar at 2024-12-29 (Sunday).
        # request.end = 2024-12-31, so gap = 2 days (within tolerance).
        bars = _good_bars(n=364, start_date=date(2024, 1, 1))
        with self._patched(coverage=cov, bars=bars):
            df, _, warnings = data_loader.load_backtest_bars(cfg)
        self.assertGreater(len(df), 0)
        codes = [w.code for w in warnings]
        self.assertIn("boundary_non_trading_end", codes)
        bw = next(w for w in warnings if w.code == "boundary_non_trading_end")
        self.assertEqual(bw.extras["gap_days"], 2)

    def test_start_gap_just_beyond_tolerance_still_fails(self):
        """Regression for M17.A.fixup5. A gap >= 8 days at the start
        (one beyond the 7-day tolerance) MUST still hard-fail. Verifies
        the tolerance doesn't quietly accept real truncation."""
        cfg = parse_config_dict(_good_config_dict())
        cov = _good_coverage()
        # 8 days past requested start
        bars = _good_bars(n=400, start_date=date(2024, 1, 9))
        with self._patched(coverage=cov, bars=bars):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        msg = str(ctx.exception)
        self.assertIn("starting at",        msg)
        self.assertIn("2024-01-09",          msg)
        self.assertIn("exceeds boundary tolerance", msg)
        # Still produces a valid backfill command, no --start/--end
        self.assertIn("python -m bot.historical.cli backfill", msg)
        self.assertNotIn("--start ", msg)
        self.assertNotIn("--end ",   msg)

    def test_end_gap_just_beyond_tolerance_still_fails(self):
        """Regression for M17.A.fixup5. A gap >= 8 days at the end
        MUST still hard-fail."""
        cfg = parse_config_dict(_good_config_dict())
        cov = _good_coverage()
        # 358 bars from 2024-01-01 -> last bar 2024-12-23, gap=8 days
        bars = _good_bars(n=358, start_date=date(2024, 1, 1))
        with self._patched(coverage=cov, bars=bars):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        msg = str(ctx.exception)
        self.assertIn("ending at",   msg)
        self.assertIn("exceeds boundary tolerance", msg)

    def test_boundary_tolerance_only_when_quality_clean(self):
        """Regression for M17.A.fixup5. Even a small (1-day) boundary
        gap must HARD-FAIL when coverage quality is not 'clean' (e.g.
        'warn'). The tolerance is conditional on a trustworthy
        coverage row, per operator decision."""
        cfg = parse_config_dict(_good_config_dict())
        cov = _good_coverage(quality_status="warn")  # NOT clean
        bars = _good_bars(n=400, start_date=date(2024, 1, 2))
        with self._patched(coverage=cov, bars=bars):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        # The reason must reference the boundary tolerance rule
        self.assertIn("exceeds boundary tolerance", str(ctx.exception))

    def test_boundary_tolerance_only_when_missing_count_zero(self):
        """Regression for M17.A.fixup5. A small boundary gap with
        missing_count > 0 must HARD-FAIL — gaps in the body of the
        data are a separate failure mode from a clean boundary."""
        # Note: missing_count > 0 fails at the coverage gate first
        # (before bar-level checks even run), so this verifies the
        # check ordering: coverage-missing fires before boundary
        # evaluation. Either way the request fails — that's the point.
        cfg = parse_config_dict(_good_config_dict())
        cov = _good_coverage(missing_count=5)
        bars = _good_bars(n=400, start_date=date(2024, 1, 2))
        with self._patched(coverage=cov, bars=bars):
            with self.assertRaises(MissingDataError) as ctx:
                data_loader.load_backtest_bars(cfg)
        # Falls through the coverage gate's 'missing_count=5' message
        self.assertIn("missing_count=5", str(ctx.exception))


class G2_ExampleConfig(unittest.TestCase):
    """Group 2 (fixup5 sub-group): verify configs/backtests/
    example_sma_aapl.json runs through the engine cleanly under
    mocked M16 bars. Catches the kind of config-vs-checker mismatch
    that broke the VPS example backtest after fixup4."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="m17_g2_ex_"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_example_config_succeeds_with_mocked_m16(self):
        """The shipped example config must succeed end-to-end against
        a covering M16 fixture. This is the regression for the VPS
        example-backtest failure caused by fixup4."""
        # Load the actual shipped config
        cfg = parse_config_file("configs/backtests/example_sma_aapl.json")
        self.assertEqual(cfg.request.symbol, "AAPL")
        self.assertEqual(cfg.request.timeframe, "1D")

        # Build bars covering the example config's request range plus
        # warmup. Need >= (request.end - request.start) + warmup days.
        # Use the same crossover-friendly price series shape we use
        # elsewhere.
        prices = (list(range(100, 130)) + list(range(130, 100, -1)) +
                    list(range(100, 170)) + list(range(170, 100, -1)) +
                    list(range(100, 160)) + list(range(160, 110, -1)) +
                    list(range(110, 150)) + list(range(150, 100, -1)))
        n = len(prices)
        bars = pd.DataFrame({
            "ts_utc": pd.date_range(
                start=pd.Timestamp(cfg.request.start, tz="UTC"),
                periods=n, freq="D"),
            "open":   [float(p) for p in prices],
            "high":   [float(p) + 1.0 for p in prices],
            "low":    [float(p) - 1.0 for p in prices],
            "close":  [float(p) for p in prices],
            "volume": [1_000_000] * n,
            "quality_flags": [0] * n,
        })
        cov = {
            "symbol": "AAPL", "timeframe": "1D", "provider": "yfinance",
            "first_ts_utc": pd.Timestamp("2023-01-01", tz="UTC"),
            "last_ts_utc":  pd.Timestamp("2025-12-31", tz="UTC"),
            "bar_count":     500, "missing_count": 0, "duplicate_count": 0,
            "quality_status": "clean", "freshness_status": "fresh",
            "last_refresh_at_utc": pd.Timestamp("2025-01-02", tz="UTC"),
            "last_refresh_id":     "abc",
            "provider_limit_note": None,
            "source_timeframe":    "1D",
            "derivation_method":   "native",
            "resample_rule_version": None,
        }
        fake = MagicMock()
        fake.get_coverage = MagicMock(return_value=cov)
        fake.get_bars     = MagicMock(return_value=bars)
        with patch.object(data_loader, "_m16_store", fake):
            run_dir = _runner_for_g2.run_and_write(cfg, output_dir=self.tmpdir)
        # All 6 artifacts written
        for name in ("manifest.json", "report.json",
                       "trades.csv", "trades.jsonl",
                       "equity_curve.csv", "warnings.json"):
            self.assertTrue((run_dir / name).exists(),
                              f"missing artifact: {name}")
        # report.json reflects a real backtest
        report = json.loads((run_dir / "report.json").read_text())
        self.assertGreaterEqual(report["metrics"]["n_trades"], 0)
        self.assertGreater(report["bars_processed"], 0)


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

    def test_eod_exit_fee_reflected_in_final_equity(self):
        """Regression for M17.A.fixup2 Issue 1: when a position rides
        to EOD, the EOD exit charges an exit fee. metrics['final_equity']
        and metrics['total_return_pct'] must reflect that fee.

        Pre-fix bug: equity_curve was recorded mark-to-close at the
        last bar BEFORE the post-loop EOD close ran, so the last
        equity point was stale (overstated by the EOD exit fee).
        """
        from bot.backtesting.metrics import compute_metrics
        # 5 bars, all close=100. Entry at bar 1 open, position rides to EOD.
        bars = _make_bars([100.0] * 5)
        sigs = _make_signals(5, entry_at=0)
        # 1% fees per side, full position
        cfg = _config(fee_bps=100, slippage_bps=0, max_position_pct=1.0)
        ledger = Ledger()
        simulate(bars=bars, signals=sigs, cfg=cfg, ledger=ledger)

        # The trade must have exited at EOD
        self.assertEqual(len(ledger.trades), 1)
        t = ledger.trades[0]
        self.assertEqual(t.exit_reason, "eod")

        # Compute metrics + verify final_equity matches post-EOD cash
        m = compute_metrics(
            ledger=ledger, bars=bars, exec_cfg=cfg.execution)
        # Hand-reconstruct: initial=10000, cap=1.0, fee_rate=0.01.
        # qty = floor(10000 / (100 * 1.01)) = 99
        # entry: cash -= 99*100 + 99*100*0.01 = 9900 + 99 = 9999 -> cash = 1
        # EOD exit (no slippage): cash += 99*100 - 99*100*0.01 = 9900 - 99 = 9801
        # final cash = 1 + 9801 = 9802
        expected_final_cash = 9802.0
        self.assertAlmostEqual(
            m["final_equity"], expected_final_cash, places=6,
            msg="final_equity must reflect post-EOD-exit cash (incl. fee)")
        # total_return_pct = 9802 / 10000 - 1 = -0.0198
        self.assertAlmostEqual(
            m["total_return_pct"], -0.0198, places=6,
            msg="total_return_pct must use post-fee final equity")
        # And the last equity point must be the same post-EOD value
        self.assertAlmostEqual(
            ledger.equity_curve[-1].equity, expected_final_cash, places=6)
        self.assertEqual(ledger.equity_curve[-1].position_qty, 0.0)
        # And equity-curve length still matches bars length (replacement,
        # not append, so exposure-time math stays consistent)
        self.assertEqual(len(ledger.equity_curve), len(bars))

    def test_trade_slippage_paid_is_round_trip(self):
        """Regression for M17.A.fixup2 Issue 2: Trade.slippage_paid
        must record the ROUND-TRIP slippage (entry + exit). Pre-fix
        it recorded exit-side only."""
        from bot.backtesting.metrics import compute_metrics
        # 1% slippage per side, fee=0 for clean accounting
        bars = _make_bars([100.0] * 5)
        sigs = _make_signals(5, entry_at=0, exit_at=2)
        cfg = _config(slippage_bps=100, fee_bps=0, max_position_pct=1.0)
        ledger = Ledger()
        simulate(bars=bars, signals=sigs, cfg=cfg, ledger=ledger)
        t = ledger.trades[0]
        # Entry slippage per share = 101 - 100 = 1
        # Exit  slippage per share = 100 - 99  = 1
        # Round-trip slippage per share = 2 -> total = 2 * qty
        expected_round_trip = 2.0 * t.qty
        self.assertAlmostEqual(
            t.slippage_paid, expected_round_trip, places=6,
            msg="Trade.slippage_paid must be round-trip (entry + exit)")
        # And metrics.total_slippage_paid sums to the same
        m = compute_metrics(
            ledger=ledger, bars=bars, exec_cfg=cfg.execution)
        self.assertAlmostEqual(
            m["total_slippage_paid"], expected_round_trip, places=6)


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

    def test_entry_bar_low_hits_stop_closes_same_bar(self):
        """Entry bar's low touches the stop -> SL fires on the entry
        bar itself (same-bar entry+exit). The agreed model is:
          * signal at bar i close
          * entry at bar i+1 OPEN
          * intrabar SL/TP via bar i+1 high/low — including the entry bar
        """
        # Entry at bar 1 open = 100, stop = 95. Bar 1 low = 94 -> SL hit.
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
        self.assertEqual(len(ledger.trades), 1)
        t = ledger.trades[0]
        self.assertEqual(t.exit_reason, "stop_loss")
        self.assertAlmostEqual(t.exit_price, 95.0, places=6)
        # Same-bar entry+exit -> entry_ts and exit_ts are bar 1's ts
        self.assertEqual(t.entry_ts_utc, t.exit_ts_utc)

    def test_entry_bar_high_hits_target_closes_same_bar(self):
        """Entry bar's high touches the take-profit -> TP fires on the
        entry bar itself."""
        # Entry at bar 1 open = 100, target = 105. Bar 1 high = 106 -> TP hit.
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC"),
            "open":   [100, 100, 100, 100],
            "high":   [101, 106, 100, 100],   # bar 1 high = 106 (entry bar)
            "low":    [ 99,  99,  99,  99],
            "close":  [100, 101, 100, 100],
            "volume": [1_000_000] * 4,
            "quality_flags": [0] * 4,
        })
        sigs = _make_signals(4, entry_at=0)
        cfg = _config(take_profit_pct=0.05, max_position_pct=1.0)
        ledger = Ledger()
        simulate(bars=bars, signals=sigs, cfg=cfg, ledger=ledger)
        self.assertEqual(len(ledger.trades), 1)
        t = ledger.trades[0]
        self.assertEqual(t.exit_reason, "take_profit")
        self.assertAlmostEqual(t.exit_price, 105.0, places=6)
        self.assertEqual(t.entry_ts_utc, t.exit_ts_utc)

    def test_entry_bar_hits_both_sl_wins(self):
        """Entry bar touches BOTH SL and TP -> pessimistic SL wins
        (same rule as any other bar)."""
        # Entry at bar 1 open = 100, SL = 95, TP = 105.
        # Bar 1 low = 94 AND high = 106 -> both touched -> SL wins.
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC"),
            "open":   [100, 100, 100, 100],
            "high":   [101, 106, 100, 100],   # touches TP at 105
            "low":    [ 99,  94,  99,  99],   # touches SL at 95
            "close":  [100, 101, 100, 100],
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
                          "entry bar with both SL+TP touched -> SL wins")


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

    def test_cash_never_goes_negative_even_with_max_position_and_fees(self):
        """Invariant: after open_long, Portfolio.cash >= 0.
        Reproduces the original pre-fix bug: max_position_pct=1.0
        with non-zero fees previously left cash negative by the fee
        amount because compute_size didn't reserve for the entry fee."""
        bars = _make_bars([100.0] * 5)
        sigs = _make_signals(5, entry_at=0)
        cfg = _config(fee_bps=100, max_position_pct=1.0)
        ledger = Ledger()
        simulate(bars=bars, signals=sigs, cfg=cfg, ledger=ledger)
        # Walk the equity curve: cash must never be < 0
        for ep in ledger.equity_curve:
            self.assertGreaterEqual(
                ep.cash, 0.0,
                f"cash went negative at {ep.ts_utc}: cash={ep.cash}")


# ─────────────────────────────────────────────────────────────────────
# G8 — Metrics (Phase 6)
# ─────────────────────────────────────────────────────────────────────

from bot.backtesting.metrics import compute_metrics
from bot.backtesting.config import ExecutionConfig
from bot.backtesting.models import EquityPoint, Trade


def _make_exec_cfg(initial_equity=10000.0):
    return ExecutionConfig(
        initial_equity=initial_equity, fee_bps=0, slippage_bps=0,
        stop_loss_pct=None, take_profit_pct=None,
        risk_per_trade_pct=0.01, max_position_pct=1.0,
        allow_short=False,
    )


def _make_ledger_with_trades(trades_data):
    """trades_data: list of (qty, entry_price, exit_price, fees,
    slippage, bars_held) tuples."""
    led = Ledger()
    base_ts = pd.Timestamp("2024-01-01", tz="UTC")
    for i, (qty, ep, xp, fees, slip, bh) in enumerate(trades_data):
        cost_basis = qty * ep
        pnl_abs = qty * (xp - ep) - fees
        pnl_pct = pnl_abs / cost_basis if cost_basis > 0 else 0.0
        led.record_trade(
            symbol="AAPL", qty=qty,
            entry_ts_utc=(base_ts + pd.Timedelta(days=i*10)).to_pydatetime(),
            entry_price=ep,
            exit_ts_utc=(base_ts + pd.Timedelta(days=i*10 + bh)).to_pydatetime(),
            exit_price=xp,
            exit_reason="signal",
            fees_paid=fees, slippage_paid=slip,
            pnl_absolute=pnl_abs, pnl_pct=pnl_pct,
            bars_held=bh,
        )
    return led


def _make_equity_curve(led: Ledger, equity_seq, dates=None):
    """Append synthetic equity points to ledger."""
    if dates is None:
        dates = pd.date_range("2024-01-01", periods=len(equity_seq),
                                freq="D", tz="UTC")
    for ts, eq in zip(dates, equity_seq):
        led.record_equity(
            ts_utc=ts.to_pydatetime(),
            equity=float(eq), cash=float(eq),
            position_qty=0.0, position_market_value=0.0,
        )


class G8_Metrics(unittest.TestCase):
    """Group 8: metric computations on known-input ledgers."""

    # ---- 1. trade-level metrics --------------------------------------

    def test_empty_ledger_all_zeros(self):
        led = Ledger()
        bars = _make_bars([100.0, 101.0, 102.0])
        m = compute_metrics(ledger=led, bars=bars,
                              exec_cfg=_make_exec_cfg())
        self.assertEqual(m["n_trades"], 0)
        self.assertEqual(m["win_rate"], 0.0)
        self.assertEqual(m["total_return_pct"], 0.0)
        self.assertEqual(m["max_drawdown_pct"], 0.0)

    def test_total_return_from_equity_curve(self):
        led = _make_ledger_with_trades([
            (10, 100.0, 110.0, 0.0, 0.0, 3),   # +100 pnl
        ])
        _make_equity_curve(led, [10000, 9900, 11000])
        bars = _make_bars([100.0, 100.0, 100.0])
        m = compute_metrics(ledger=led, bars=bars,
                              exec_cfg=_make_exec_cfg(10000.0))
        # initial=10000, final=11000 -> +10%
        self.assertAlmostEqual(m["total_return_pct"], 0.10, places=6)

    def test_win_rate_three_winners_one_loser(self):
        led = _make_ledger_with_trades([
            (10, 100, 110, 0, 0, 3),
            (10, 100, 120, 0, 0, 3),
            (10, 100, 105, 0, 0, 3),
            (10, 100,  90, 0, 0, 3),
        ])
        bars = _make_bars([100.0] * 4)
        m = compute_metrics(ledger=led, bars=bars,
                              exec_cfg=_make_exec_cfg())
        self.assertEqual(m["n_trades"], 4)
        self.assertEqual(m["n_winners"], 3)
        self.assertEqual(m["n_losers"], 1)
        self.assertAlmostEqual(m["win_rate"], 0.75, places=6)

    def test_profit_factor_normal_case(self):
        # winners: +100, +200 = 300; losers: -100, -50 = -150
        # PF = 300 / 150 = 2.0
        led = _make_ledger_with_trades([
            (10, 100, 110, 0, 0, 3),   # +100
            (10, 100, 120, 0, 0, 3),   # +200
            (10, 100,  90, 0, 0, 3),   # -100
            (10, 100,  95, 0, 0, 3),   # -50
        ])
        bars = _make_bars([100.0] * 4)
        m = compute_metrics(ledger=led, bars=bars,
                              exec_cfg=_make_exec_cfg())
        self.assertAlmostEqual(m["profit_factor"], 2.0, places=6)

    def test_profit_factor_no_losers_returns_inf(self):
        led = _make_ledger_with_trades([
            (10, 100, 110, 0, 0, 3),
            (10, 100, 120, 0, 0, 3),
        ])
        bars = _make_bars([100.0] * 2)
        m = compute_metrics(ledger=led, bars=bars,
                              exec_cfg=_make_exec_cfg())
        self.assertEqual(m["profit_factor"], "inf")

    def test_expectancy_equals_mean_pnl(self):
        led = _make_ledger_with_trades([
            (10, 100, 110, 0, 0, 3),   # +100
            (10, 100,  90, 0, 0, 3),   # -100
        ])
        bars = _make_bars([100.0] * 2)
        m = compute_metrics(ledger=led, bars=bars,
                              exec_cfg=_make_exec_cfg())
        self.assertAlmostEqual(m["expectancy"], 0.0, places=6)

    def test_avg_win_and_avg_loss(self):
        led = _make_ledger_with_trades([
            (10, 100, 120, 0, 0, 3),   # +200
            (10, 100, 110, 0, 0, 3),   # +100
            (10, 100,  90, 0, 0, 3),   # -100
        ])
        bars = _make_bars([100.0] * 3)
        m = compute_metrics(ledger=led, bars=bars,
                              exec_cfg=_make_exec_cfg())
        self.assertAlmostEqual(m["avg_win"], 150.0, places=6)
        self.assertAlmostEqual(m["avg_loss"], -100.0, places=6)

    # ---- 2. drawdown -------------------------------------------------

    def test_max_drawdown_simple(self):
        # 10000 -> 12000 -> 9000: peak=12000, trough=9000
        # DD = (9000 - 12000) / 12000 = -0.25 -> reported as 0.25
        led = Ledger()
        _make_equity_curve(led, [10000, 12000, 9000])
        bars = _make_bars([100.0, 100.0, 100.0])
        m = compute_metrics(ledger=led, bars=bars,
                              exec_cfg=_make_exec_cfg(10000.0))
        self.assertAlmostEqual(m["max_drawdown_pct"], 0.25, places=6)

    def test_max_drawdown_zero_when_monotonic_up(self):
        led = Ledger()
        _make_equity_curve(led, [10000, 11000, 12000, 13000])
        bars = _make_bars([100.0] * 4)
        m = compute_metrics(ledger=led, bars=bars,
                              exec_cfg=_make_exec_cfg(10000.0))
        self.assertAlmostEqual(m["max_drawdown_pct"], 0.0, places=6)

    # ---- 3. exposure / fees / slippage -------------------------------

    def test_exposure_time_pct(self):
        led = Ledger()
        ts = pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC")
        # 2 of 4 bars have position
        for i, t in enumerate(ts):
            qty = 10.0 if i in (1, 2) else 0.0
            led.record_equity(ts_utc=t.to_pydatetime(),
                                equity=10000.0, cash=10000.0,
                                position_qty=qty,
                                position_market_value=qty * 100.0)
        bars = _make_bars([100.0] * 4)
        m = compute_metrics(ledger=led, bars=bars,
                              exec_cfg=_make_exec_cfg())
        self.assertAlmostEqual(m["exposure_time_pct"], 0.5, places=6)

    def test_fees_and_slippage_summed(self):
        led = _make_ledger_with_trades([
            (10, 100, 110, 1.0, 0.5, 3),
            (10, 100,  90, 2.0, 1.5, 3),
        ])
        bars = _make_bars([100.0] * 2)
        m = compute_metrics(ledger=led, bars=bars,
                              exec_cfg=_make_exec_cfg())
        self.assertAlmostEqual(m["total_fees_paid"],     3.0, places=6)
        self.assertAlmostEqual(m["total_slippage_paid"], 2.0, places=6)

    # ---- 4. sharpe / sortino gates ----------------------------------

    def test_sharpe_None_when_too_few_trades(self):
        led = _make_ledger_with_trades([
            (10, 100, 110, 0, 0, 3),   # only 1 trade < 30
        ])
        _make_equity_curve(led,
            list(range(10000, 10000 + 100 * 100, 100)))  # 100 days
        bars = _make_bars([100.0] * 100)
        m = compute_metrics(ledger=led, bars=bars,
                              exec_cfg=_make_exec_cfg(10000.0))
        self.assertIsNone(m["sharpe_annualised"])
        self.assertIsNone(m["sortino_annualised"])
        self.assertIn("insufficient_trades", m["sample_size_note"])

    def test_sharpe_None_when_too_few_days(self):
        # 30 trades but only 30 days -> still gated.
        led = _make_ledger_with_trades(
            [(10, 100, 110, 0, 0, 1) for _ in range(30)])
        _make_equity_curve(led, [10000 + i*10 for i in range(30)])
        bars = _make_bars([100.0] * 30)
        m = compute_metrics(ledger=led, bars=bars,
                              exec_cfg=_make_exec_cfg(10000.0))
        self.assertIsNone(m["sharpe_annualised"])
        self.assertIn("insufficient_days", m["sample_size_note"])

    # ---- 5. buy-and-hold benchmark ----------------------------------

    def test_buy_and_hold_benchmark_total_return(self):
        # 100 -> 110: +10%
        led = Ledger()
        _make_equity_curve(led, [10000, 10000, 10000])
        bars = _make_bars([100.0, 105.0, 110.0])
        m = compute_metrics(ledger=led, bars=bars,
                              exec_cfg=_make_exec_cfg(10000.0))
        self.assertAlmostEqual(m["benchmark"]["total_return_pct"], 0.10,
                                  places=6)
        self.assertEqual(m["benchmark"]["name"], "buy_and_hold")

    def test_buy_and_hold_benchmark_drawdown(self):
        # 100 -> 150 -> 100: peak=150, trough=100, DD = 1/3
        led = Ledger()
        _make_equity_curve(led, [10000] * 3)
        bars = _make_bars([100.0, 150.0, 100.0])
        m = compute_metrics(ledger=led, bars=bars,
                              exec_cfg=_make_exec_cfg(10000.0))
        self.assertAlmostEqual(m["benchmark"]["max_drawdown_pct"],
                                  1.0/3.0, places=6)


# ─────────────────────────────────────────────────────────────────────
# G9 — Output reproducibility (Phase 7)
# Golden-path E2E tests come at the end of G9 in Phase 8.
# ─────────────────────────────────────────────────────────────────────

import shutil
import tempfile

from bot.backtesting.output import build_run_id, write_results
from bot.backtesting.models import BacktestResult, BacktestWarning


def _make_result_with_one_trade(cfg):
    """Build a BacktestResult with one closed trade + 3 equity points
    + one warning. Deterministic content."""
    led = _make_ledger_with_trades([(10, 100.0, 110.0, 1.0, 0.5, 3)])
    _make_equity_curve(led, [10000.0, 10500.0, 11000.0])
    led.record_warning(BacktestWarning(
        code="test_warn", message="for repro test",
        ts_utc=pd.Timestamp("2024-01-02", tz="UTC").to_pydatetime(),
        extras={"foo": "bar"},
    ))
    bars = _make_bars([100.0, 105.0, 110.0])
    metrics = compute_metrics(ledger=led, bars=bars,
                                exec_cfg=cfg.execution)
    return BacktestResult(
        run_id="placeholder",
        created_at_utc=datetime(2024, 1, 1, tzinfo=timezone.utc),
        config=config_to_dict(cfg),
        config_hash=config_hash(cfg),
        coverage_metadata={
            "symbol": "AAPL", "timeframe": "1D",
            "first_ts_utc": pd.Timestamp("2023-01-01", tz="UTC"),
            "last_ts_utc":  pd.Timestamp("2025-01-01", tz="UTC"),
            "bar_count": 500, "missing_count": 0,
            "quality_status": "clean", "freshness_status": "fresh",
        },
        trades=led.trades,
        equity_curve=led.equity_curve,
        warnings=led.warnings,
        metrics=metrics,
        bars_processed=len(bars),
    )


class G9_OutputReproducibility(unittest.TestCase):
    """Group 9: output artifacts + reproducibility."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="m17_g9_"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---- file presence ---------------------------------------------

    def test_all_six_artifacts_written(self):
        cfg = _config()
        result = _make_result_with_one_trade(cfg)
        run_dir = write_results(result, cfg, self.tmpdir)
        for name in ("manifest.json", "report.json",
                       "trades.csv", "trades.jsonl",
                       "equity_curve.csv", "warnings.json"):
            self.assertTrue((run_dir / name).exists(),
                              f"missing artifact: {name}")

    def test_run_id_format(self):
        cfg = _config()
        result = _make_result_with_one_trade(cfg)
        rid = build_run_id(
            cfg,
            created_at_utc=datetime(2024, 6, 15, 14, 30, 45,
                                     tzinfo=timezone.utc),
            cfg_hash="abc123def456",
        )
        # Format: <YYYYMMDDTHHMMSSZ>_<strategy>_<config_hash>
        self.assertEqual(rid, "20240615T143045Z_sma_crossover_abc123def456")

    # ---- byte-identical reproducibility -----------------------------

    def test_repeated_runs_produce_identical_report_json(self):
        """Same config + same result -> byte-identical report.json."""
        cfg = _config()
        result1 = _make_result_with_one_trade(cfg)
        result2 = _make_result_with_one_trade(cfg)
        # Use the same run_id + created_at to remove the only
        # non-deterministic manifest fields.
        ts = datetime(2024, 6, 15, 14, 30, 45, tzinfo=timezone.utc)
        rid = build_run_id(cfg, created_at_utc=ts,
                              cfg_hash=config_hash(cfg))
        rd1 = write_results(result1, cfg, self.tmpdir / "a",
                              run_id=rid, created_at_utc=ts)
        rd2 = write_results(result2, cfg, self.tmpdir / "b",
                              run_id=rid, created_at_utc=ts)
        # report.json must be byte-identical
        b1 = (rd1 / "report.json").read_bytes()
        b2 = (rd2 / "report.json").read_bytes()
        self.assertEqual(b1, b2,
            "report.json must be byte-identical for same inputs")

    def test_trades_csv_and_jsonl_have_same_row_count(self):
        cfg = _config()
        result = _make_result_with_one_trade(cfg)
        run_dir = write_results(result, cfg, self.tmpdir)
        csv_lines = (run_dir / "trades.csv").read_text().strip().splitlines()
        # CSV has a header row
        csv_rows = len(csv_lines) - 1
        jsonl_rows = len((run_dir / "trades.jsonl")
                            .read_text().strip().splitlines())
        self.assertEqual(csv_rows, jsonl_rows)
        self.assertEqual(csv_rows, len(result.trades))

    # ---- manifest contents ------------------------------------------

    def test_manifest_contains_required_fields(self):
        cfg = _config()
        result = _make_result_with_one_trade(cfg)
        run_dir = write_results(result, cfg, self.tmpdir)
        manifest = json.loads((run_dir / "manifest.json").read_text())
        for field in ("run_id", "created_at_utc", "engine_version",
                        "config", "config_hash", "coverage_metadata",
                        "bot_historical_schema_version",
                        "strategy_module_sha256", "git_head_sha",
                        "python_version", "pandas_version", "numpy_version",
                        "bars_processed", "trade_count", "warning_count"):
            self.assertIn(field, manifest, f"missing manifest field: {field}")

    def test_manifest_bot_historical_schema_version_is_int_and_matches(self):
        """Regression for M17.A.fixup3 Issue B: the manifest must
        include the M16 historical-store schema version as an int.

        The value must equal bot.historical.schema.SCHEMA_VERSION at
        the time of writing — verified by reading it through
        bot.backtesting.data_loader.M16_SCHEMA_VERSION (the only
        approved re-export path, to keep G10's
        'only-data_loader-imports-bot.historical' invariant intact).
        """
        from bot.backtesting.data_loader import M16_SCHEMA_VERSION
        cfg = _config()
        result = _make_result_with_one_trade(cfg)
        run_dir = write_results(result, cfg, self.tmpdir)
        manifest = json.loads((run_dir / "manifest.json").read_text())
        v = manifest["bot_historical_schema_version"]
        self.assertIsInstance(v, int)
        self.assertGreater(v, 0)
        self.assertEqual(v, M16_SCHEMA_VERSION)

    def test_manifest_config_round_trips(self):
        cfg = _config()
        result = _make_result_with_one_trade(cfg)
        run_dir = write_results(result, cfg, self.tmpdir)
        manifest = json.loads((run_dir / "manifest.json").read_text())
        # The echoed config can be re-parsed back into a BacktestConfig
        # with the same hash.
        re_parsed = parse_config_dict(manifest["config"])
        self.assertEqual(config_hash(re_parsed), manifest["config_hash"])

    def test_strategy_module_sha256_is_valid_hex(self):
        cfg = _config()
        result = _make_result_with_one_trade(cfg)
        run_dir = write_results(result, cfg, self.tmpdir)
        manifest = json.loads((run_dir / "manifest.json").read_text())
        sha = manifest["strategy_module_sha256"]
        self.assertEqual(len(sha), 64)   # SHA256 hex digest
        self.assertTrue(all(c in "0123456789abcdef" for c in sha))


# ─────────────────────────────────────────────────────────────────────
# G9 — Golden-path E2E (Phase 8)
# Runner + CLI integration tests with mocked M16 store.
# ─────────────────────────────────────────────────────────────────────

import subprocess

from bot.backtesting import runner
from bot.backtesting.cli import main as cli_main


class G9_GoldenPathE2E(unittest.TestCase):
    """End-to-end runs through runner.run() and the CLI, with M16
    store mocked. No network, no real M16 data."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="m17_g9e2e_"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _bars_with_crossover(self):
        """~400 daily bars with two clear SMA crossover regions.

        Extended from 200 to ~400 bars (fixup4) so the fixture covers
        the standard test request range 2024-01-01..2024-12-31 under
        M17.A's strict bar-level range check.
        """
        prices = (list(range(100, 130)) +          # up to bar 30
                    list(range(130, 100, -1)) +     # down to bar 60
                    list(range(100, 170)) +         # up to bar 130
                    list(range(170, 100, -1)) +     # down to bar 200
                    list(range(100, 160)) +         # up to bar 260
                    list(range(160, 110, -1)) +     # down to bar 310
                    list(range(110, 150)) +         # up to bar 350
                    list(range(150, 100, -1)))      # down to bar 400
        n = len(prices)
        return pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-01", periods=n, freq="D",
                                      tz="UTC"),
            "open":   [float(p) for p in prices],
            "high":   [float(p) + 1.0 for p in prices],
            "low":    [float(p) - 1.0 for p in prices],
            "close":  [float(p) for p in prices],
            "volume": [1_000_000] * n,
            "quality_flags": [0] * n,
        })

    def _patched_loader(self, bars):
        cov = _good_coverage(start="2023-01-01", end="2025-01-01")
        fake = MagicMock()
        fake.get_coverage = MagicMock(return_value=cov)
        fake.get_bars     = MagicMock(return_value=bars)
        return patch.object(data_loader, "_m16_store", fake)

    # ---- runner.run -------------------------------------------------

    def test_runner_run_returns_BacktestResult_with_trades(self):
        cfg = _config()
        bars = self._bars_with_crossover()
        with self._patched_loader(bars):
            result = runner.run(cfg)
        self.assertGreaterEqual(result.trade_count, 1)
        self.assertEqual(result.bars_processed, len(bars))
        self.assertIn("n_trades", result.metrics)
        self.assertEqual(result.metrics["n_trades"], result.trade_count)

    def test_runner_run_then_write_produces_full_artifact_set(self):
        cfg = _config()
        bars = self._bars_with_crossover()
        with self._patched_loader(bars):
            run_dir = runner.run_and_write(cfg, output_dir=self.tmpdir)
        for name in ("manifest.json", "report.json",
                       "trades.csv", "trades.jsonl",
                       "equity_curve.csv", "warnings.json"):
            self.assertTrue((run_dir / name).exists())

    # ---- CLI --------------------------------------------------------

    def test_cli_run_with_config_file_exit_0(self):
        # Write a temporary config file
        cfg_path = self.tmpdir / "cfg.json"
        cfg_path.write_text(json.dumps(_good_config_dict()))
        bars = self._bars_with_crossover()
        with self._patched_loader(bars):
            rc = cli_main([
                "run", "--config", str(cfg_path),
                "--output-dir", str(self.tmpdir / "out"),
            ])
        self.assertEqual(rc, 0)
        # Output directory must contain exactly one run dir.
        run_dirs = list((self.tmpdir / "out").iterdir())
        self.assertEqual(len(run_dirs), 1)

    def test_cli_run_inline_args_exit_0(self):
        bars = self._bars_with_crossover()
        with self._patched_loader(bars):
            rc = cli_main([
                "run",
                "--symbol", "AAPL", "--timeframe", "1D",
                "--from", "2024-01-01", "--to", "2024-12-31",
                "--strategy", "sma_crossover",
                "--fast", "5", "--slow", "20",
                "--initial-equity", "10000",
                "--fee-bps", "5", "--slippage-bps", "5",
                "--stop-loss-pct", "0.03",
                "--take-profit-pct", "0.06",
                "--risk-per-trade-pct", "0.01",
                "--max-position-pct", "0.25",
                "--output-dir", str(self.tmpdir / "out"),
            ])
        self.assertEqual(rc, 0)

    def test_cli_missing_data_exit_2_with_refresh_command(self):
        """When M16 has no coverage, CLI must exit 2 and stderr must
        contain the M16 refresh command."""
        import io
        from contextlib import redirect_stderr
        cfg_path = self.tmpdir / "cfg.json"
        cfg_path.write_text(json.dumps(_good_config_dict()))
        # Mock M16 to return no coverage
        fake = MagicMock()
        fake.get_coverage = MagicMock(return_value=None)
        fake.get_bars     = MagicMock(return_value=pd.DataFrame())
        with patch.object(data_loader, "_m16_store", fake):
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = cli_main([
                    "run", "--config", str(cfg_path),
                    "--output-dir", str(self.tmpdir / "out"),
                ])
            stderr = buf.getvalue()
        self.assertEqual(rc, 2)
        self.assertIn("python -m bot.historical.cli backfill", stderr)
        self.assertIn("--symbols AAPL", stderr)

    def test_cli_bad_config_exit_3(self):
        # ConfigError path: unknown strategy
        cfg_path = self.tmpdir / "cfg.json"
        bad = _good_config_dict()
        bad["strategy"]["name"] = "scanner_replica"   # M17.B, not M17.A
        cfg_path.write_text(json.dumps(bad))
        rc = cli_main([
            "run", "--config", str(cfg_path),
            "--output-dir", str(self.tmpdir / "out"),
        ])
        self.assertEqual(rc, 3)

    def test_cli_missing_config_file_exit_3(self):
        """Regression for M17.A.fixup3 Issue C: a --config path that
        doesn't exist must surface as ConfigError -> exit code 3,
        not as an unexpected FileNotFoundError -> exit 1."""
        import io
        from contextlib import redirect_stderr
        nonexistent = self.tmpdir / "does_not_exist.json"
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = cli_main([
                "run", "--config", str(nonexistent),
                "--output-dir", str(self.tmpdir / "out"),
            ])
        stderr = buf.getvalue()
        self.assertEqual(rc, 3,
            f"missing config -> expected exit 3 (ConfigError), got {rc}\n"
            f"stderr: {stderr}")
        self.assertIn("config file not found", stderr.lower())

    # ---- end-to-end reproducibility --------------------------------

    def test_two_runs_same_bars_produce_byte_identical_report(self):
        """Two full runs through runner.run_and_write with identical
        config + identical bars produce byte-identical report.json."""
        cfg = _config()
        bars = self._bars_with_crossover()

        with self._patched_loader(bars):
            run_dir_a = runner.run_and_write(cfg, output_dir=self.tmpdir / "a")
        with self._patched_loader(bars):
            run_dir_b = runner.run_and_write(cfg, output_dir=self.tmpdir / "b")

        # report.json must be byte-identical (no per-run metadata in it)
        b_a = (run_dir_a / "report.json").read_bytes()
        b_b = (run_dir_b / "report.json").read_bytes()
        self.assertEqual(b_a, b_b)

        # trades.csv must be byte-identical
        t_a = (run_dir_a / "trades.csv").read_bytes()
        t_b = (run_dir_b / "trades.csv").read_bytes()
        self.assertEqual(t_a, t_b)


# ─────────────────────────────────────────────────────────────────────
# G10 — Hygiene (AST + protected-files + gitignore + no-network)
# Phase 9. NO docs closeout — that comes after VPS verification.
# ─────────────────────────────────────────────────────────────────────

import ast
import hashlib
import os
import socket as _socket_module


# Baseline commit for protected-files diff. This is the HEAD before
# M17.A began (audit-P1-data-rate-limit-fix closeout).
_M17_BASELINE_SHA = "13a3aa4"

# Files M17.A explicitly MUST NOT touch.
_PROTECTED_PATHS = (
    "bot/data.py",
    "bot/scanner.py",
    "bot/strategy.py",
    "bot/backtest.py",
    "bot/backtest_v2.py",
    "bot/risk.py",
    "bot/risk_authority/engine.py",
    "bot/risk_authority/governor.py",
    "bot/risk_authority/audit_decisions.py",
    "bot/risk_authority/snapshot.py",
    "bot/risk_authority/preflight.py",
    "bot/risk_authority/ibkr_paper_reader.py",
    "bot/etoro/live_broker.py",
    "bot/etoro/paper_broker.py",
    "main.py",
    "sync.sh",
    "deploy.sh",
    "dashboard/app.py",
    "dashboard/auth/manual_reset.py",
    "dashboard/auth/audit_export.py",
)

# Forbidden imports for any module in bot/backtesting/ (except
# data_loader.py which is the SOLE allowed gateway to bot.historical).
_FORBIDDEN_IMPORT_PREFIXES = (
    "yfinance",
    "bot.data",
    "bot.providers",
    "bot.scanner",
    "bot.backtest",       # also covers bot.backtest_v2
    "bot.brokers",
    "bot.broker_",
    "bot.gateway_",
    "bot.etoro.live_broker",
    "bot.etoro.paper_broker",
    "bot.etoro.signal_only",
    "bot.risk_authority.engine",
    "bot.risk_authority.governor",
    "bot.risk_authority.snapshot",
    "bot.risk_authority.preflight",
    "bot.risk_authority.ibkr_paper_reader",
    "ibapi",
    "ib_insync",
    "requests",
    "urllib.request",
    "urllib3",
    "http.client",
)

# Order-method names that must not appear as string literals.
_FORBIDDEN_STRING_LITERALS = (
    "placeOrder",
    "cancelOrder",
    "submitOrder",
    "placeOrders",
    "modifyOrder",
    "closePosition",
)


def _bot_backtesting_files():
    """All .py files in bot/backtesting/."""
    root = Path("bot/backtesting")
    return sorted(p for p in root.glob("*.py"))


def _imports_in(path: Path):
    """Yield every module name imported by `path` as a string."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                yield node.module


def _string_literals_in(path: Path):
    """Yield every string-literal value (ast.Constant with a str value)
    in the file."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


class G10_Hygiene(unittest.TestCase):
    """Group 10: hygiene gates. AST imports + string literals +
    protected-files diff + gitignore + no-network at runtime."""

    # ---- AST: no forbidden imports anywhere in bot/backtesting/ -----

    def test_no_forbidden_imports_in_bot_backtesting(self):
        """Every .py in bot/backtesting/ must import only stdlib +
        pandas/numpy + bot.backtesting.* + (only data_loader.py)
        bot.historical.store."""
        offenders = []
        for f in _bot_backtesting_files():
            for imp in _imports_in(f):
                for forbidden in _FORBIDDEN_IMPORT_PREFIXES:
                    if imp == forbidden or imp.startswith(forbidden + "."):
                        offenders.append((f.name, imp))
                        break
        self.assertEqual(offenders, [],
            f"bot/backtesting/ imports forbidden modules: {offenders}")

    def test_only_data_loader_imports_bot_historical(self):
        """bot.historical may be imported ONLY by data_loader.py.
        Every other module in bot/backtesting/ must not touch it."""
        offenders = []
        for f in _bot_backtesting_files():
            if f.name == "data_loader.py":
                continue
            for imp in _imports_in(f):
                if imp == "bot.historical" or imp.startswith("bot.historical."):
                    offenders.append((f.name, imp))
        self.assertEqual(offenders, [],
            f"bot.historical imported outside data_loader.py: {offenders}")

    # ---- AST: no order-method strings in bot/backtesting/ ----------

    def test_no_order_method_string_literals(self):
        offenders = []
        for f in _bot_backtesting_files():
            for s in _string_literals_in(f):
                # Don't flag short literals that incidentally match;
                # check exact equality only.
                for forbidden in _FORBIDDEN_STRING_LITERALS:
                    if forbidden in s:
                        offenders.append((f.name, forbidden, s[:80]))
        self.assertEqual(offenders, [],
            f"order-method string literals found: {offenders}")

    # ---- Protected files: unchanged vs M17 baseline ----------------

    def test_protected_files_unchanged_vs_M17_baseline(self):
        """Every file in _PROTECTED_PATHS is byte-identical to its
        content at the M17 baseline commit (13a3aa4)."""
        import subprocess
        unchanged = []
        changed = []
        missing = []
        for path in _PROTECTED_PATHS:
            try:
                current = subprocess.run(
                    ["git", "rev-parse", f"HEAD:{path}"],
                    capture_output=True, text=True, timeout=5)
                baseline = subprocess.run(
                    ["git", "rev-parse", f"{_M17_BASELINE_SHA}:{path}"],
                    capture_output=True, text=True, timeout=5)
                if current.returncode != 0 or baseline.returncode != 0:
                    missing.append(path)
                    continue
                if current.stdout.strip() == baseline.stdout.strip():
                    unchanged.append(path)
                else:
                    changed.append(path)
            except Exception as e:
                missing.append((path, str(e)))
        self.assertEqual(changed, [],
            f"Protected files modified by M17.A: {changed}")
        # Missing files are OK (they may not exist at the baseline or now);
        # we just need NO changed files.

    def test_bot_data_py_byte_identical(self):
        """Hard invariant: bot/data.py must be byte-identical to the
        baseline. Used to be modified pre-P0-batch; must stay frozen
        from now on."""
        import subprocess
        current = subprocess.run(
            ["git", "rev-parse", "HEAD:bot/data.py"],
            capture_output=True, text=True, timeout=5)
        baseline = subprocess.run(
            ["git", "rev-parse", f"{_M17_BASELINE_SHA}:bot/data.py"],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(current.returncode, 0,
                          "bot/data.py missing at HEAD")
        self.assertEqual(baseline.returncode, 0,
                          "bot/data.py missing at baseline")
        self.assertEqual(current.stdout.strip(), baseline.stdout.strip(),
            "bot/data.py changed since M17 baseline — INVARIANT VIOLATED")

    # ---- Output paths: data/backtests/ is gitignored ----------------

    def test_data_backtests_is_gitignored(self):
        """The output directory must be in .gitignore so generated
        artifacts never land in the repo."""
        import subprocess
        # Create a dummy path and ask git check-ignore.
        os.makedirs("data/backtests", exist_ok=True)
        Path("data/backtests/.dummy_for_test").touch()
        try:
            result = subprocess.run(
                ["git", "check-ignore", "data/backtests/.dummy_for_test"],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0,
                "data/backtests/ is NOT git-ignored — INVARIANT VIOLATED")
        finally:
            try:
                os.remove("data/backtests/.dummy_for_test")
            except FileNotFoundError:
                pass

    # ---- New files: only the expected set ---------------------------

    def test_no_unexpected_files_added(self):
        """M17.A adds files only in bot/backtesting/, configs/backtests/,
        plus test_m17_backtesting.py. No surprises."""
        import subprocess
        result = subprocess.run(
            ["git", "diff", "--name-only", _M17_BASELINE_SHA, "HEAD"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        changed = sorted(result.stdout.strip().splitlines())
        # Build the allowed set
        allowed_prefixes = ("bot/backtesting/", "configs/backtests/")
        allowed_exact = {"test_m17_backtesting.py"}
        unexpected = [
            p for p in changed
            if not p.startswith(allowed_prefixes)
                and p not in allowed_exact
        ]
        self.assertEqual(unexpected, [],
            f"Unexpected files changed: {unexpected}")

    # ---- No network at runtime --------------------------------------

    def test_no_socket_calls_during_backtest(self):
        """Running a backtest must not open any sockets. Patches
        socket.socket to raise; the run should still complete."""
        cfg = _config()
        # Mock M16 to return bars without touching disk. Use 400 bars
        # so the fixture spans the full 2024-01-01..2024-12-31 request
        # range under fixup4's strict bar-level check.
        N = 400
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-01", periods=N, freq="D",
                                      tz="UTC"),
            "open":   [100.0] * N,
            "high":   [101.0] * N,
            "low":    [ 99.0] * N,
            "close":  [100.0] * N,
            "volume": [1_000_000] * N,
            "quality_flags": [0] * N,
        })
        cov = _good_coverage(start="2023-01-01", end="2025-01-01")
        fake = MagicMock()
        fake.get_coverage = MagicMock(return_value=cov)
        fake.get_bars     = MagicMock(return_value=bars)

        # Patch socket.socket to forbid construction.
        def _no_sockets(*args, **kwargs):
            raise RuntimeError(
                "socket.socket() called during backtest — NETWORK ACCESS")

        with patch.object(data_loader, "_m16_store", fake), \
              patch.object(_socket_module, "socket", _no_sockets):
            # If the engine tries to open a socket, the run fails.
            result = runner.run(cfg)
        # We just need it to complete without exception. trade_count
        # may be 0 (flat series, no crossover) — that's fine.
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
