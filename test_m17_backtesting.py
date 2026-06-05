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


if __name__ == "__main__":
    unittest.main(verbosity=2)
