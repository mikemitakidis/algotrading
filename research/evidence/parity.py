"""research.evidence.parity — counterfactual labels vs the M17 simulator.

For LONG candidates whose label is definitive and resolved by a stop or
target, run the production-equivalent M17 bar loop
(``bot.backtesting.execution.simulate``, ATR mode, zero fees and slippage)
on the same bars with a single entry signal, and compare exit reason, exit
bar and exit price. Exact agreement is required.

Known, documented divergence (counted separately, never as a match): when a
bar OPENS beyond the target (a favourable gap) and the same bar later also
touches the stop, M17 applies its same-bar pessimistic rule and books a stop,
although a resting take-profit would have filled at the open first. The
labeler books the target. Ambiguous labels are excluded by construction
(Issue #2 critique #1: parity is claimed only on unambiguous paths).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import numpy as np
import pandas as pd

from bot.backtesting.config import parse_config_dict
from bot.backtesting.execution import simulate
from bot.backtesting.ledger import Ledger as M17Ledger
from bot.backtesting.strategy import SIG_ENTRY


@dataclass(frozen=True)
class ParityResult:
    n_compared: int
    n_match: int
    n_mismatch: int
    n_known_divergence: int
    n_skipped: int
    mismatches: List[Dict]

    @property
    def exact(self) -> bool:
        return self.n_mismatch == 0 and self.n_compared > 0


def _cfg(symbol: str, timeframe: str, stop_mult: float, target_mult: float):
    return parse_config_dict({
        "request": {"symbol": symbol, "timeframe": timeframe, "start": "2000-01-01", "end": "2100-01-01"},
        "strategy": {"name": "sma_crossover", "params": {}},
        "execution": {"fee_bps": 0.0, "slippage_bps": 0.0, "stop_mode": "atr", "stop_atr_mult": stop_mult,
                      "target_atr_mult": target_mult, "initial_equity": 1_000_000.0,
                      "risk_per_trade_pct": 0.01, "max_position_pct": 1.0},
    })


def check(labels: pd.DataFrame, bars_by_symbol: Dict[str, pd.DataFrame], candidates_by_id: Dict,
          *, timeframe: str, tol: float = 1e-9) -> ParityResult:
    sel = labels[(labels["direction"] == "long") & (labels["status"] == "definitive")
                 & labels["outcome"].isin(["stop", "target"])]
    n_cmp = n_match = n_div = n_skip = 0
    mism: List[Dict] = []
    for r in sel.itertuples(index=False):
        bars = bars_by_symbol[r.symbol]
        cd = candidates_by_id[r.candidate_id]
        e, j = int(r.entry_idx), int(r.exit_idx)
        dec = e - 1
        if dec < 0:
            n_skip += 1
            continue
        sub = bars.iloc[dec:j + 2].reset_index(drop=True)   # decision bar .. one bar after exit
        sig = pd.DataFrame({"signal": np.zeros(len(sub), dtype=np.int64), "direction": "flat",
                            "atr_at_signal": np.nan, "entry_price_hint": sub["close"].astype(float)})
        sig.loc[0, "signal"] = SIG_ENTRY
        sig.loc[0, "direction"] = "long"
        sig.loc[0, "atr_at_signal"] = float(cd.atr_at_decision)
        cfg = _cfg(r.symbol, timeframe, cd.bracket.stop_atr_mult, cd.bracket.target_atr_mult)
        led = M17Ledger()
        simulate(bars=sub, signals=sig, cfg=cfg, ledger=led)
        trades = [t for t in led.trades if t.exit_reason in ("stop_loss", "take_profit")]
        n_cmp += 1
        exit_bar = j - dec
        if not trades:
            mism.append({"candidate_id": r.candidate_id, "why": "M17 recorded no SL/TP exit"})
            continue
        t = trades[0]
        m17_reason = "stop" if t.exit_reason == "stop_loss" else "target"
        m17_bar = int(t.bars_held) + 1                         # entry at sub index 1
        same = (m17_reason == r.outcome and m17_bar == exit_bar
                and abs(t.exit_price - r.exit_price) <= tol * max(1.0, abs(r.exit_price)))
        if same:
            n_match += 1
            continue
        b = sub.iloc[exit_bar]
        known = (r.outcome == "target" and m17_reason == "stop" and m17_bar == exit_bar
                 and b["open"] >= r.target_price and b["low"] <= r.stop_price)
        if known:
            n_div += 1
        else:
            mism.append({"candidate_id": r.candidate_id, "label": (r.outcome, exit_bar, r.exit_price),
                         "m17": (m17_reason, m17_bar, t.exit_price)})
    return ParityResult(n_cmp, n_match, len(mism), n_div, n_skip, mism)


__all__ = ["ParityResult", "check"]
