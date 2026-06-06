"""bot.backtesting.portfolio — cash, position, sizing.

The Portfolio holds runtime state for a backtest run:
  * cash (starts at execution_config.initial_equity)
  * the current open Position (M17.A: at most one at a time, long-only)
  * a small accounting log used by metrics (total fees, total slippage)

Position sizing logic — fixed_risk with max-position cap:

    risk_amount      = equity * risk_per_trade_pct
    risk_per_share   = entry_price - stop_price        (long)
    shares_by_risk   = risk_amount / risk_per_share    (if SL is set)
    max_notional     = equity * max_position_pct
    shares_by_cap    = max_notional / entry_price
    shares           = floor(min(shares_by_risk, shares_by_cap))

If `stop_loss_pct` is None, the strategy doesn't define a stop and we
fall back to cap-only sizing:
    shares = floor(max_notional / entry_price)

Zero-size guard: if `shares == 0` (tight stop or insufficient equity),
the trade is REJECTED with a BacktestWarning(code='zero_size_skipped').
NOT a failure — the backtest continues.

Insufficient-cash guard: if `shares * entry_price > cash`, we cannot
afford even the cap-sized position. Trade REJECTED with a
BacktestWarning(code='insufficient_cash').
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import List, Optional, Tuple

from bot.backtesting.config import ExecutionConfig
from bot.backtesting.models import BacktestWarning, Position


# ─────────────────────────────────────────────────────────────────────
# Portfolio
# ─────────────────────────────────────────────────────────────────────

class Portfolio:
    """Single-position cash account. Long-only in M17.A."""

    def __init__(self, exec_cfg: ExecutionConfig):
        self.cfg = exec_cfg
        self.cash: float = float(exec_cfg.initial_equity)
        self.position: Optional[Position] = None
        # Cumulative accounting (used by metrics)
        self.total_fees_paid: float = 0.0
        self.total_slippage_paid: float = 0.0

    # -- read-only views ----------------------------------------------

    @property
    def has_open_position(self) -> bool:
        return self.position is not None and self.position.is_open

    def equity(self, mark_price: float) -> float:
        """Current equity = cash + position market value at mark_price."""
        if self.has_open_position:
            return self.cash + self.position.qty * float(mark_price)
        return self.cash

    # -- sizing -------------------------------------------------------

    def compute_size(self, *, entry_price: float,
                       stop_price: Optional[float],
                       mark_equity: float,
                       fee_rate: float = 0.0,
                       ) -> Tuple[int, List[BacktestWarning]]:
        """Compute share count for a long entry. Returns (qty, warnings).

        qty == 0 means trade rejected; warnings explains why.
        qty > 0 means proceed with the trade at this size.

        `fee_rate` is the per-side fee as a fraction (e.g. 0.0005 for
        5 bps). When > 0 the affordability check reserves cash for the
        entry fee so that `cash - shares*entry - fee >= 0` after
        open_long() — i.e. cash NEVER goes negative due to fees.
        """
        warnings: List[BacktestWarning] = []

        if entry_price <= 0:
            warnings.append(BacktestWarning(
                code="invalid_entry_price",
                message=f"entry_price={entry_price} <= 0; trade rejected"))
            return 0, warnings

        max_notional = mark_equity * self.cfg.max_position_pct
        shares_by_cap = max_notional / entry_price

        if stop_price is not None and stop_price < entry_price:
            risk_amount    = mark_equity * self.cfg.risk_per_trade_pct
            risk_per_share = entry_price - stop_price
            shares_by_risk = risk_amount / risk_per_share
            shares_raw     = min(shares_by_risk, shares_by_cap)
        else:
            # No stop or invalid stop -> cap-only sizing.
            shares_raw = shares_by_cap

        shares = int(math.floor(shares_raw))

        if shares <= 0:
            warnings.append(BacktestWarning(
                code="zero_size_skipped",
                message=(f"sizing produced 0 shares "
                          f"(entry={entry_price:.2f}, "
                          f"stop={stop_price}, "
                          f"mark_equity={mark_equity:.2f}); "
                          f"trade rejected")))
            return 0, warnings

        # Affordability check INCLUDING entry fee:
        #     cash >= shares * entry_price * (1 + fee_rate)
        # => shares <= cash / (entry_price * (1 + fee_rate))
        affordable = int(math.floor(
            self.cash / (entry_price * (1.0 + fee_rate))))
        if shares > affordable:
            shares = affordable
            if shares <= 0:
                warnings.append(BacktestWarning(
                    code="insufficient_cash",
                    message=(f"cash={self.cash:.2f} cannot afford 1 "
                              f"share at {entry_price:.2f} "
                              f"with fee_rate={fee_rate}; "
                              f"trade rejected")))
                return 0, warnings

        return shares, warnings

    # -- open / close -------------------------------------------------

    def open_long(self, *, ts_utc: datetime, symbol: str,
                    qty: int, entry_price: float,
                    stop_price: Optional[float],
                    target_price: Optional[float],
                    fee: float, slippage: float) -> None:
        """Open a long position. Caller has already computed qty and
        passed in the post-slippage entry_price."""
        if self.has_open_position:
            raise RuntimeError(
                "Portfolio.open_long called while a position is already "
                "open — engine invariant violated")
        notional = qty * entry_price
        self.cash -= notional
        self.cash -= fee
        self.total_fees_paid     += fee
        self.total_slippage_paid += slippage
        self.position = Position(
            symbol=symbol,
            direction="long",
            qty=qty,
            entry_ts_utc=ts_utc,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            fees_paid=fee,
        )

    def close_long(self, *, exit_price: float, fee: float,
                     slippage: float) -> Tuple[float, float, float]:
        """Close the open long position. Returns (pnl_absolute, pnl_pct,
        gross_proceeds). Caller passes in the post-slippage exit_price."""
        if not self.has_open_position:
            raise RuntimeError(
                "Portfolio.close_long called with no open position")
        p = self.position
        gross = p.qty * exit_price
        self.cash += gross
        self.cash -= fee
        self.total_fees_paid     += fee
        self.total_slippage_paid += slippage
        cost_basis = p.qty * p.entry_price
        round_trip_fees = p.fees_paid + fee
        pnl_absolute = gross - cost_basis - round_trip_fees
        pnl_pct = pnl_absolute / cost_basis if cost_basis > 0 else 0.0
        self.position = None
        return pnl_absolute, pnl_pct, gross


__all__ = ["Portfolio"]
