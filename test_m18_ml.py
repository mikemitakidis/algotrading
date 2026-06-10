"""M18 test suite — G1 (CLI surface) and G10 (hygiene).

This file accumulates G2..G8 test blocks across M18.A.2 through
M18.A.8. The initial skeleton (this commit) contains the imports
and the G10 Hygiene block; later phases extend it.
"""
from __future__ import annotations

import ast
import io
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pandas as pd

# Import the modules under test
from bot.ml import errors as ml_errors
from bot.ml import schemas as ml_schemas
from bot.ml import hashing as ml_hashing
from bot.ml import cli as ml_cli
from bot.ml.dataset import m16_loader
from bot.ml.dataset import flywheel_reader
from bot.ml.features import (
    price_return, trend, momentum, vol_regime, volume_liquidity,
    mtf_confluence, scanner_replica, market_context, symbol_meta,
    signal_history,
)
from bot.ml.labels import (
    triple_barrier, forward_returns, mfe_mae, risk_adjusted,
)
from bot.ml.labels.base import assert_label_resolved_after_anchor
import sqlite3


# Path constants
_REPO_ROOT = Path(__file__).parent
_BOT_ML_DIR = _REPO_ROOT / "bot" / "ml"


def _walk_bot_ml_py_files():
    """Yield every .py file under bot/ml/, excluding __pycache__."""
    for f in _BOT_ML_DIR.rglob("*.py"):
        if "__pycache__" in f.parts:
            continue
        yield f


# G10 whitelist — directories M18 is allowed to add files in.
# Anything created outside these directories is flagged by G10's
# test_no_unexpected_files_added.
_M18_WHITELIST_PREFIXES = (
    "bot/ml/",
    "configs/ml/",
    "docs/M18",
    "test_m18_ml.py",
)


# ═════════════════════════════════════════════════════════════════════
# G2 — M16 loader + safe feature groups (M18.A.2)
# ═════════════════════════════════════════════════════════════════════
#
# SR-4 tolerances (mirrors M17.B):
#   _PARITY_RTOL_SYNTH = 1e-9   for parity vs bot.backtesting.indicators
#   _PARITY_ATOL       = 1e-8   absolute floor for near-zero values
#
# Parity tests below compare M18 feature output bit-by-bit against
# the M17.B indicator helpers in bot.backtesting.indicators. Production
# bot/ml/* code does NOT import that module (the M18 feature modules
# reimplement the math); only test_m18_ml.py imports it for parity.

_PARITY_RTOL_SYNTH = 1e-9
_PARITY_ATOL       = 1e-8


def _make_synthetic_bars(n: int = 300, seed: int = 42,
                          start_price: float = 100.0,
                          drift: float = 0.0005,
                          vol: float = 0.015) -> pd.DataFrame:
    """Generate deterministic synthetic OHLCV bars for testing.

    Uses numpy.random.default_rng(seed) — no global RNG state.
    Returns a DataFrame matching the M16 loader contract:
    ts_utc (UTC), open, high, low, close, volume, quality_flags.
    """
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2024-01-02", periods=n, freq="1D", tz="UTC")
    returns = rng.normal(drift, vol, size=n)
    close = start_price * np.exp(np.cumsum(returns))
    # open = previous close (gap-free synthetic)
    open_ = np.concatenate([[start_price], close[:-1]])
    # high/low form a non-degenerate envelope around (open, close)
    spread = np.abs(rng.normal(0.0, 0.008, size=n)) * close + 0.01
    high = np.maximum(open_, close) + spread / 2.0
    low  = np.minimum(open_, close) - spread / 2.0
    volume = rng.integers(1_000_000, 10_000_000, size=n).astype(float)
    return pd.DataFrame({
        "ts_utc": ts,
        "open":   open_.astype(float),
        "high":   high.astype(float),
        "low":    low.astype(float),
        "close":  close.astype(float),
        "volume": volume,
        "quality_flags": 0,
    })


class G2_M16Loader(unittest.TestCase):
    """SR-7 — bot/ml/dataset/m16_loader is the SOLE bot.historical
    importer. This class tests its contract and error semantics; the
    AST guard that bot.historical doesn't leak elsewhere lives in G10.
    """

    def test_m16_loader_imports_bot_historical(self):
        """The loader is the only file that legitimately imports
        bot.historical in bot/ml/* — sanity-check it does so."""
        f = Path(__file__).parent / "bot" / "ml" / "dataset" / "m16_loader.py"
        imports = set(_imports_in_file(f))
        # Must import bot.historical (the loader's whole purpose)
        self.assertTrue(
            any(i == "bot.historical" or i.startswith("bot.historical.")
                for i in imports),
            f"m16_loader.py must import bot.historical; got {imports}")

    def test_raises_M16CoverageError_below_min_rows(self):
        """Empty / too-thin coverage raises with backfill command."""
        with tempfile.TemporaryDirectory() as td:
            # M16 store layout: <root>/<provider>/<tf>/<symbol>.parquet
            # We deliberately do NOT create any file → no coverage.
            os.environ.setdefault("BOT_HISTORICAL_ROOT", td)
            # Even without configured root, get_bars returns an empty
            # frame for a non-existent path — our loader must convert
            # that empty frame into M16CoverageError.
            with self.assertRaises(ml_errors.M16CoverageError) as cm:
                m16_loader.load_bars(
                    "NONEXISTENT_SYMBOL_XYZ", "1D", min_rows=1)
            msg = str(cm.exception)
            self.assertIn("bot.historical.cli refresh", msg,
                "error message must include explicit backfill command")
            self.assertIn("NONEXISTENT_SYMBOL_XYZ", msg)

    def test_validate_lookback_coverage_pass(self):
        """50 bars satisfy a 14-bar lookback (need 15)."""
        bars = _make_synthetic_bars(n=50)
        # Should not raise.
        m16_loader.validate_lookback_coverage(
            bars, lookback_bars=14, feature_name="rsi_14")

    def test_validate_lookback_coverage_fail_with_feature_name(self):
        """Too-short bars raise with the feature name in the message
        so the user can find the source of the failure."""
        bars = _make_synthetic_bars(n=10)
        with self.assertRaises(ml_errors.M16CoverageError) as cm:
            m16_loader.validate_lookback_coverage(
                bars, lookback_bars=50,
                feature_name="trend.sma_distance_50")
        self.assertIn("trend.sma_distance_50", str(cm.exception))

    def test_validate_lookback_coverage_rejects_negative_lookback(self):
        bars = _make_synthetic_bars(n=10)
        with self.assertRaises(ValueError):
            m16_loader.validate_lookback_coverage(
                bars, lookback_bars=-1)

    def test_assert_utc_index_rejects_naive(self):
        bars = _make_synthetic_bars(n=10)
        bars["ts_utc"] = bars["ts_utc"].dt.tz_localize(None)
        with self.assertRaises(ml_errors.M16CoverageError):
            m16_loader.assert_utc_index(bars)

    def test_assert_utc_index_accepts_utc(self):
        bars = _make_synthetic_bars(n=10)
        # Should not raise.
        m16_loader.assert_utc_index(bars)


# ─────────────────────────────────────────────────────────────────────
# G2_PriceReturn — 13 features
# ─────────────────────────────────────────────────────────────────────

class G2_PriceReturn(unittest.TestCase):

    def test_close_passthrough(self):
        bars = _make_synthetic_bars(n=50)
        out = price_return.compute(bars)
        np.testing.assert_allclose(
            out["price_return.close"].to_numpy(),
            bars["close"].to_numpy(),
            rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)

    def test_log_ret_1_known_values(self):
        # Construct a series where each bar is +1% over the previous.
        # log_ret_1 should be ln(1.01) ≈ 0.00995... for every bar
        # after the warmup (which is 1 bar for log_ret_1).
        n = 30
        close = 100.0 * np.power(1.01, np.arange(n))
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open":   close,
            "high":   close * 1.001,
            "low":    close * 0.999,
            "close":  close,
            "volume": np.full(n, 1_000_000.0),
            "quality_flags": 0,
        })
        out = price_return.compute(bars)
        ret = out["price_return.log_ret_1"]
        self.assertTrue(pd.isna(ret.iloc[0]))   # warmup
        np.testing.assert_allclose(
            ret.iloc[1:].to_numpy(),
            np.full(n - 1, np.log(1.01)),
            rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)

    def test_log_ret_warmup_lengths(self):
        bars = _make_synthetic_bars(n=50)
        out = price_return.compute(bars)
        # log_ret_5 has 5 NaN at start; log_ret_20 has 20.
        self.assertEqual(int(out["price_return.log_ret_5"].isna().sum()), 5)
        self.assertEqual(int(out["price_return.log_ret_20"].isna().sum()), 20)

    def test_gap_pct_known(self):
        # Two-bar fixture: open[1] = close[0] * 1.02 → gap_pct[1] = 0.02
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=3,
                                      freq="1D", tz="UTC"),
            "open":   [100.0, 102.0, 105.0],
            "high":   [101.0, 103.0, 106.0],
            "low":    [99.0,  101.0, 104.0],
            "close":  [100.0, 102.0, 105.0],
            "volume": [1_000_000.0] * 3,
            "quality_flags": 0,
        })
        out = price_return.compute(bars)
        gap = out["price_return.gap_pct"]
        self.assertTrue(pd.isna(gap.iloc[0]))   # warmup
        self.assertAlmostEqual(gap.iloc[1], 0.02, places=12)
        # Bar 2: open=105 vs close[1]=102 → gap = 3/102
        self.assertAlmostEqual(gap.iloc[2], 3.0 / 102.0, places=12)

    def test_body_and_wick_known(self):
        # Single bar: open=100, close=110, high=115, low=98
        # body  = (110 - 100) / 100 = 0.10
        # hl    = (115 - 98)  / 100 = 0.17
        # upper = (115 - 110) / 100 = 0.05  (top of body = max(o,c) = 110)
        # lower = (100 - 98)  / 100 = 0.02  (bottom of body = min(o,c) = 100)
        bars = pd.DataFrame({
            "ts_utc": [pd.Timestamp("2024-01-02", tz="UTC")],
            "open":   [100.0],
            "high":   [115.0],
            "low":    [98.0],
            "close":  [110.0],
            "volume": [1_000_000.0],
            "quality_flags": 0,
        })
        out = price_return.compute(bars)
        self.assertAlmostEqual(out["price_return.body_pct"].iloc[0],
                                0.10, places=12)
        self.assertAlmostEqual(out["price_return.hl_range_pct"].iloc[0],
                                0.17, places=12)
        self.assertAlmostEqual(out["price_return.upper_wick_pct"].iloc[0],
                                0.05, places=12)
        self.assertAlmostEqual(out["price_return.lower_wick_pct"].iloc[0],
                                0.02, places=12)

    def test_dist_from_rolling_high_known(self):
        # Monotone-up series → rolling max == current close → distance == 0
        n = 30
        close = 100.0 + np.arange(n)   # 100, 101, ..., 129
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open":   close,
            "high":   close,
            "low":    close,
            "close":  close,
            "volume": np.full(n, 1e6),
            "quality_flags": 0,
        })
        out = price_return.compute(bars)
        dist = out["price_return.dist_from_rolling_high_20"]
        # First 19 bars are warmup; from bar 20 onward, rolling-max
        # == current bar → distance == 0
        self.assertTrue(pd.isna(dist.iloc[18]))
        for i in range(19, n):
            self.assertAlmostEqual(dist.iloc[i], 0.0, places=12,
                msg=f"dist_from_rolling_high_20 at bar {i}")

    def test_determinism(self):
        bars = _make_synthetic_bars(n=100, seed=123)
        out1 = price_return.compute(bars)
        out2 = price_return.compute(bars)
        pd.testing.assert_frame_equal(out1, out2, check_exact=True)

    def test_specs_all_safe_leak_class(self):
        for s in price_return.SPECS:
            self.assertEqual(s.leak_class, "safe",
                f"{s.feature_id} must be leak_class='safe'")


# ─────────────────────────────────────────────────────────────────────
# G2_Trend — 8 features
# ─────────────────────────────────────────────────────────────────────

class G2_Trend(unittest.TestCase):

    def test_constant_series_sma_distance_zero(self):
        n = 250
        close = np.full(n, 100.0)
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open":   close, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        out = trend.compute(bars)
        # After warmup, every distance should be exactly 0 (constant series)
        post_warmup_50  = out["trend.sma_distance_50"].iloc[50:]
        post_warmup_200 = out["trend.sma_distance_200"].iloc[200:]
        np.testing.assert_allclose(post_warmup_50.to_numpy(),
                                     0.0, atol=_PARITY_ATOL)
        np.testing.assert_allclose(post_warmup_200.to_numpy(),
                                     0.0, atol=_PARITY_ATOL)

    def test_uptrend_ema20_above_ema50(self):
        n = 100
        close = 100.0 + np.arange(n) * 1.0   # strict uptrend
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open": close, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        out = trend.compute(bars)
        # After EMA50 warmup, ema20_gt_ema50 should be 1.
        post_warmup = out["trend.ema20_gt_ema50"].iloc[50:]
        self.assertTrue((post_warmup == 1).all(),
            "ema20_gt_ema50 must be 1 in a sustained uptrend")

    def test_uptrend_ema_slopes_positive(self):
        n = 100
        close = 100.0 + np.arange(n) * 1.0
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open": close, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        out = trend.compute(bars)
        ema20_slope = out["trend.ema20_slope"].iloc[55:].dropna()
        self.assertTrue((ema20_slope > 0).all(),
            "ema20_slope must be positive in sustained uptrend")

    def test_sma_parity_vs_m17b(self):
        """ema_distance / sma_distance share their internal SMA/EMA
        with bot.backtesting.indicators. Spot-check parity."""
        from bot.backtesting.indicators import sma as live_sma
        from bot.backtesting.indicators import ema as live_ema
        bars = _make_synthetic_bars(n=300)
        c = bars["close"].astype(float)
        # Compute via the same paths used inside trend.compute
        ours_sma50 = c.rolling(window=50, min_periods=50).mean()
        theirs_sma50 = live_sma(c, 50)
        np.testing.assert_allclose(
            ours_sma50.dropna().to_numpy(),
            theirs_sma50.dropna().to_numpy(),
            rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)
        ours_ema20 = c.ewm(span=20, adjust=False, min_periods=20).mean()
        theirs_ema20 = live_ema(c, 20)
        np.testing.assert_allclose(
            ours_ema20.dropna().to_numpy(),
            theirs_ema20.dropna().to_numpy(),
            rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)

    def test_determinism(self):
        bars = _make_synthetic_bars(n=300, seed=7)
        a = trend.compute(bars)
        b = trend.compute(bars)
        pd.testing.assert_frame_equal(a, b, check_exact=True)


# ─────────────────────────────────────────────────────────────────────
# G2_Momentum — RSI live-parity, MACD live-parity, ROC, accel
# ─────────────────────────────────────────────────────────────────────

class G2_Momentum(unittest.TestCase):

    def test_rsi_warmup_14_bars(self):
        bars = _make_synthetic_bars(n=50)
        out = momentum.compute(bars)
        # First 14 bars are NaN (1 bar for diff + 13 more for the SMA(14))
        rsi = out["momentum.rsi_14_sma_gain_loss"]
        self.assertEqual(int(rsi.iloc[:14].isna().sum()), 14)
        self.assertFalse(rsi.iloc[14:].isna().any(),
            "RSI must be defined for all bars after warmup on real data")

    def test_rsi_monotone_up_approaches_high(self):
        # Strict monotone-up series → RSI should be very high (≈100)
        n = 50
        close = 100.0 + np.arange(n) * 0.5
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open": close, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        out = momentum.compute(bars)
        rsi_tail = out["momentum.rsi_14_sma_gain_loss"].iloc[20:]
        # All gains, no losses → rs = gain/eps → very high RSI
        # With the +1e-9 epsilon, RSI saturates to ≈100 to floating-point
        self.assertTrue((rsi_tail > 99.0).all(),
            f"RSI on monotone-up should be near 100; got {rsi_tail.values}")

    def test_rsi_parity_vs_m17b(self):
        """SR-4 — bit-identical at rtol=1e-9, atol=1e-8 vs
        bot.backtesting.indicators.rsi(mode='sma_gain_loss')."""
        from bot.backtesting.indicators import rsi as live_rsi
        bars = _make_synthetic_bars(n=400, seed=11)
        out = momentum.compute(bars)
        ours = out["momentum.rsi_14_sma_gain_loss"]
        theirs = live_rsi(bars["close"].astype(float), 14,
                          mode="sma_gain_loss")
        # Align both series, drop warmup NaN, compare element-wise.
        # Both should produce the same NaN mask.
        mask = ours.notna() & theirs.notna()
        self.assertEqual(int(mask.sum()), int(ours.notna().sum()),
            "NaN masks differ between M18 RSI and live RSI")
        np.testing.assert_allclose(
            ours[mask].to_numpy(), theirs[mask].to_numpy(),
            rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)

    def test_macd_parity_vs_m17b(self):
        from bot.backtesting.indicators import macd as live_macd
        bars = _make_synthetic_bars(n=400, seed=23)
        out = momentum.compute(bars)
        theirs = live_macd(bars["close"].astype(float))
        for theirs_col, ours_col in [
                ("macd",   "momentum.macd_line"),
                ("signal", "momentum.macd_signal"),
                ("hist",   "momentum.macd_hist")]:
            o = out[ours_col]
            t = theirs[theirs_col]
            mask = o.notna() & t.notna()
            np.testing.assert_allclose(
                o[mask].to_numpy(), t[mask].to_numpy(),
                rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL,
                err_msg=f"parity fail for {ours_col}")

    def test_roc_10_known_constant_return(self):
        # If each bar is +1% over the previous, roc_10 = (1.01^10) - 1
        n = 30
        close = 100.0 * np.power(1.01, np.arange(n))
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open": close, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        out = momentum.compute(bars)
        roc = out["momentum.roc_10"]
        expected = (1.01 ** 10) - 1.0
        np.testing.assert_allclose(
            roc.iloc[10:].to_numpy(),
            np.full(n - 10, expected),
            rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)

    def test_momentum_acceleration_zero_on_geometric(self):
        # Constant log-return series → log_ret_5 is constant → diff = 0
        n = 30
        close = 100.0 * np.power(1.01, np.arange(n))
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open": close, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        out = momentum.compute(bars)
        acc = out["momentum.momentum_acceleration"]
        # First 10 bars are warmup (5 for log_ret_5 + 5 for diff(5))
        np.testing.assert_allclose(
            acc.iloc[10:].to_numpy(),
            0.0, atol=_PARITY_ATOL)

    def test_determinism(self):
        bars = _make_synthetic_bars(n=300, seed=99)
        a = momentum.compute(bars)
        b = momentum.compute(bars)
        pd.testing.assert_frame_equal(a, b, check_exact=True)


# ─────────────────────────────────────────────────────────────────────
# G2_VolRegime — ATR live-parity, bb_pos live-parity, regime flag
# ─────────────────────────────────────────────────────────────────────

class G2_VolRegime(unittest.TestCase):

    def test_atr_warmup(self):
        bars = _make_synthetic_bars(n=50)
        out = vol_regime.compute(bars)
        atr = out["vol_regime.atr_14_sma_true_range"]
        # ATR(14) sma mode: 1 diff bar + 14 sma rows → 14 NaN at start
        self.assertEqual(int(atr.iloc[:14].isna().sum()), 14)
        self.assertFalse(atr.iloc[14:].isna().any())

    def test_atr_parity_vs_m17b(self):
        from bot.backtesting.indicators import atr as live_atr
        bars = _make_synthetic_bars(n=400, seed=31)
        out = vol_regime.compute(bars)
        ours = out["vol_regime.atr_14_sma_true_range"]
        theirs = live_atr(
            bars["high"].astype(float),
            bars["low"].astype(float),
            bars["close"].astype(float),
            14, mode="sma_true_range")
        mask = ours.notna() & theirs.notna()
        np.testing.assert_allclose(
            ours[mask].to_numpy(), theirs[mask].to_numpy(),
            rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)

    def test_bb_pos_parity_vs_m17b(self):
        from bot.backtesting.indicators import bb_pos as live_bb_pos
        bars = _make_synthetic_bars(n=400, seed=47)
        out = vol_regime.compute(bars)
        ours = out["vol_regime.bb_pos"]
        theirs = live_bb_pos(bars["close"].astype(float), 20, 2.0)
        # Compare element-by-element handling both NaN-warmup AND the
        # 0.5 fallback (where rng <= 0). Both implementations should
        # produce identical results at every position.
        ours_a   = ours.to_numpy()
        theirs_a = theirs.to_numpy()
        # NaN masks must match exactly
        np.testing.assert_array_equal(
            np.isnan(ours_a), np.isnan(theirs_a),
            err_msg="bb_pos NaN masks differ between M18 and M17.B")
        # Non-NaN values must match to floating-point
        m = ~np.isnan(ours_a)
        np.testing.assert_allclose(
            ours_a[m], theirs_a[m],
            rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)

    def test_realized_vol_zero_on_constant_close(self):
        n = 30
        close = np.full(n, 100.0)
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open": close, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        out = vol_regime.compute(bars)
        rv = out["vol_regime.realized_vol_20"]
        # log_ret_1 is 0 for constant series → std is 0 (or NaN if
        # pandas returns NaN for zero-variance window). Accept both.
        post = rv.iloc[21:]
        for val in post:
            self.assertTrue(val == 0.0 or pd.isna(val),
                f"realized_vol on constant series should be 0 or NaN, got {val}")

    def test_vol_regime_flag_bounds(self):
        bars = _make_synthetic_bars(n=200, seed=55)
        out = vol_regime.compute(bars)
        flag = out["vol_regime.vol_regime_flag"]
        self.assertEqual(flag.dtype, np.int8)
        self.assertTrue(((flag >= 0) & (flag <= 3)).all())

    def test_determinism(self):
        bars = _make_synthetic_bars(n=300, seed=77)
        a = vol_regime.compute(bars)
        b = vol_regime.compute(bars)
        pd.testing.assert_frame_equal(a, b, check_exact=True)


# ─────────────────────────────────────────────────────────────────────
# G2_VolumeLiquidity — volume_ratio parity, vol_zscore, liquidity bucket
# ─────────────────────────────────────────────────────────────────────

class G2_VolumeLiquidity(unittest.TestCase):

    def test_vol_ratio_one_on_flat_volume(self):
        n = 30
        bars = _make_synthetic_bars(n=n)
        bars["volume"] = 1_000_000.0
        out = volume_liquidity.compute(bars)
        ratio = out["volume_liquidity.vol_ratio_20"]
        np.testing.assert_allclose(
            ratio.iloc[20:].to_numpy(),
            1.0, rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)

    def test_vol_ratio_parity_vs_m17b(self):
        """M18 returns NaN on zero-volume SMA; M17.B uses +1e-9.
        Synthetic data has only positive volumes, so the two formulas
        agree to floating-point for ALL bars after warmup."""
        from bot.backtesting.indicators import volume_ratio as live_vr
        bars = _make_synthetic_bars(n=400, seed=63)
        out = volume_liquidity.compute(bars)
        ours = out["volume_liquidity.vol_ratio_20"]
        theirs = live_vr(bars["volume"].astype(float), 20)
        mask = ours.notna() & theirs.notna()
        np.testing.assert_allclose(
            ours[mask].to_numpy(), theirs[mask].to_numpy(),
            rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)

    def test_dollar_vol_20_known(self):
        # Build a fixture where close * volume = constant; dollar_vol_20
        # should equal that constant after warmup.
        n = 30
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open":   np.full(n, 100.0),
            "high":   np.full(n, 100.0),
            "low":    np.full(n, 100.0),
            "close":  np.full(n, 100.0),
            "volume": np.full(n, 1_000_000.0),
            "quality_flags": 0,
        })
        out = volume_liquidity.compute(bars)
        dv = out["volume_liquidity.dollar_vol_20"]
        np.testing.assert_allclose(
            dv.iloc[20:].to_numpy(), 100.0 * 1_000_000.0,
            rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)

    def test_liquidity_bucket_ordinal_bounds(self):
        bars = _make_synthetic_bars(n=400, seed=88)
        out = volume_liquidity.compute(bars)
        b = out["volume_liquidity.liquidity_bucket"]
        self.assertEqual(b.dtype, np.int8)
        self.assertTrue(((b >= 0) & (b <= 4)).all())

    def test_determinism(self):
        bars = _make_synthetic_bars(n=300, seed=66)
        a = volume_liquidity.compute(bars)
        b = volume_liquidity.compute(bars)
        pd.testing.assert_frame_equal(a, b, check_exact=True)


# ─────────────────────────────────────────────────────────────────────
# G2 — Future-bar scramble (cross-group leak-safety)
# ─────────────────────────────────────────────────────────────────────

class G2_FutureBarScramble(unittest.TestCase):
    """For every leak_class='safe' feature in every M18.A.2 group,
    scrambling the bars AFTER an anchor T must NOT change the feature
    value at any bar <= T. This is the canonical look-ahead test.

    Mirrors the M17.B 'future-bar scramble' approach: bars beyond
    anchor get replaced with completely different values, then the
    safe features for positions <= anchor must remain bit-identical."""

    def _build_scrambled_pair(self, n=300, anchor_idx=200, seed=42,
                               scramble_seed=99999):
        original = _make_synthetic_bars(n=n, seed=seed).copy()
        scrambled = original.copy()
        rng = np.random.default_rng(scramble_seed)
        future_n = n - anchor_idx - 1
        # Replace future bars with very different values
        new_close = rng.uniform(1.0, 1000.0, size=future_n)
        new_open  = rng.uniform(1.0, 1000.0, size=future_n)
        new_high  = np.maximum(new_open, new_close) * (
            1.0 + np.abs(rng.normal(0, 0.05, future_n)))
        new_low   = np.minimum(new_open, new_close) * (
            1.0 - np.abs(rng.normal(0, 0.05, future_n)))
        new_vol   = rng.integers(1, 1_000_000_000, future_n).astype(float)
        sl = slice(anchor_idx + 1, n)
        scrambled.loc[sl, "open"]   = new_open
        scrambled.loc[sl, "high"]   = new_high
        scrambled.loc[sl, "low"]    = new_low
        scrambled.loc[sl, "close"]  = new_close
        scrambled.loc[sl, "volume"] = new_vol
        return original, scrambled, anchor_idx

    def test_all_safe_features_unchanged_at_or_before_anchor(self):
        orig, scram, anchor = self._build_scrambled_pair()
        for mod in (price_return, trend, momentum,
                     vol_regime, volume_liquidity):
            with self.subTest(group=mod.GROUP_NAME):
                # Verify all SPECS are leak_class='safe' for M18.A.2.
                for s in mod.SPECS:
                    self.assertEqual(s.leak_class, "safe",
                        f"{s.feature_id} is not safe — should not be "
                        f"in M18.A.2")
                a = mod.compute(orig).iloc[:anchor + 1]
                b = mod.compute(scram).iloc[:anchor + 1]
                # Bit-identical for the at-or-before-anchor window.
                # NaN positions must also match.
                for col in a.columns:
                    av = a[col].to_numpy()
                    bv = b[col].to_numpy()
                    np.testing.assert_array_equal(
                        np.isnan(av), np.isnan(bv),
                        err_msg=f"{mod.GROUP_NAME}/{col}: NaN mask "
                                  f"differs across scramble (leak!)")
                    m = ~np.isnan(av)
                    np.testing.assert_array_equal(
                        av[m], bv[m],
                        err_msg=f"{mod.GROUP_NAME}/{col}: values "
                                  f"differ across scramble (leak!)")


# ═════════════════════════════════════════════════════════════════════
# G2 — M18.A.3 feature groups: multi-TF, benchmark, metadata, flywheel
# ═════════════════════════════════════════════════════════════════════


def _make_multi_tf_bars(seed: int = 1, n_15m: int = 400):
    """Generate aligned multi-TF synthetic bars at 15m/1H/4H/1D.

    Each TF gets its own RNG seed so the resulting series differ;
    timestamps start from the same anchor and use the requested
    cadence so that snapshot_at() will find at-or-before bars at
    every 15m anchor.
    """
    def _one(n, freq, seed_, start="2024-01-02"):
        rng = np.random.default_rng(seed_)
        ts = pd.date_range(start, periods=n, freq=freq, tz="UTC")
        close = 100.0 * np.exp(np.cumsum(
            rng.normal(0.0001, 0.005, n)))
        open_ = np.concatenate([[100.0], close[:-1]])
        spread = np.abs(rng.normal(0, 0.005, n)) * close + 0.01
        high = np.maximum(open_, close) + spread / 2.0
        low  = np.minimum(open_, close) - spread / 2.0
        vol = rng.integers(1_000_000, 10_000_000, n).astype(float)
        return pd.DataFrame({"ts_utc": ts, "open": open_, "high": high,
                              "low": low, "close": close, "volume": vol,
                              "quality_flags": 0})

    # 15m anchor; coarser TFs at proportional sample counts.
    b15 = _one(n_15m,            "15min", seed * 11)
    b1h = _one(max(80, n_15m//4), "1h",    seed * 13)
    b4h = _one(max(30, n_15m//16), "4h",   seed * 17)
    b1d = _one(max(20, n_15m//96), "1D",   seed * 19)
    return {"15m": b15, "1H": b1h, "4H": b4h, "1D": b1d}


# ─────────────────────────────────────────────────────────────────────
# G2_SymbolMeta
# ─────────────────────────────────────────────────────────────────────

class G2_SymbolMeta(unittest.TestCase):

    EXAMPLE = "configs/ml/symbol_metadata.example.json"

    def test_load_example_file_succeeds(self):
        data = symbol_meta.load_metadata(self.EXAMPLE)
        self.assertEqual(data["schema_version"], 1)
        self.assertIn("symbols", data)
        self.assertIn("encodings", data)
        # Must contain at least one example symbol.
        self.assertIn("AAPL", data["symbols"])

    def test_known_symbol_lookup(self):
        bars = _make_synthetic_bars(n=10)
        out = symbol_meta.compute(bars, symbol="AAPL",
                                    metadata_path=self.EXAMPLE)
        # AAPL: sector=technology(0), market_cap=mega(4),
        #       asset_class=equity(0), etf=false, ipo=1980
        self.assertEqual(int(out["symbol_meta.sector_code"].iloc[0]), 0)
        self.assertEqual(int(out["symbol_meta.market_cap_code"].iloc[0]),
                          4)
        self.assertEqual(int(out["symbol_meta.asset_class_code"].iloc[0]),
                          0)
        self.assertEqual(int(out["symbol_meta.is_etf"].iloc[0]), 0)
        self.assertEqual(int(out["symbol_meta.ipo_year"].iloc[0]), 1980)

    def test_etf_symbol_marked_correctly(self):
        bars = _make_synthetic_bars(n=10)
        out = symbol_meta.compute(bars, symbol="SPY",
                                    metadata_path=self.EXAMPLE)
        self.assertEqual(int(out["symbol_meta.is_etf"].iloc[0]), 1)
        self.assertEqual(int(out["symbol_meta.market_cap_code"].iloc[0]),
                          5)

    def test_unknown_symbol_falls_back_to_unknown_codes(self):
        bars = _make_synthetic_bars(n=10)
        out = symbol_meta.compute(bars, symbol="NEVER_SEEN_BEFORE_XYZ",
                                    metadata_path=self.EXAMPLE)
        # unknown sector → 99, unknown cap → 99, unknown asset → 99,
        # unknown ipo → 0, unknown etf → -1
        self.assertEqual(int(out["symbol_meta.sector_code"].iloc[0]),
                          99)
        self.assertEqual(int(out["symbol_meta.market_cap_code"].iloc[0]),
                          99)
        self.assertEqual(int(out["symbol_meta.asset_class_code"].iloc[0]),
                          99)
        self.assertEqual(int(out["symbol_meta.ipo_year"].iloc[0]), 0)
        self.assertEqual(int(out["symbol_meta.is_etf"].iloc[0]), -1)

    def test_constant_across_rows(self):
        """Every row should have the same value (static metadata)."""
        bars = _make_synthetic_bars(n=50)
        out = symbol_meta.compute(bars, symbol="AAPL",
                                    metadata_path=self.EXAMPLE)
        for col in out.columns:
            self.assertEqual(out[col].nunique(), 1,
                f"{col} is not constant across rows")

    def test_specs_all_safe_leak_class(self):
        for s in symbol_meta.SPECS:
            self.assertEqual(s.leak_class, "safe",
                f"{s.feature_id} must be leak_class='safe'")

    def test_schema_validation_rejects_bad_file(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.json"
            bad.write_text('{"schema_version": 99, "symbols": {}, '
                           '"encodings": {}}')
            with self.assertRaises(ValueError):
                symbol_meta.load_metadata(bad)


# ─────────────────────────────────────────────────────────────────────
# G2_MTFConfluence
# ─────────────────────────────────────────────────────────────────────

class G2_MTFConfluence(unittest.TestCase):

    def test_basic_compute_shape(self):
        per_tf = _make_multi_tf_bars(seed=1)
        out = mtf_confluence.compute(per_tf["15m"], per_tf_bars=per_tf)
        self.assertEqual(len(out), len(per_tf["15m"]))
        self.assertEqual(
            set(out.columns),
            {"mtf_confluence.available_tf_count",
              "mtf_confluence.tf_15m_present",
              "mtf_confluence.tf_1h_present",
              "mtf_confluence.tf_4h_present",
              "mtf_confluence.tf_1d_present"})

    def test_full_availability_after_warmup(self):
        per_tf = _make_multi_tf_bars(seed=1, n_15m=600)
        out = mtf_confluence.compute(per_tf["15m"], per_tf_bars=per_tf)
        # After enough 15m bars to also have 1D/4H/1H snapshots, every
        # TF must be present at the LAST anchor.
        last = out.iloc[-1]
        self.assertEqual(int(last["mtf_confluence.available_tf_count"]),
                          4)
        for col in ("tf_15m_present", "tf_1h_present",
                      "tf_4h_present", "tf_1d_present"):
            self.assertEqual(int(last[f"mtf_confluence.{col}"]), 1)

    def test_only_anchor_tf_at_first_anchor(self):
        # At the very first 15m anchor, the coarser TFs (1H/4H/1D) may
        # NOT yet have a bar at-or-before (since their bars start at
        # the same UTC date but the first 1H bar's ts is later than
        # the first 15m bar's ts). Verify available_tf_count is
        # consistent with the snapshot semantics.
        per_tf = _make_multi_tf_bars(seed=1)
        out = mtf_confluence.compute(per_tf["15m"], per_tf_bars=per_tf)
        # The 15m TF MUST be present at every anchor (it's the anchor).
        self.assertTrue((out["mtf_confluence.tf_15m_present"] == 1).all())
        # available_tf_count >= 1 always (15m present)
        self.assertTrue(
            (out["mtf_confluence.available_tf_count"] >= 1).all())

    def test_leak_safety_future_15m_scramble(self):
        per_tf = _make_multi_tf_bars(seed=2)
        anchor_idx = len(per_tf["15m"]) - 50
        scram_15m = per_tf["15m"].copy()
        rng = np.random.default_rng(987)
        future_n = len(scram_15m) - anchor_idx - 1
        scram_15m.loc[anchor_idx + 1:, "close"] = rng.uniform(
            1, 1000, future_n)
        scram_15m.loc[anchor_idx + 1:, "high"] = rng.uniform(
            1000, 2000, future_n)
        scram_15m.loc[anchor_idx + 1:, "low"] = rng.uniform(
            0.1, 1, future_n)
        scram_15m.loc[anchor_idx + 1:, "volume"] = rng.uniform(
            1, 1e9, future_n)
        scram_per_tf = dict(per_tf)
        scram_per_tf["15m"] = scram_15m

        a = mtf_confluence.compute(per_tf["15m"], per_tf_bars=per_tf)
        b = mtf_confluence.compute(scram_15m,
                                     per_tf_bars=scram_per_tf)
        # Features at or before anchor_idx must be identical.
        for col in a.columns:
            np.testing.assert_array_equal(
                a.iloc[:anchor_idx + 1][col].to_numpy(),
                b.iloc[:anchor_idx + 1][col].to_numpy(),
                err_msg=f"mtf_confluence/{col}: scramble leaked future")


# ─────────────────────────────────────────────────────────────────────
# G2_ScannerReplica
# ─────────────────────────────────────────────────────────────────────

class G2_ScannerReplica(unittest.TestCase):

    def test_basic_compute_shape(self):
        per_tf = _make_multi_tf_bars(seed=3)
        out = scanner_replica.compute(per_tf["15m"],
                                        per_tf_bars=per_tf)
        self.assertEqual(len(out), len(per_tf["15m"]))
        self.assertEqual(out.shape[1], len(scanner_replica.SPECS))

    def test_signal_fires_parity_vs_M17B_strategy(self):
        """The binary signal column we produce must equal the M17.B
        strategy's signal output (== SIG_ENTRY -> 1)."""
        per_tf = _make_multi_tf_bars(seed=4, n_15m=600)
        out = scanner_replica.compute(per_tf["15m"],
                                        per_tf_bars=per_tf)
        # Pull M17.B's canonical signal df via the parity helper
        sig_df = scanner_replica.parity_check_with_strategy(
            per_tf["15m"], per_tf_bars=per_tf)
        # SIG_ENTRY constant in M17.B; per the strategy it's used as
        # signal == SIG_ENTRY for entries. We just compare zero/non-zero.
        m18_fires = out["scanner_replica.signal_fires"].to_numpy() == 1
        m17_fires = sig_df["signal"].to_numpy() != 0
        np.testing.assert_array_equal(
            m18_fires, m17_fires,
            err_msg="M18 scanner_replica.signal_fires must match "
                    "M17.B ScannerReplicaStrategy.generate signal column")

    def test_long_count_within_bounds(self):
        per_tf = _make_multi_tf_bars(seed=5)
        out = scanner_replica.compute(per_tf["15m"],
                                        per_tf_bars=per_tf)
        lc = out["scanner_replica.long_count"]
        avail = out["scanner_replica.available_tf_count"]
        # long_count must be <= available_tf_count at every anchor
        self.assertTrue((lc <= avail).all(),
            "long_count cannot exceed available_tf_count")
        # Both within [0, 4]
        self.assertTrue(((lc >= 0) & (lc <= 4)).all())
        self.assertTrue(((avail >= 0) & (avail <= 4)).all())

    def test_signal_implies_long_count_meets_min_valid(self):
        """When signal_fires=1, long_count >= confluence_min_valid."""
        per_tf = _make_multi_tf_bars(seed=6, n_15m=600)
        out = scanner_replica.compute(per_tf["15m"],
                                        per_tf_bars=per_tf)
        fires = out["scanner_replica.signal_fires"] == 1
        lc = out.loc[fires, "scanner_replica.long_count"]
        mv = out.loc[fires, "scanner_replica.confluence_min_valid"]
        self.assertTrue((lc >= mv).all(),
            "signal fired but long_count < confluence_min_valid")

    def test_leak_safety_future_15m_scramble(self):
        per_tf = _make_multi_tf_bars(seed=7, n_15m=500)
        anchor_idx = len(per_tf["15m"]) - 50
        scram_15m = per_tf["15m"].copy()
        rng = np.random.default_rng(54321)
        future_n = len(scram_15m) - anchor_idx - 1
        for col, lo, hi in (("close", 1, 1000), ("high", 1000, 2000),
                              ("low", 0.1, 1), ("volume", 1, 1e9)):
            scram_15m.loc[anchor_idx + 1:, col] = rng.uniform(
                lo, hi, future_n)
        scram_per_tf = dict(per_tf)
        scram_per_tf["15m"] = scram_15m

        a = scanner_replica.compute(per_tf["15m"], per_tf_bars=per_tf)
        b = scanner_replica.compute(scram_15m,
                                      per_tf_bars=scram_per_tf)
        for col in a.columns:
            np.testing.assert_array_equal(
                a.iloc[:anchor_idx + 1][col].to_numpy(),
                b.iloc[:anchor_idx + 1][col].to_numpy(),
                err_msg=f"scanner_replica/{col}: scramble leaked future")

    def test_specs_all_safe_leak_class(self):
        for s in scanner_replica.SPECS:
            self.assertEqual(s.leak_class, "safe",
                f"{s.feature_id} must be leak_class='safe'")


# ─────────────────────────────────────────────────────────────────────
# G2_MarketContext
# ─────────────────────────────────────────────────────────────────────

class G2_MarketContext(unittest.TestCase):

    @staticmethod
    def _make_spy_qqq(n=300, seed=10, start="2023-01-01"):
        rng = np.random.default_rng(seed)
        ts = pd.date_range(start, periods=n, freq="1D", tz="UTC")
        close = 100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, n)))
        return pd.DataFrame({
            "ts_utc": ts, "open": close, "high": close * 1.005,
            "low": close * 0.995, "close": close,
            "volume": np.full(n, 1e8), "quality_flags": 0,
        })

    def test_benchmark_available_when_both_provided(self):
        bars = _make_synthetic_bars(n=50)
        spy = self._make_spy_qqq(n=400, seed=10)
        qqq = self._make_spy_qqq(n=400, seed=11)
        out = market_context.compute(
            bars, benchmark_bars={"SPY": spy, "QQQ": qqq})
        # benchmark_data_available should be 1 across the bars window
        # (both SPY and QQQ have rows at-or-before every bar).
        self.assertTrue(
            (out["market_context.benchmark_data_available"] == 1).all())

    def test_benchmark_unavailable_when_missing(self):
        bars = _make_synthetic_bars(n=50)
        out = market_context.compute(bars, benchmark_bars={})
        self.assertTrue(
            (out["market_context.benchmark_data_available"] == 0).all())
        # SPY/QQQ-derived float features must be NaN
        self.assertTrue(
            out["market_context.spy_drawdown_pct_60d"].isna().all())
        self.assertTrue(
            out["market_context.qqq_log_ret_1d_at_anchor"].isna().all())

    def test_spy_above_ema200_uptrend(self):
        # Sustained uptrend SPY → close > EMA200 once warmup completes
        bars = _make_synthetic_bars(n=50)
        n = 400
        close = 100.0 + np.arange(n) * 0.5
        spy = pd.DataFrame({
            "ts_utc": pd.date_range("2022-01-01", periods=n,
                                      freq="1D", tz="UTC"),
            "open": close, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e8), "quality_flags": 0,
        })
        out = market_context.compute(
            bars, benchmark_bars={"SPY": spy})
        # spy_above_ema200_1d must be 1 at every anchor (uptrend +
        # benchmark warmup complete before bars start)
        self.assertTrue(
            (out["market_context.spy_above_ema200_1d"] == 1).all())

    def test_leak_safety_future_spy_scramble(self):
        bars = _make_synthetic_bars(n=50)
        spy = self._make_spy_qqq(n=400, seed=10)
        qqq = self._make_spy_qqq(n=400, seed=11)

        # Scramble SPY bars beyond the anchor of last `bars` row.
        last_anchor = bars["ts_utc"].iloc[-25]
        rng = np.random.default_rng(444)
        scram_spy = spy.copy()
        mask = scram_spy["ts_utc"] > last_anchor
        nscram = int(mask.sum())
        scram_spy.loc[mask, "close"] = rng.uniform(1, 1000, nscram)
        scram_spy.loc[mask, "open"]  = rng.uniform(1, 1000, nscram)
        scram_spy.loc[mask, "high"]  = rng.uniform(1000, 2000, nscram)
        scram_spy.loc[mask, "low"]   = rng.uniform(0.1, 1, nscram)

        a = market_context.compute(
            bars, benchmark_bars={"SPY": spy, "QQQ": qqq})
        b = market_context.compute(
            bars, benchmark_bars={"SPY": scram_spy, "QQQ": qqq})
        # The first 25 bars (whose anchors are all before last_anchor)
        # must not be affected by the SPY scramble.
        n_keep = 25   # bars[:n_keep] all have ts_utc <= last_anchor
        for col in a.columns:
            av = a[col].iloc[:n_keep].to_numpy()
            bv = b[col].iloc[:n_keep].to_numpy()
            np.testing.assert_array_equal(
                np.isnan(av), np.isnan(bv),
                err_msg=f"market_context/{col}: NaN mask diff")
            m = ~np.isnan(av)
            np.testing.assert_array_equal(av[m], bv[m],
                err_msg=f"market_context/{col}: SPY scramble leaked")


# ─────────────────────────────────────────────────────────────────────
# G2_SignalHistory
# ─────────────────────────────────────────────────────────────────────

class G2_SignalHistory(unittest.TestCase):

    @staticmethod
    def _build_signal_outcomes_db(path, rows):
        """Create a real sqlite3 DB with the (subset of) flywheel
        schema this reader needs, then insert the provided rows."""
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("""
                CREATE TABLE signal_outcomes (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id        INTEGER NOT NULL DEFAULT 0,
                    intent_id        INTEGER DEFAULT NULL,
                    symbol           TEXT    NOT NULL DEFAULT '',
                    direction        TEXT    NOT NULL DEFAULT '',
                    entry_price      REAL,
                    exit_price       REAL,
                    return_pct       REAL,
                    outcome          TEXT    DEFAULT NULL,
                    bars_held        INTEGER DEFAULT NULL,
                    resolved_at      TEXT    DEFAULT NULL,
                    resolution_method TEXT   DEFAULT NULL
                )
            """)
            for r in rows:
                conn.execute(
                    "INSERT INTO signal_outcomes "
                    "(symbol, direction, return_pct, outcome, "
                    " resolved_at) VALUES (?, ?, ?, ?, ?)",
                    (r["symbol"], r.get("direction", "long"),
                      r.get("return_pct"), r["outcome"],
                      r["resolved_at"]))
            conn.commit()

    def test_no_db_returns_all_nan(self):
        bars = _make_synthetic_bars(n=10)
        out = signal_history.compute(bars, symbol="AAPL")
        self.assertTrue(
            (out["signal_history.signals_count_30d"] == 0).all())
        self.assertTrue(
            out["signal_history.win_rate_30d"].isna().all())
        self.assertTrue(
            out["signal_history.avg_return_pct_90d"].isna().all())

    def test_nonexistent_db_returns_all_nan(self):
        bars = _make_synthetic_bars(n=5)
        out = signal_history.compute(bars, symbol="AAPL",
            db_path="/tmp/this_path_does_not_exist_xyz.db")
        self.assertTrue(
            (out["signal_history.signals_count_30d"] == 0).all())

    def test_specs_use_requires_past_flywheel_leak_class(self):
        for s in signal_history.SPECS:
            self.assertEqual(s.leak_class,
                              "requires_past_flywheel_only",
                f"{s.feature_id} should be "
                f"leak_class='requires_past_flywheel_only'")

    def test_point_in_time_correctness_real_db(self):
        """Build a real DB with outcomes at known timestamps; compute
        signal_history at an anchor; verify only outcomes resolved
        BEFORE the anchor are counted."""
        from contextlib import closing
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "test.db"
            anchor = pd.Timestamp("2024-06-01", tz="UTC")
            rows = [
                # Resolved BEFORE anchor — within 30d → count for 30d/90d
                {"symbol": "AAPL", "outcome": "WIN",
                  "return_pct": 0.05,
                  "resolved_at": "2024-05-20T10:00:00+00:00"},
                {"symbol": "AAPL", "outcome": "LOSS",
                  "return_pct": -0.02,
                  "resolved_at": "2024-05-25T10:00:00+00:00"},
                # Resolved BEFORE anchor — in 30-90d window (not 30d)
                {"symbol": "AAPL", "outcome": "WIN",
                  "return_pct": 0.08,
                  "resolved_at": "2024-04-10T10:00:00+00:00"},
                # Resolved AT anchor — strict <, so excluded
                {"symbol": "AAPL", "outcome": "WIN",
                  "return_pct": 0.10,
                  "resolved_at": "2024-06-01T00:00:00+00:00"},
                # Resolved AFTER anchor — excluded (future)
                {"symbol": "AAPL", "outcome": "LOSS",
                  "return_pct": -0.04,
                  "resolved_at": "2024-06-15T10:00:00+00:00"},
                # Different symbol — excluded
                {"symbol": "MSFT", "outcome": "WIN",
                  "return_pct": 0.20,
                  "resolved_at": "2024-05-20T10:00:00+00:00"},
                # OPEN — excluded (future leak)
                {"symbol": "AAPL", "outcome": "OPEN",
                  "return_pct": None,
                  "resolved_at": "2024-05-22T10:00:00+00:00"},
            ]
            self._build_signal_outcomes_db(db, rows)

            # Bars: single anchor at 2024-06-01
            bars = pd.DataFrame({
                "ts_utc": [anchor],
                "open": [100.0], "high": [101.0], "low": [99.0],
                "close": [100.0], "volume": [1e6],
                "quality_flags": [0],
            })
            out = signal_history.compute(bars, symbol="AAPL",
                                           db_path=db)
            # 30d: AAPL closed in [2024-05-02, 2024-06-01)
            # → WIN (5/20), LOSS (5/25) = 2 outcomes, 1 win
            #   → win_rate_30d = 0.5
            self.assertEqual(int(out["signal_history.signals_count_30d"]
                                    .iloc[0]), 2)
            self.assertAlmostEqual(
                float(out["signal_history.win_rate_30d"].iloc[0]),
                0.5, places=10)
            # 90d: same 2 plus the 4/10 WIN = 3 outcomes, 2 wins
            #   → win_rate_90d = 2/3, avg_return = (0.05 -0.02 +0.08)/3
            self.assertEqual(int(out["signal_history.signals_count_90d"]
                                    .iloc[0]), 3)
            self.assertAlmostEqual(
                float(out["signal_history.win_rate_90d"].iloc[0]),
                2.0 / 3.0, places=10)
            self.assertAlmostEqual(
                float(out["signal_history.avg_return_pct_90d"]
                        .iloc[0]),
                (0.05 - 0.02 + 0.08) / 3.0, places=10)

    def test_flywheel_reader_is_read_only(self):
        """Open a writable DB but verify the reader's connection
        rejects writes."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "ro_test.db"
            self._build_signal_outcomes_db(db, [
                {"symbol": "AAPL", "outcome": "WIN",
                  "return_pct": 0.01,
                  "resolved_at": "2024-05-01T00:00:00+00:00"}])
            reader = flywheel_reader.FlywheelReader(db)
            self.assertTrue(reader.is_available())
            # Open the reader's connection internally and try a write
            conn = reader._open_ro()
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("DELETE FROM signal_outcomes")
                    conn.commit()
            finally:
                conn.close()


# ═════════════════════════════════════════════════════════════════════
# G3 — Label compute groups (M18.A.4)
# ═════════════════════════════════════════════════════════════════════


def _trending_bars_for_labels(direction: str = "up", n: int = 80,
                                bar_size: float = 1.0,
                                hl_spread: float = 0.5):
    """Build a deterministic bars frame with a strict monotone
    direction at a fixed bar size. Used to verify which barrier
    (target / stop) gets hit in the triple-barrier label."""
    if direction not in ("up", "down", "flat"):
        raise ValueError(direction)
    sign = {"up": 1.0, "down": -1.0, "flat": 0.0}[direction]
    base = 100.0
    closes = np.array([base + sign * i * bar_size for i in range(n)])
    opens  = np.concatenate([[base], closes[:-1]])
    highs  = np.maximum(opens, closes) + hl_spread
    lows   = np.minimum(opens, closes) - hl_spread
    return pd.DataFrame({
        "ts_utc": pd.date_range("2024-01-02", periods=n,
                                  freq="1D", tz="UTC"),
        "open":   opens,
        "high":   highs,
        "low":    lows,
        "close":  closes,
        "volume": np.full(n, 1_000_000.0),
        "quality_flags": 0,
    })


def _atr_at_start(bars: pd.DataFrame, value: float) -> pd.Series:
    """A constant ATR series — useful for analytical fixture tests
    where we want predictable target/stop levels."""
    return pd.Series(np.full(len(bars), value), index=bars.index)


# ─────────────────────────────────────────────────────────────────────
# G3_TripleBarrier
# ─────────────────────────────────────────────────────────────────────

class G3_TripleBarrier(unittest.TestCase):

    LID = "triple_barrier_atr_2_3_50"

    def test_target_hit_in_uptrend(self):
        """Monotone uptrend at +1 per bar with HL spread 0.5 and
        ATR=1.0 → target = entry + 2.0 (close after 2 bars), stop =
        entry - 3.0 (never hit). Expect every resolved row to have
        label = +1."""
        bars = _trending_bars_for_labels(direction="up", n=80,
                                            bar_size=1.0, hl_spread=0.5)
        atr = _atr_at_start(bars, 1.0)
        out = triple_barrier.compute(bars, atr_series=atr)
        resolved = out[out[f"{self.LID}.is_pending"] == 0]
        # In a strict uptrend with ATR=1, target=entry+2 is reached
        # within 2-3 bars; stop=entry-3 is unreachable. All should be +1.
        self.assertTrue((resolved[self.LID] == 1.0).all(),
            f"uptrend → expected all +1, got distribution "
            f"{resolved[self.LID].value_counts().to_dict()}")

    def test_stop_hit_in_downtrend(self):
        bars = _trending_bars_for_labels(direction="down", n=80,
                                            bar_size=1.0, hl_spread=0.5)
        atr = _atr_at_start(bars, 1.0)
        out = triple_barrier.compute(bars, atr_series=atr)
        resolved = out[out[f"{self.LID}.is_pending"] == 0]
        self.assertTrue((resolved[self.LID] == -1.0).all(),
            f"downtrend → expected all -1, got distribution "
            f"{resolved[self.LID].value_counts().to_dict()}")

    def test_timeout_in_flat_market_with_wide_atr(self):
        """Flat bars with small HL spread and WIDE ATR → neither
        target (+2*ATR) nor stop (-3*ATR) ever reached → all
        resolved rows are 0 (timeout)."""
        bars = _trending_bars_for_labels(direction="flat", n=80,
                                            bar_size=0.0, hl_spread=0.1)
        atr = _atr_at_start(bars, 10.0)   # way too wide to hit
        out = triple_barrier.compute(bars, atr_series=atr)
        resolved = out[out[f"{self.LID}.is_pending"] == 0]
        self.assertGreater(len(resolved), 0,
            "expected some rows to resolve as timeout")
        self.assertTrue((resolved[self.LID] == 0.0).all(),
            f"flat market w/ wide ATR → expected all 0 (timeout), "
            f"got {resolved[self.LID].value_counts().to_dict()}")

    def test_pending_for_last_window(self):
        """The last 50 anchors cannot resolve (need 50 forward bars).
        Plus the very last row also has no i+1 entry bar."""
        bars = _trending_bars_for_labels(direction="up", n=100,
                                            bar_size=0.5, hl_spread=0.2)
        atr = _atr_at_start(bars, 1.0)
        out = triple_barrier.compute(bars, atr_series=atr)
        # Anchor i needs i+1..i+50 forward bars → resolves only if
        # i+50 < 100, i.e., i < 50. So last 50 rows are pending.
        pending_count = int(out[f"{self.LID}.is_pending"].sum())
        self.assertEqual(pending_count, 50,
            f"expected exactly 50 pending rows, got {pending_count}")
        # Pending rows must have NaN label and NaT resolved_ts
        pending_rows = out[out[f"{self.LID}.is_pending"] == 1]
        self.assertTrue(pending_rows[self.LID].isna().all())
        self.assertTrue(
            pd.isna(pending_rows[f"{self.LID}.resolved_ts"]).all())

    def test_same_bar_tie_pessimistic_stop_first(self):
        """Construct a bar where high >= target AND low <= stop on
        the SAME bar. Pessimistic convention = label is -1."""
        # Bar 0: entry happens on bar 1's open.
        # Construct so bar 1's open=100, atr=1.0 →
        #   target = 102, stop = 97
        # Bar 1 itself: high=103 (>=target), low=96 (<=stop) → tie
        n = 60
        opens  = np.full(n, 100.0)
        closes = np.full(n, 100.0)
        highs  = np.full(n, 100.5)
        lows   = np.full(n,  99.5)
        # Override bar 1 (the entry bar) to have the tie
        opens[1]  = 100.0
        highs[1]  = 103.0   # >= 102 target
        lows[1]   = 96.0    # <= 97 stop
        closes[1] = 100.0
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open":   opens, "high": highs, "low": lows, "close": closes,
            "volume": np.full(n, 1_000_000.0),
            "quality_flags": 0,
        })
        atr = _atr_at_start(bars, 1.0)
        out = triple_barrier.compute(bars, atr_series=atr)
        # Anchor 0 → entry bar 1 → tie → label=-1, resolved at bar 1
        self.assertEqual(float(out[self.LID].iloc[0]), -1.0,
            "same-bar tie must resolve pessimistic_stop_first → -1")
        self.assertEqual(
            int(out[f"{self.LID}.bars_to_resolution"].iloc[0]), 1)

    def test_resolved_ts_strictly_after_anchor(self):
        bars = _trending_bars_for_labels(direction="up", n=80,
                                            bar_size=0.6, hl_spread=0.3)
        atr = _atr_at_start(bars, 1.0)
        out = triple_barrier.compute(bars, atr_series=atr)
        assert_label_resolved_after_anchor(bars, self.LID, out)

    def test_nan_atr_yields_pending(self):
        bars = _trending_bars_for_labels(direction="up", n=80)
        atr = pd.Series(np.full(len(bars), np.nan), index=bars.index)
        out = triple_barrier.compute(bars, atr_series=atr)
        # No anchor can resolve without a valid ATR.
        self.assertTrue(
            (out[f"{self.LID}.is_pending"] == 1).all(),
            "every row must be pending when ATR is all NaN")

    def test_specs_use_future_label_only_leak_class(self):
        for s in triple_barrier.SPECS:
            self.assertEqual(s.leak_class, "future_label_only")
            self.assertEqual(s.label_class, "classification_3way")
            self.assertEqual(s.tp_mult, 2.0)
            self.assertEqual(s.sl_mult, 3.0)
            self.assertEqual(s.horizon_bars, 50)
            self.assertEqual(s.tie_breaker, "pessimistic_stop_first")
            self.assertEqual(s.entry_price_source,
                              "next_bar_open_after_anchor")


# ─────────────────────────────────────────────────────────────────────
# G3_ForwardReturns (raw + cost-adjusted)
# ─────────────────────────────────────────────────────────────────────

class G3_ForwardReturns(unittest.TestCase):

    def test_fwd_log_ret_known_geometric_series(self):
        # close[i+5] = open[i+1] * 1.01^5 → fwd_log_ret_5 = 5*ln(1.01)
        n = 30
        close = 100.0 * np.power(1.01, np.arange(n))
        open_ = np.concatenate([[100.0], close[:-1]])
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open": open_, "high": close * 1.005,
            "low": close * 0.995, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        out = forward_returns.compute(bars)
        # fwd_log_ret_5 at anchor 0: entry=open[1]=close[0]=100,
        # exit=close[5]=100*1.01^5 → log = 5*ln(1.01)
        expected = 5.0 * np.log(1.01)
        self.assertAlmostEqual(float(out["fwd_log_ret_5"].iloc[0]),
                                 expected, places=12)
        # fwd_log_ret_20 at anchor 0: but n=30, so 0+20=20 < 30 — OK
        expected20 = 20.0 * np.log(1.01)
        self.assertAlmostEqual(float(out["fwd_log_ret_20"].iloc[0]),
                                 expected20, places=12)

    def test_cost_adjusted_subtracts_10bps(self):
        n = 30
        close = 100.0 * np.power(1.01, np.arange(n))
        open_ = np.concatenate([[100.0], close[:-1]])
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open": open_, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        out = forward_returns.compute(bars)
        diff_5  = (out["fwd_log_ret_5"]
                    - out["fwd_log_ret_5_cost_10bps"]).dropna()
        diff_20 = (out["fwd_log_ret_20"]
                    - out["fwd_log_ret_20_cost_10bps"]).dropna()
        # Every resolved row must show exactly 0.0010 cost subtracted.
        np.testing.assert_allclose(diff_5.to_numpy(),
                                     0.0010, atol=1e-12)
        np.testing.assert_allclose(diff_20.to_numpy(),
                                     0.0010, atol=1e-12)

    def test_pending_for_tail_rows(self):
        n = 30
        bars = _trending_bars_for_labels(direction="up", n=n)
        out = forward_returns.compute(bars)
        # fwd_log_ret_5 pending: anchors where i+5 >= n → i >= 25
        # → 5 pending rows
        self.assertEqual(int(out["fwd_log_ret_5.is_pending"].sum()), 5)
        # fwd_log_ret_20 pending: i+20 >= n → i >= 10 → 20 pending
        self.assertEqual(int(out["fwd_log_ret_20.is_pending"].sum()),
                          20)
        # fwd_log_ret_1 pending: i+1 >= n → i >= 29 → 1 pending
        self.assertEqual(int(out["fwd_log_ret_1.is_pending"].sum()), 1)

    def test_resolved_ts_invariant_all_labels(self):
        bars = _trending_bars_for_labels(direction="up", n=60)
        out = forward_returns.compute(bars)
        for h in (1, 5, 20):
            assert_label_resolved_after_anchor(
                bars, f"fwd_log_ret_{h}", out)
            assert_label_resolved_after_anchor(
                bars, f"fwd_log_ret_{h}_cost_10bps", out)

    def test_specs_use_future_label_only(self):
        for s in forward_returns.SPECS:
            self.assertEqual(s.leak_class, "future_label_only")
            self.assertEqual(s.label_class, "regression")
        cost_adj = [s for s in forward_returns.SPECS
                      if s.cost_model_applied]
        self.assertEqual(len(cost_adj), 3,
            "expected 3 cost-adjusted labels (1, 5, 20 horizons)")


# ─────────────────────────────────────────────────────────────────────
# G3_MFE_MAE
# ─────────────────────────────────────────────────────────────────────

class G3_MFE_MAE(unittest.TestCase):

    def test_mfe_zero_in_strict_downtrend(self):
        """Strict downtrend: every bar makes a new low. Entry at
        bar 1's open. The forward window's highs never exceed entry
        (since prices only fall), so MFE = 0."""
        bars = _trending_bars_for_labels(direction="down", n=80,
                                            bar_size=1.0, hl_spread=0.0)
        out = mfe_mae.compute(bars)
        resolved = out[out["mfe_20.is_pending"] == 0]
        # MFE should be ~0 (window highs <= entry); MAE should be
        # large and positive.
        self.assertTrue((resolved["mfe_20"] <= 1e-9).all(),
            f"MFE should be 0 in strict downtrend; got max "
            f"{resolved['mfe_20'].max()}")
        self.assertTrue((resolved["mae_20"] > 0).all(),
            f"MAE should be > 0 in strict downtrend; got min "
            f"{resolved['mae_20'].min()}")

    def test_mae_zero_in_strict_uptrend(self):
        bars = _trending_bars_for_labels(direction="up", n=80,
                                            bar_size=1.0, hl_spread=0.0)
        out = mfe_mae.compute(bars)
        resolved = out[out["mae_20.is_pending"] == 0]
        self.assertTrue((resolved["mae_20"] <= 1e-9).all())
        self.assertTrue((resolved["mfe_20"] > 0).all())

    def test_pct_versions_are_fraction_of_entry(self):
        bars = _trending_bars_for_labels(direction="up", n=80,
                                            bar_size=0.5,
                                            hl_spread=0.3)
        out = mfe_mae.compute(bars)
        resolved = out[out["mfe_20.is_pending"] == 0]
        # mfe_pct_20 == mfe_20 / entry_price (== open[i+1])
        bar_open = bars["open"].astype(float).values
        for i in resolved.index:
            entry = bar_open[i + 1]
            expected_pct = resolved.loc[i, "mfe_20"] / entry
            self.assertAlmostEqual(
                float(resolved.loc[i, "mfe_pct_20"]),
                expected_pct, places=10)

    def test_atr_normalized_requires_atr(self):
        bars = _trending_bars_for_labels(direction="up", n=80)
        # Without atr_series, the over_atr labels must be all NaN.
        out = mfe_mae.compute(bars)
        self.assertTrue(out["mfe_over_atr_20"].isna().all())
        self.assertTrue(out["mae_over_atr_20"].isna().all())
        # With a constant ATR, they should be finite where resolved.
        out2 = mfe_mae.compute(bars,
                                 atr_series=_atr_at_start(bars, 1.0))
        resolved = out2[out2["mfe_20.is_pending"] == 0]
        self.assertFalse(resolved["mfe_over_atr_20"].isna().any())

    def test_resolved_ts_invariant(self):
        bars = _trending_bars_for_labels(direction="up", n=80)
        out = mfe_mae.compute(bars,
                                atr_series=_atr_at_start(bars, 1.0))
        for lid in ("mfe_20", "mae_20", "mfe_pct_20", "mae_pct_20",
                      "mfe_over_atr_20", "mae_over_atr_20"):
            assert_label_resolved_after_anchor(bars, lid, out)


# ─────────────────────────────────────────────────────────────────────
# G3_RiskAdjusted
# ─────────────────────────────────────────────────────────────────────

class G3_RiskAdjusted(unittest.TestCase):

    def test_over_atr_division(self):
        """fwd_log_ret_20_over_atr = fwd_log_ret_20 / (ATR/entry).
        With constant per-bar log return r and constant ATR a:
        fwd_log_ret_20 = 20r; over_atr = 20r / (a / entry).
        """
        n = 50
        close = 100.0 * np.power(1.005, np.arange(n))
        open_ = np.concatenate([[100.0], close[:-1]])
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open": open_, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        atr = pd.Series(np.full(n, 1.0), index=bars.index)
        out = risk_adjusted.compute(bars, atr_series=atr)
        # Anchor 0: entry=open[1]=100.5 (since close[0]=100,
        # close[1]=100*1.005); actually open[1]=close[0]=100.
        # exit close[20] = 100*1.005**20
        # fwd_log_ret_20 = log(close[20]/100) = 20*log(1.005)
        # over_atr = 20*log(1.005) / (1.0/100) = 2000 * log(1.005)
        expected = 20 * np.log(1.005) / (1.0 / 100.0)
        self.assertAlmostEqual(
            float(out["fwd_log_ret_20_over_atr"].iloc[0]),
            expected, places=10)

    def test_over_rvol_division(self):
        n = 50
        close = 100.0 * np.power(1.005, np.arange(n))
        open_ = np.concatenate([[100.0], close[:-1]])
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open": open_, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        rvol = pd.Series(np.full(n, 0.01), index=bars.index)
        out = risk_adjusted.compute(bars, atr_series=None,
                                       rvol_series=rvol)
        # fwd_log_ret_20 / 0.01 at anchor 0
        expected = (20 * np.log(1.005)) / 0.01
        self.assertAlmostEqual(
            float(out["fwd_log_ret_20_over_rvol"].iloc[0]),
            expected, places=10)

    def test_nan_denominator_yields_nan_value(self):
        n = 50
        bars = _trending_bars_for_labels(direction="up", n=n)
        # ATR = all NaN → over_atr all NaN even where resolved
        atr = pd.Series(np.full(n, np.nan), index=bars.index)
        rvol = pd.Series(np.full(n, np.nan), index=bars.index)
        out = risk_adjusted.compute(bars, atr_series=atr,
                                       rvol_series=rvol)
        self.assertTrue(out["fwd_log_ret_20_over_atr"].isna().all())
        self.assertTrue(out["fwd_log_ret_20_over_rvol"].isna().all())
        # But resolved_ts / is_pending should NOT mark the rows as
        # pending — the forward window resolved, the denominator is
        # just undefined.
        non_pending = (out["fwd_log_ret_20_over_atr.is_pending"]
                          == 0).sum()
        # n - 20 anchors have a valid forward window
        self.assertEqual(int(non_pending), n - 20)

    def test_resolved_ts_invariant(self):
        n = 50
        bars = _trending_bars_for_labels(direction="up", n=n)
        atr = _atr_at_start(bars, 1.0)
        rvol = _atr_at_start(bars, 0.01)
        out = risk_adjusted.compute(bars, atr_series=atr,
                                       rvol_series=rvol)
        for lid in ("fwd_log_ret_20_over_atr",
                      "fwd_log_ret_20_over_rvol"):
            assert_label_resolved_after_anchor(bars, lid, out)


# ─────────────────────────────────────────────────────────────────────
# G3_LabelLeakSafety — past-bar scramble
# ─────────────────────────────────────────────────────────────────────

class G3_LabelLeakSafety(unittest.TestCase):
    """Labels look only AT or AFTER the anchor (entry = open[i+1],
    forward window from i+1). Scrambling bars STRICTLY BEFORE the
    anchor must not change the label at the anchor — provided we
    hold the ATR series constant (since computed ATR depends on
    past bars; that dependency is correctly handled by passing
    pre-computed ATR through, not by the label group recomputing
    ATR internally).
    """

    def test_past_bar_scramble_does_not_change_labels(self):
        bars = _trending_bars_for_labels(direction="up", n=80,
                                            bar_size=0.6,
                                            hl_spread=0.3)
        atr = _atr_at_start(bars, 1.0)

        # Scramble bars 0..30 (strictly before the anchors we'll check
        # at index 40 onwards).
        anchor_lo = 40
        rng = np.random.default_rng(31415)
        scrambled = bars.copy()
        scrambled.loc[:30, "open"]   = rng.uniform(1, 1000, 31)
        scrambled.loc[:30, "high"]   = rng.uniform(1000, 2000, 31)
        scrambled.loc[:30, "low"]    = rng.uniform(0.1, 1, 31)
        scrambled.loc[:30, "close"]  = rng.uniform(1, 1000, 31)

        for mod_name, kwargs in [
            ("triple_barrier", {"atr_series": atr}),
            ("forward_returns", {}),
            ("mfe_mae", {"atr_series": atr}),
            ("risk_adjusted", {"atr_series": atr,
                                 "rvol_series": _atr_at_start(
                                     bars, 0.01)}),
        ]:
            mod = {"triple_barrier": triple_barrier,
                    "forward_returns": forward_returns,
                    "mfe_mae": mfe_mae,
                    "risk_adjusted": risk_adjusted}[mod_name]
            a = mod.compute(bars, **kwargs)
            b = mod.compute(scrambled, **kwargs)
            # Compare every label-value column at anchor_lo..end
            for col in a.columns:
                if col.endswith(".resolved_ts") \
                        or col.endswith(".is_pending"):
                    continue
                if col.endswith(".bars_to_resolution") \
                        or col.endswith(".return_log_at_resolution"):
                    continue
                av = a[col].iloc[anchor_lo:].to_numpy()
                bv = b[col].iloc[anchor_lo:].to_numpy()
                np.testing.assert_array_equal(
                    np.isnan(av), np.isnan(bv),
                    err_msg=f"{mod_name}/{col}: NaN mask differs "
                              f"under past-bar scramble")
                m = ~np.isnan(av)
                np.testing.assert_allclose(
                    av[m], bv[m],
                    rtol=1e-12, atol=1e-12,
                    err_msg=f"{mod_name}/{col}: past-bar scramble "
                              f"changed label values (leak!)")


class G10_Hygiene(unittest.TestCase):
    """Hygiene tests: no syntax errors, no socket-at-import,
    no forbidden imports, no unexpected files."""

    def test_all_bot_ml_files_compile(self):
        """Every .py file in bot/ml/ must be parseable by py_compile.
        Catches partial commits before the whole test suite runs."""
        offenders = []
        for f in _walk_bot_ml_py_files():
            try:
                ast.parse(f.read_text())
            except SyntaxError as e:
                offenders.append((str(f), str(e)))
        self.assertEqual(offenders, [],
            f"bot/ml/* syntax errors: {offenders}")

    # ---- bot.historical sole-importer rule (M18.A.2 introduces this) -

    def test_only_m16_loader_imports_bot_historical(self):
        """SR-7 — bot.historical may be imported by ONE file in
        bot/ml/* production code: bot/ml/dataset/m16_loader.py.
        Every other M18 module that needs bars must go through it.

        Mirrors test_m17_backtesting.G10_Hygiene
        .test_only_data_loader_imports_bot_historical for the
        M17.B side."""
        allowed = (Path(__file__).parent / "bot" / "ml" /
                    "dataset" / "m16_loader.py").resolve()
        offenders = []
        for f in _walk_bot_ml_py_files():
            if f.resolve() == allowed:
                continue
            for imp in _imports_in_file(f):
                if imp == "bot.historical" or imp.startswith(
                        "bot.historical."):
                    offenders.append((
                        str(f.relative_to(Path(__file__).parent)),
                        imp))
        self.assertEqual(offenders, [],
            f"bot.historical must be imported ONLY by bot/ml/dataset/"
            f"m16_loader.py; offenders: {offenders}")

    def test_no_socket_at_import_time(self):
        """Importing bot.ml + its submodules must not open any
        sockets. Runs in a SUBPROCESS so that any module-cache
        manipulation here cannot pollute the in-process test suite
        (an earlier version of this test used importlib.reload, which
        clobbered class identity for downstream G2 tests).

        The subprocess patches socket.socket to raise on construction,
        then imports every bot.ml submodule. Non-zero exit = a socket
        was opened during import.
        """
        code = (
            "import socket\n"
            "class _RaiseOnSocket:\n"
            "    def __init__(self, *a, **kw):\n"
            "        raise RuntimeError('M18 must not open sockets "
            "at import time')\n"
            "socket.socket = _RaiseOnSocket\n"
            "import bot.ml.errors\n"
            "import bot.ml.schemas\n"
            "import bot.ml.hashing\n"
            "import bot.ml.cli\n"
            "import bot.ml.dataset\n"
            "import bot.ml.dataset.m16_loader\n"
            "import bot.ml.features\n"
            "import bot.ml.features.price_return\n"
            "import bot.ml.features.trend\n"
            "import bot.ml.features.momentum\n"
            "import bot.ml.features.vol_regime\n"
            "import bot.ml.features.volume_liquidity\n"
            "import bot.ml\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent), timeout=30)
        self.assertEqual(
            result.returncode, 0,
            f"bot.ml import opened a socket. stderr:\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()
