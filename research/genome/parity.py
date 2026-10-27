"""research.genome.parity — trade-for-trade parity with the M17 backtester.

Runs the production single-symbol simulator (``bot.backtesting.execution.
simulate``) and the research portfolio engine on the same bars with the same
M17 strategy signals. The engine runs with ``m17_compat=True``, one position
slot and percentage stops. Every trade must match: timestamps, quantity,
prices, exit reason, fees, slippage, P&L and bars held.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from bot.backtesting.config import parse_config_dict
from bot.backtesting.execution import simulate
from bot.backtesting.ledger import Ledger as M17Ledger
from bot.backtesting.strategy import SIG_ENTRY, SIG_EXIT, SmaCrossoverStrategy
from research.genome.engine import EngineConfig, run
from research.genome.schema import ExitSpec, RiskSpec

FIELDS = ("qty", "entry_price", "exit_price", "fees_paid", "slippage_paid", "pnl_absolute", "pnl_pct")


def m17_config(symbol: str, *, fast: int, slow: int, stop_pct: Optional[float], target_pct: Optional[float],
               equity: float = 10_000.0, fee_bps: float = 5.0, slippage_bps: float = 5.0,
               risk_per_trade: float = 0.01, max_position_pct: float = 0.25):
    return parse_config_dict({
        "request": {"symbol": symbol, "timeframe": "1D", "start": "2018-01-01", "end": "2030-01-01"},
        "strategy": {"name": "sma_crossover", "params": {"fast_window": fast, "slow_window": slow}},
        "execution": {"initial_equity": equity, "fee_bps": fee_bps, "slippage_bps": slippage_bps,
                      "stop_loss_pct": stop_pct, "take_profit_pct": target_pct,
                      "risk_per_trade_pct": risk_per_trade, "max_position_pct": max_position_pct}})


def compare(bars: pd.DataFrame, symbol: str = "S", *, fast: int = 10, slow: int = 30,
            stop_pct: Optional[float] = 0.05, target_pct: Optional[float] = 0.10, equity: float = 10_000.0,
            fee_bps: float = 5.0, slippage_bps: float = 5.0, research_overrides: Optional[Dict] = None) -> Dict:
    """`research_overrides` deliberately mis-configures the research side (used to prove that the
    comparison detects differences)."""
    cfg = m17_config(symbol, fast=fast, slow=slow, stop_pct=stop_pct, target_pct=target_pct, equity=equity,
                     fee_bps=fee_bps, slippage_bps=slippage_bps)
    b = bars.reset_index(drop=True)
    sig = SmaCrossoverStrategy({"fast_window": fast, "slow_window": slow}).run(b)
    led = M17Ledger()
    simulate(bars=b, signals=sig, cfg=cfg, ledger=led)
    rs = pd.DataFrame({"entry": sig["signal"] == SIG_ENTRY, "exit": sig["signal"] == SIG_EXIT,
                       "atr": np.nan, "score": 0.0})
    ecfg = EngineConfig(initial_equity=equity, fee_bps=fee_bps, slippage_bps=slippage_bps, max_positions=1,
                        max_gross_exposure=1e9, stop_mode="pct", stop_pct=stop_pct, target_pct=target_pct,
                        m17_compat=True)
    if research_overrides:
        from dataclasses import replace as _replace
        ecfg = _replace(ecfg, **research_overrides)
    res = run({cfg.request.symbol: b}, {cfg.request.symbol: rs}, ExitSpec(), RiskSpec(0.01, 0.25), ecfg)
    m, r = led.trades, res.trades
    mism: List[str] = []
    if len(m) != len(r):
        mism.append("trade count %d (M17) vs %d (research)" % (len(m), len(r)))
    max_abs = 0.0
    for i, (a, x) in enumerate(zip(m, r)):
        if pd.Timestamp(a.entry_ts_utc) != x.entry_ts or pd.Timestamp(a.exit_ts_utc) != x.exit_ts:
            mism.append("trade %d timestamps differ" % i)
        if a.exit_reason != x.exit_reason or int(a.bars_held) != int(x.bars_held):
            mism.append("trade %d exit reason / bars held differ (%s/%d vs %s/%d)" % (
                i, a.exit_reason, a.bars_held, x.exit_reason, x.bars_held))
        for f in FIELDS:
            d = abs(float(getattr(a, f)) - float(getattr(x, f)))
            max_abs = max(max_abs, d)
            if d > 1e-6 * max(1.0, abs(float(getattr(a, f)))):
                mism.append("trade %d %s %r vs %r" % (i, f, getattr(a, f), getattr(x, f)))
    m17_final = led.equity_curve[-1].equity if led.equity_curve else equity
    eq_diff = abs(float(m17_final) - float(res.equity.iloc[-1])) if len(res.equity) else 0.0
    if eq_diff > 1e-6 * max(1.0, abs(m17_final)):
        mism.append("final equity %r vs %r" % (m17_final, float(res.equity.iloc[-1])))
    return {"trades_m17": len(m), "trades_research": len(r), "mismatches": mism, "max_abs_field_diff": max_abs,
            "final_equity_m17": float(m17_final), "final_equity_research": float(res.equity.iloc[-1]),
            "exit_reasons": sorted({x.exit_reason for x in r})}


__all__ = ["m17_config", "compare"]
