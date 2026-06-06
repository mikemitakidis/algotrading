"""bot.backtesting.ledger — accumulator for trades, equity, warnings.

The Ledger is a write-only accumulator during a backtest. After the
bar loop completes, runner.run() reads its fields and bundles them
into a BacktestResult.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal

from bot.backtesting.models import (BacktestWarning, EquityPoint, Trade)


ExitReason = Literal["signal", "stop_loss", "take_profit", "eod"]


class Ledger:
    """Append-only accumulator for trades + equity curve + warnings."""

    def __init__(self):
        self.trades:      List[Trade]            = []
        self.equity_curve: List[EquityPoint]     = []
        self.warnings:    List[BacktestWarning] = []

    # -- writes -------------------------------------------------------

    def record_trade(
        self, *,
        symbol: str,
        qty: int,
        entry_ts_utc: datetime,
        entry_price: float,
        exit_ts_utc: datetime,
        exit_price: float,
        exit_reason: ExitReason,
        fees_paid: float,
        slippage_paid: float,
        pnl_absolute: float,
        pnl_pct: float,
        bars_held: int,
    ) -> None:
        self.trades.append(Trade(
            symbol=symbol,
            direction="long",          # M17.A: long-only
            qty=float(qty),
            entry_ts_utc=entry_ts_utc,
            entry_price=float(entry_price),
            exit_ts_utc=exit_ts_utc,
            exit_price=float(exit_price),
            exit_reason=exit_reason,
            fees_paid=float(fees_paid),
            slippage_paid=float(slippage_paid),
            pnl_absolute=float(pnl_absolute),
            pnl_pct=float(pnl_pct),
            bars_held=int(bars_held),
        ))

    def record_equity(
        self, *,
        ts_utc: datetime,
        equity: float,
        cash: float,
        position_qty: float,
        position_market_value: float,
    ) -> None:
        self.equity_curve.append(EquityPoint(
            ts_utc=ts_utc,
            equity=float(equity),
            cash=float(cash),
            position_qty=float(position_qty),
            position_market_value=float(position_market_value),
        ))

    def record_warning(self, w: BacktestWarning) -> None:
        self.warnings.append(w)

    def extend_warnings(self, ws: List[BacktestWarning]) -> None:
        self.warnings.extend(ws)


__all__ = ["Ledger", "ExitReason"]
