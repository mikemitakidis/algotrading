"""bot.backtesting.metrics — pure metric computations.

Single entry point: `compute_metrics(ledger, bars, exec_cfg)` returns a
plain dict suitable for direct JSON serialisation by output.py.

Design rules:
  * Pure functions: no mutation of inputs, no I/O.
  * Inputs are the Ledger (trades + equity_curve + warnings), the bars
    DataFrame (used for B&H benchmark), and the ExecutionConfig (used
    for initial_equity).
  * NaN-safe: empty ledger -> all-zero metrics rather than NaN
    propagation, except where 'undefined' is the truthful answer
    (Sharpe with <2 returns).
  * Sharpe / Sortino gated: require >=30 trades AND >=90 days to be
    statistically meaningful; otherwise emit None with a `reason` key.
  * Profit factor with zero gross loss is reported as `inf` (the
    convention; a pure-winners ledger has no defined ratio).

Returned dict shape (top level):
{
  'n_trades':           int,
  'n_winners':          int,
  'n_losers':           int,
  'win_rate':           float,                  # in [0, 1]
  'total_return_pct':   float,                  # final/initial - 1
  'total_pnl_absolute': float,
  'max_drawdown_pct':   float,                  # in [0, 1], positive
  'profit_factor':      float | str('inf'),
  'expectancy':         float,                  # avg pnl per trade
  'avg_win':            float,
  'avg_loss':           float,                  # negative
  'avg_bars_held':      float,
  'total_fees_paid':    float,
  'total_slippage_paid':float,
  'exposure_time_pct':  float,                  # fraction of bars in position
  'sharpe_annualised':  float | None,
  'sortino_annualised': float | None,
  'sample_size_note':   str | None,             # explains None values
  'benchmark': {
    'name':              'buy_and_hold',
    'total_return_pct':  float,
    'max_drawdown_pct':  float,
  },
}
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from bot.backtesting.config import ExecutionConfig
from bot.backtesting.ledger import Ledger
from bot.backtesting.models import EquityPoint, Trade


# Gates for Sharpe / Sortino (statistical meaningfulness):
_MIN_TRADES_FOR_RATIOS = 30
_MIN_DAYS_FOR_RATIOS   = 90

# Annualisation factor for daily returns (252 US trading days).
# For intraday timeframes the engine uses bar-count -> trading days
# heuristic via the bars DataFrame timestamps.
_TRADING_DAYS_PER_YEAR = 252


def compute_metrics(
    *,
    ledger: Ledger,
    bars: pd.DataFrame,
    exec_cfg: ExecutionConfig,
) -> Dict[str, Any]:
    """Compute the full metric dict. Pure: doesn't mutate inputs."""

    trades = ledger.trades
    equity = ledger.equity_curve

    out: Dict[str, Any] = {}

    # ---- 1. Trade-level metrics --------------------------------------
    n = len(trades)
    out["n_trades"] = n

    if n == 0:
        out.update({
            "n_winners":          0,
            "n_losers":           0,
            "win_rate":           0.0,
            "total_pnl_absolute": 0.0,
            "profit_factor":      0.0,
            "expectancy":         0.0,
            "avg_win":            0.0,
            "avg_loss":           0.0,
            "avg_bars_held":      0.0,
        })
    else:
        pnls    = np.array([t.pnl_absolute for t in trades], dtype=float)
        winners = pnls[pnls > 0]
        losers  = pnls[pnls < 0]
        out["n_winners"]          = int(len(winners))
        out["n_losers"]           = int(len(losers))
        out["win_rate"]           = float(len(winners) / n)
        out["total_pnl_absolute"] = float(pnls.sum())
        gross_win  = float(winners.sum()) if len(winners) > 0 else 0.0
        gross_loss = float(-losers.sum()) if len(losers)  > 0 else 0.0
        if gross_loss > 0:
            out["profit_factor"] = float(gross_win / gross_loss)
        elif gross_win > 0:
            out["profit_factor"] = "inf"   # JSON-safe sentinel
        else:
            out["profit_factor"] = 0.0
        out["expectancy"]      = float(pnls.mean())
        out["avg_win"]         = float(winners.mean()) if len(winners) > 0 else 0.0
        out["avg_loss"]        = float(losers.mean())  if len(losers)  > 0 else 0.0
        out["avg_bars_held"]   = float(
            np.mean([t.bars_held for t in trades]))

    # ---- 2. Equity-curve metrics -------------------------------------
    initial_equity = float(exec_cfg.initial_equity)
    if equity:
        final_equity = float(equity[-1].equity)
    else:
        final_equity = initial_equity

    out["initial_equity"]    = initial_equity
    out["final_equity"]      = final_equity
    out["total_return_pct"]  = (
        float(final_equity / initial_equity - 1.0)
        if initial_equity > 0 else 0.0
    )

    # Max drawdown from equity curve (positive number; 0.15 = 15%).
    if len(equity) >= 2:
        eq_series = pd.Series([e.equity for e in equity], dtype=float)
        running_peak = eq_series.cummax()
        drawdown = (eq_series - running_peak) / running_peak.where(
            running_peak > 0, np.nan)
        mdd = float(-drawdown.min()) if len(drawdown.dropna()) > 0 else 0.0
        out["max_drawdown_pct"] = mdd if mdd > 0 else 0.0
    else:
        out["max_drawdown_pct"] = 0.0

    # ---- 3. Fees / slippage / exposure -------------------------------
    out["total_fees_paid"]     = float(sum(t.fees_paid     for t in trades))
    out["total_slippage_paid"] = float(sum(t.slippage_paid for t in trades))

    if len(equity) > 0:
        bars_in_position = sum(
            1 for e in equity if e.position_qty > 0)
        out["exposure_time_pct"] = float(bars_in_position / len(equity))
    else:
        out["exposure_time_pct"] = 0.0

    # ---- 4. Sharpe / Sortino with sample-size gate -------------------
    sharpe, sortino, note = _compute_risk_adjusted(
        equity=equity, bars=bars, n_trades=n)
    out["sharpe_annualised"]  = sharpe
    out["sortino_annualised"] = sortino
    out["sample_size_note"]   = note

    # ---- 5. Buy-and-hold benchmark from the same bars ---------------
    out["benchmark"] = _buy_and_hold_benchmark(bars)

    return out


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _compute_risk_adjusted(
    *,
    equity: List[EquityPoint],
    bars: pd.DataFrame,
    n_trades: int,
):
    """Compute annualised Sharpe + Sortino, or (None, None, reason)
    if the sample is too small to be meaningful."""

    if len(equity) < 2:
        return None, None, "insufficient_equity_points"

    # Day-count gate: span of equity timestamps must be >= MIN_DAYS.
    first_ts = pd.Timestamp(equity[0].ts_utc)
    last_ts  = pd.Timestamp(equity[-1].ts_utc)
    if first_ts.tz is None:
        first_ts = first_ts.tz_localize("UTC")
    if last_ts.tz is None:
        last_ts = last_ts.tz_localize("UTC")
    days_span = (last_ts - first_ts).days
    if days_span < _MIN_DAYS_FOR_RATIOS:
        return None, None, (
            f"insufficient_days ({days_span} < {_MIN_DAYS_FOR_RATIOS})")
    if n_trades < _MIN_TRADES_FOR_RATIOS:
        return None, None, (
            f"insufficient_trades ({n_trades} < {_MIN_TRADES_FOR_RATIOS})")

    eq_vals = np.array([e.equity for e in equity], dtype=float)
    # Bar-to-bar simple returns; first value undefined.
    rets = np.diff(eq_vals) / eq_vals[:-1]
    rets = rets[np.isfinite(rets)]
    if len(rets) < 2:
        return None, None, "insufficient_returns"

    mean = float(rets.mean())
    std  = float(rets.std(ddof=1))
    # Downside std uses only negative returns (Sortino definition).
    downside = rets[rets < 0]
    downside_std = float(downside.std(ddof=1)) if len(downside) >= 2 else 0.0

    if std == 0 or not math.isfinite(std):
        sharpe = None
    else:
        sharpe = float(mean / std * math.sqrt(_TRADING_DAYS_PER_YEAR))

    if downside_std <= 0 or not math.isfinite(downside_std):
        sortino = None
    else:
        sortino = float(
            mean / downside_std * math.sqrt(_TRADING_DAYS_PER_YEAR))

    return sharpe, sortino, None


def _buy_and_hold_benchmark(bars: pd.DataFrame) -> Dict[str, Any]:
    """B&H return for the same symbol over the same window.
    Buys 1 share at first bar's CLOSE; sells at last bar's CLOSE."""
    if bars is None or len(bars) < 2:
        return {
            "name":               "buy_and_hold",
            "total_return_pct":   0.0,
            "max_drawdown_pct":   0.0,
        }
    closes = bars["close"].astype(float)
    first = float(closes.iloc[0])
    last  = float(closes.iloc[-1])
    total_return_pct = (last / first - 1.0) if first > 0 else 0.0

    running_peak = closes.cummax()
    drawdown = (closes - running_peak) / running_peak.where(
        running_peak > 0, np.nan)
    mdd = float(-drawdown.min()) if len(drawdown.dropna()) > 0 else 0.0
    return {
        "name":               "buy_and_hold",
        "total_return_pct":   float(total_return_pct),
        "max_drawdown_pct":   mdd if mdd > 0 else 0.0,
    }


__all__ = ["compute_metrics"]
