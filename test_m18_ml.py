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
from contextlib import redirect_stderr, redirect_stdout
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
from bot.ml.features import (
    price_return, trend, momentum, vol_regime, volume_liquidity,
)


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
