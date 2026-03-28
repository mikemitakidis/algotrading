"""
bot/brokers/base.py
Abstract BrokerAdapter interface — Milestone 10.

All broker implementations (paper, IBKR, eToro) must subclass BrokerAdapter.
The execution layer never imports a broker directly — it goes through get_broker().

M10 scope: paper trading only. IBKR is a placeholder.
No live orders in this milestone.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class OrderIntent:
    """
    A fully-described order intent before it reaches the broker.
    Created by the execution layer from a signal dict.
    """
    signal_id:    int
    symbol:       str
    direction:    str          # 'long' | 'short'
    route:        str          # 'IBKR' | 'ETORO' | 'WATCH'
    entry_price:  float
    stop_loss:    float
    target_price: float
    valid_count:  int
    strategy_version: int
    created_at:   str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Risk fields populated by RiskManager before submission
    position_size:    Optional[float] = None
    risk_usd:         Optional[float] = None
    risk_checks:      dict = field(default_factory=dict)


@dataclass
class OrderResult:
    """
    Result returned by broker.submit() regardless of outcome.
    """
    intent:        OrderIntent
    status:        str          # 'accepted' | 'rejected' | 'paper_logged' | 'error'
    broker_order_id: Optional[str] = None
    reason:        Optional[str] = None
    filled_price:  Optional[float] = None
    submitted_at:  str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class BrokerAdapter(ABC):
    """Abstract base for all broker integrations."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short broker name, e.g. 'paper', 'ibkr'."""

    @property
    def is_live(self) -> bool:
        """True only for real live-money brokers. False for paper/shadow."""
        return False

    @abstractmethod
    def submit(self, intent: OrderIntent) -> OrderResult:
        """
        Submit an order intent to the broker.
        Must NEVER raise — return OrderResult with status='error' on any failure.
        """

    def cancel(self, broker_order_id: str) -> bool:
        """Cancel an open order. Returns True if successful."""
        return False

    def get_positions(self) -> list:
        """Return list of current open positions (broker-specific dicts)."""
        return []
