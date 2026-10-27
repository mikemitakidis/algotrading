"""research.evidence.costs — explicit, scenario-based execution cost assumptions.

Counterfactual prices are modelled fills, so every label carries the cost
assumptions used to turn a gross path outcome into net R. Costs are charged
per side as basis points of price plus a per-share commission, plus an extra
penalty on gap-through stops (where real fills are worse than the open in
fast markets) and a borrow fee for shorts. ``scale`` lets reports show the
same labels at 1x and 2x costs (Issue #2 cost-stress requirement).
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class CostModel:
    name: str = "base"
    half_spread_bps: float = 2.0         # paid on entry and on exit
    slippage_bps: float = 3.0            # per side
    commission_per_share: float = 0.005  # IBKR-like tiered lower bound; assumption, not a quote
    stop_gap_extra_bps: float = 5.0      # extra adverse slippage when a stop gaps through
    borrow_bps_per_day: float = 1.0      # shorts only
    bars_per_day: float = 1.0            # to convert holding bars into days for borrow

    def scale(self, k: float) -> "CostModel":
        return replace(self, name="%s_x%g" % (self.name, k), half_spread_bps=self.half_spread_bps * k,
                       slippage_bps=self.slippage_bps * k, commission_per_share=self.commission_per_share * k,
                       stop_gap_extra_bps=self.stop_gap_extra_bps * k, borrow_bps_per_day=self.borrow_bps_per_day * k)

    def round_trip_cost(self, entry_price: float, exit_price: float, *, short: bool, holding_bars: int,
                        stop_gap: bool) -> float:
        """Total cost per share in price units (always >= 0)."""
        per_side = (self.half_spread_bps + self.slippage_bps) / 1e4
        c = per_side * (entry_price + exit_price) + 2.0 * self.commission_per_share
        if stop_gap:
            c += self.stop_gap_extra_bps / 1e4 * exit_price
        if short:
            c += self.borrow_bps_per_day / 1e4 * entry_price * holding_bars / max(self.bars_per_day, 1e-9)
        return float(c)


ZERO = CostModel(name="zero", half_spread_bps=0.0, slippage_bps=0.0, commission_per_share=0.0,
                 stop_gap_extra_bps=0.0, borrow_bps_per_day=0.0)

__all__ = ["CostModel", "ZERO"]
