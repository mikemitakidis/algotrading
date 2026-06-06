"""bot.backtesting.execution — the bar loop.

Translates bars + signals + execution config into Trades and an
equity curve. The single function exposed is `simulate()`.

Execution model (per operator decision D4):
  * Signal generated from bar i CLOSE.
  * Entry / strategy-exit fills at bar i+1 OPEN.
  * SL / TP can trigger INTRABAR on bar i+1..N using high / low.
  * If SL and TP both touched in the same bar → assume SL first
    (pessimistic).
  * Gap-aware: if bar opens BEYOND the stop (long: open < stop),
    fill at the OPEN, not at the stop price.
  * Fees + slippage applied on every entry AND every exit.
  * No leverage. No shorts in M17.A.
  * If the backtest range ends with an open position, EOD exit at the
    last bar's close (no slippage on EOD exit — operator chose to mark
    rather than market-out).

The `exec_signal = signal.shift(1)` idiom is applied INSIDE this
module so each strategy implementation doesn't need to remember it.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from bot.backtesting.config import BacktestConfig
from bot.backtesting.ledger import Ledger
from bot.backtesting.models import BacktestWarning, EquityPoint
from bot.backtesting.portfolio import Portfolio
from bot.backtesting.strategy import (SIG_ENTRY, SIG_EXIT, SIG_FLAT, Strategy)


def simulate(
    *,
    bars: pd.DataFrame,
    signals: pd.DataFrame,
    cfg: BacktestConfig,
    ledger: Ledger,
) -> None:
    """Walk through bars, applying signals + SL/TP + fees + slippage.
    Mutates the ledger in-place. No return value.

    Bars are expected sorted ascending by ts_utc (data_loader enforces
    this). Signals must be aligned bar-for-bar with bars.
    """
    if len(bars) != len(signals):
        raise ValueError(
            f"bars/signals length mismatch: {len(bars)} vs {len(signals)}")
    if len(bars) < 2:
        # Can't trade with < 2 bars (need a next-bar to fill).
        return

    portfolio = Portfolio(cfg.execution)
    exec_cfg  = cfg.execution
    fee_rate  = exec_cfg.fee_bps      / 10_000.0
    slip_rate = exec_cfg.slippage_bps / 10_000.0
    sl_pct    = exec_cfg.stop_loss_pct
    tp_pct    = exec_cfg.take_profit_pct

    # Pre-compute exec_signal = signal.shift(1). exec_signal[i] is the
    # action to take at bar i open, derived from bar i-1's signal.
    exec_signal = signals["signal"].shift(1, fill_value=SIG_FLAT).astype("int64")

    symbol = cfg.request.symbol
    n = len(bars)

    # Entry-side bookkeeping for the currently-open position
    entry_bar_index: Optional[int] = None

    for i in range(n):
        bar = bars.iloc[i]
        bar_ts    = _to_datetime(bar["ts_utc"])
        bar_open  = float(bar["open"])
        bar_high  = float(bar["high"])
        bar_low   = float(bar["low"])
        bar_close = float(bar["close"])

        # ---- 1. Process pending strategy action from previous bar ---
        action = int(exec_signal.iloc[i])

        # 1a. ENTRY at this bar's OPEN
        if action == SIG_ENTRY and not portfolio.has_open_position:
            # Slippage on the long entry pushes fill UP.
            fill_price = bar_open * (1.0 + slip_rate)
            slippage_per_share = fill_price - bar_open

            # Compute SL/TP at the fill price.
            stop_price   = (fill_price * (1.0 - sl_pct)) if sl_pct is not None else None
            target_price = (fill_price * (1.0 + tp_pct)) if tp_pct is not None else None

            equity_mark = portfolio.equity(bar_open)
            qty, sizing_warnings = portfolio.compute_size(
                entry_price=fill_price,
                stop_price=stop_price,
                mark_equity=equity_mark,
                fee_rate=fee_rate,
            )
            for w in sizing_warnings:
                # Attach the bar's ts so the warning is locatable.
                ledger.record_warning(BacktestWarning(
                    code=w.code, message=w.message,
                    ts_utc=bar_ts, extras=w.extras))

            if qty > 0:
                notional = qty * fill_price
                fee = notional * fee_rate
                slippage = qty * slippage_per_share
                portfolio.open_long(
                    ts_utc=bar_ts, symbol=symbol, qty=qty,
                    entry_price=fill_price,
                    stop_price=stop_price,
                    target_price=target_price,
                    fee=fee, slippage=slippage,
                )
                entry_bar_index = i

        # 1b. STRATEGY EXIT at this bar's OPEN (if a position is open)
        elif action == SIG_EXIT and portfolio.has_open_position:
            # Slippage on a long exit pushes fill DOWN.
            fill_price = bar_open * (1.0 - slip_rate)
            slippage_per_share = bar_open - fill_price
            _close_and_record(
                portfolio, ledger, symbol=symbol,
                fill_price=fill_price, exit_ts=bar_ts,
                exit_reason="signal",
                fee_rate=fee_rate,
                slippage_per_share=slippage_per_share,
                entry_bar_index=entry_bar_index,
                exit_bar_index=i,
            )
            entry_bar_index = None

        # ---- 2. Intrabar SL/TP check on the currently-open position --
        # SL/TP is evaluated whenever a position is open — INCLUDING
        # the entry bar itself (i >= entry_bar_index). The entry bar's
        # high/low is part of the executed model:
        #
        #   signal at bar i close
        #   entry at bar i+1 OPEN
        #   stop_loss / take_profit can trigger intrabar via
        #     bar i+1 high/low — pessimistic SL-first if both touched
        #
        # The gap-aware-fill branch on the entry bar is degenerate
        # (we just filled at bar_open, which sits between stop_price
        # and target_price by construction), so it reduces to:
        #   bar_low  <= stop_price   -> exit at stop_price (with slip)
        #   bar_high >= target_price -> exit at target_price (with slip)
        #   both -> SL wins.
        if portfolio.has_open_position and entry_bar_index is not None \
                and i >= entry_bar_index:
            p = portfolio.position
            sl_hit = p.stop_price is not None and bar_low  <= p.stop_price
            tp_hit = p.target_price is not None and bar_high >= p.target_price

            if sl_hit and tp_hit:
                # Pessimistic: assume SL first.
                _process_sl_exit(
                    bar_open=bar_open, stop_price=p.stop_price,
                    portfolio=portfolio, ledger=ledger,
                    symbol=symbol, exit_ts=bar_ts,
                    fee_rate=fee_rate, slip_rate=slip_rate,
                    entry_bar_index=entry_bar_index,
                    exit_bar_index=i,
                )
                entry_bar_index = None
            elif sl_hit:
                _process_sl_exit(
                    bar_open=bar_open, stop_price=p.stop_price,
                    portfolio=portfolio, ledger=ledger,
                    symbol=symbol, exit_ts=bar_ts,
                    fee_rate=fee_rate, slip_rate=slip_rate,
                    entry_bar_index=entry_bar_index,
                    exit_bar_index=i,
                )
                entry_bar_index = None
            elif tp_hit:
                _process_tp_exit(
                    bar_open=bar_open, target_price=p.target_price,
                    portfolio=portfolio, ledger=ledger,
                    symbol=symbol, exit_ts=bar_ts,
                    fee_rate=fee_rate, slip_rate=slip_rate,
                    entry_bar_index=entry_bar_index,
                    exit_bar_index=i,
                )
                entry_bar_index = None

        # ---- 3. Equity curve at bar close ----------------------------
        if portfolio.has_open_position:
            pos = portfolio.position
            pmv = pos.qty * bar_close
        else:
            pmv = 0.0
        ledger.record_equity(
            ts_utc=bar_ts,
            equity=portfolio.cash + pmv,
            cash=portfolio.cash,
            position_qty=portfolio.position.qty if portfolio.has_open_position else 0.0,
            position_market_value=pmv,
        )

    # ---- 4. EOD exit: any position still open at the last bar -------
    if portfolio.has_open_position:
        last_bar = bars.iloc[-1]
        last_close = float(last_bar["close"])
        last_ts    = _to_datetime(last_bar["ts_utc"])
        # EOD exit: no slippage applied — operator chose 'mark-to-close'
        # rather than 'market-on-close' for the M17.A end-of-data exit.
        _close_and_record(
            portfolio, ledger, symbol=symbol,
            fill_price=last_close, exit_ts=last_ts,
            exit_reason="eod",
            fee_rate=fee_rate,
            slippage_per_share=0.0,
            entry_bar_index=entry_bar_index,
            exit_bar_index=n - 1,
        )

        # The equity curve was recorded at the last bar's close BEFORE
        # the EOD exit ran, so its last value is mark-to-close
        # (= cash_pre_exit + qty * last_close). After the EOD close
        # charges an exit fee, the actual final equity is
        # post-close cash. Replace the last point so:
        #   metrics.final_equity reflects the post-fee value
        #   metrics.total_return_pct reflects the realised return
        if ledger.equity_curve:
            stale = ledger.equity_curve[-1]
            ledger.equity_curve[-1] = EquityPoint(
                ts_utc=stale.ts_utc,
                equity=portfolio.cash,    # position is closed -> equity = cash
                cash=portfolio.cash,
                position_qty=0.0,
                position_market_value=0.0,
            )


# ─────────────────────────────────────────────────────────────────────
# Exit helpers
# ─────────────────────────────────────────────────────────────────────

def _process_sl_exit(*, bar_open, stop_price, portfolio, ledger,
                       symbol, exit_ts, fee_rate, slip_rate,
                       entry_bar_index, exit_bar_index):
    """Stop-loss exit with gap-aware fill.

    If the bar OPENS at or below the stop (long), the market gapped
    through — fill at the OPEN price, not at the stop. Otherwise fill
    at the stop price. Slippage is applied as a worsening of the fill
    (long exit slip pushes price DOWN)."""
    if bar_open <= stop_price:
        # Gap below the stop — fill at the open.
        gross_fill = bar_open
    else:
        gross_fill = stop_price
    fill_price = gross_fill * (1.0 - slip_rate)
    slippage_per_share = gross_fill - fill_price
    _close_and_record(
        portfolio, ledger, symbol=symbol,
        fill_price=fill_price, exit_ts=exit_ts,
        exit_reason="stop_loss",
        fee_rate=fee_rate,
        slippage_per_share=slippage_per_share,
        entry_bar_index=entry_bar_index,
        exit_bar_index=exit_bar_index,
    )


def _process_tp_exit(*, bar_open, target_price, portfolio, ledger,
                       symbol, exit_ts, fee_rate, slip_rate,
                       entry_bar_index, exit_bar_index):
    """Take-profit exit. Gap above target -> fill at OPEN (better for
    us). Otherwise fill at target. Slippage worsens by slip_rate."""
    if bar_open >= target_price:
        gross_fill = bar_open
    else:
        gross_fill = target_price
    fill_price = gross_fill * (1.0 - slip_rate)
    slippage_per_share = gross_fill - fill_price
    _close_and_record(
        portfolio, ledger, symbol=symbol,
        fill_price=fill_price, exit_ts=exit_ts,
        exit_reason="take_profit",
        fee_rate=fee_rate,
        slippage_per_share=slippage_per_share,
        entry_bar_index=entry_bar_index,
        exit_bar_index=exit_bar_index,
    )


def _close_and_record(portfolio, ledger, *, symbol, fill_price,
                         exit_ts, exit_reason, fee_rate,
                         slippage_per_share,
                         entry_bar_index, exit_bar_index):
    """Close the position, record a Trade in the ledger.

    Trade.slippage_paid records the ROUND-TRIP $ slippage:
        position.entry_slippage  (stored by open_long)
      + qty * slippage_per_share (this exit)

    Trade.fees_paid similarly records the round trip:
        position.fees_paid  (entry fee, stored by open_long)
      + qty * fill_price * fee_rate  (this exit's fee)
    """
    p = portfolio.position
    qty = p.qty
    entry_fee = p.fees_paid
    entry_slip = p.entry_slippage
    exit_notional = qty * fill_price
    exit_fee = exit_notional * fee_rate
    exit_slip_total = qty * slippage_per_share

    pnl_abs, pnl_pct, _ = portfolio.close_long(
        exit_price=fill_price, fee=exit_fee,
        slippage=exit_slip_total,
    )

    bars_held = (exit_bar_index - entry_bar_index) if entry_bar_index is not None else 0
    ledger.record_trade(
        symbol=symbol, qty=qty,
        entry_ts_utc=p.entry_ts_utc,
        entry_price=p.entry_price,
        exit_ts_utc=exit_ts,
        exit_price=fill_price,
        exit_reason=exit_reason,
        fees_paid=entry_fee + exit_fee,
        slippage_paid=entry_slip + exit_slip_total,   # round-trip
        pnl_absolute=pnl_abs,
        pnl_pct=pnl_pct,
        bars_held=bars_held,
    )


def _to_datetime(v):
    """Normalize ts_utc to datetime regardless of input type."""
    if hasattr(v, "to_pydatetime"):
        return v.to_pydatetime()
    return v


__all__ = ["simulate"]
