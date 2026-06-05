"""bot.backtesting.models — runtime dataclasses.

These are the values that flow through the engine: bars, trades,
positions, equity points, warnings, and the final result envelope.

All dataclasses are frozen where possible (`Trade`, `EquityPoint`,
`BacktestWarning`) so accidental mutation downstream of the engine is
caught loudly. Mutable accumulator state lives in `bot.backtesting.ledger`
and `bot.backtesting.portfolio` — not here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional


# -- Direction / signal enums (string-typed for JSON round-tripping) ---

Direction = Literal["long", "short", "flat"]
SignalKind = Literal["entry", "exit", "flat"]


# -- Bar (typed read view; engine code uses pd.DataFrame for speed) ----

@dataclass(frozen=True)
class Bar:
    """A single OHLCV bar with UTC timestamp. Engine code iterates over
    pd.DataFrame rows for performance, but this class documents the
    canonical shape and is used in tests for clarity."""
    ts_utc: datetime
    open:  float
    high:  float
    low:   float
    close: float
    volume: float


# -- Position (mutable; lives inside portfolio) ------------------------

@dataclass
class Position:
    """An open position. Long-only in M17.A; `direction` is included
    for future short support but `short` is rejected at config validation."""
    symbol: str
    direction: Direction        # "long" only in M17.A
    qty: float                  # shares; float to keep arithmetic clean
    entry_ts_utc: datetime
    entry_price: float          # post-slippage fill price
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    fees_paid: float = 0.0      # cumulative fees on entry (exit added on close)

    @property
    def is_open(self) -> bool:
        return self.qty > 0


# -- Trade (closed; immutable) -----------------------------------------

@dataclass(frozen=True)
class Trade:
    """A closed trade. Immutable once written to the ledger."""
    symbol: str
    direction: Direction
    qty: float
    entry_ts_utc: datetime
    entry_price: float          # post-slippage
    exit_ts_utc: datetime
    exit_price: float           # post-slippage
    exit_reason: Literal["signal", "stop_loss", "take_profit", "eod"]
    fees_paid: float            # round-trip
    slippage_paid: float        # round-trip (absolute $, not bps)
    pnl_absolute: float         # net of fees + slippage
    pnl_pct: float              # net %
    bars_held: int


# -- Equity point (one per bar) ----------------------------------------

@dataclass(frozen=True)
class EquityPoint:
    """A single point on the equity curve, recorded at every bar's close."""
    ts_utc: datetime
    equity: float               # cash + position market value
    cash: float
    position_qty: float
    position_market_value: float


# -- Warning (recorded in result; never raised) ------------------------

@dataclass(frozen=True)
class BacktestWarning:
    """A non-fatal condition surfaced during a backtest run.

    Examples:
      * code='quality_flag'      — M16 reported a quality flag on a bar
      * code='zero_size_skipped' — sizing produced 0 shares; trade skipped
      * code='insufficient_cash' — sizing requires more than cash on hand
    """
    code: str
    message: str
    ts_utc: Optional[datetime] = None
    extras: Dict[str, Any] = field(default_factory=dict)


# -- BacktestResult (top-level envelope) -------------------------------

@dataclass
class BacktestResult:
    """The complete output of a single backtest run.

    Returned by `bot.backtesting.runner.run(config)`. Serialised to
    filesystem artifacts by `bot.backtesting.output.write_results`.
    """
    run_id: str
    created_at_utc: datetime
    config: Dict[str, Any]              # echoed input config (parsed dict)
    config_hash: str                    # deterministic hash of config
    coverage_metadata: Dict[str, Any]   # M16 coverage row at load time
    trades: List[Trade]
    equity_curve: List[EquityPoint]
    warnings: List[BacktestWarning]
    metrics: Dict[str, Any]             # computed by metrics.py
    bars_processed: int

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def warning_count(self) -> int:
        return len(self.warnings)
