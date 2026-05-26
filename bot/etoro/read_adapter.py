"""
M13.2 — eToro read adapter (high-level typed wrappers).

Wraps exactly the verified read endpoints:

Live-verified during M13 discovery (R1, R2, R3, R5 and /me sanity check):
  * GET /me
  * GET /trading/info/portfolio
  * GET /trading/info/real/pnl
  * GET /market-data/search
  * GET /trading/info/trade/history

Docs-corrected (not live-verified during R4; corrected from official
OpenAPI spec):
  * GET /market-data/instruments/rates

Five live-verified endpoints + one docs-corrected rates endpoint.

This adapter has zero `BrokerAdapter` lineage. It cannot place orders.
It cannot be registered in the broker factory. It is library code,
dormant in M13.2 production runtime.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .client import EtoroClient

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed return shapes (defensive: missing fields default to None)
# ---------------------------------------------------------------------------
@dataclass
class IdentityResult:
    gcid:     Optional[int] = None
    realCid:  Optional[int] = None
    demoCid:  Optional[int] = None


@dataclass
class PortfolioSnapshot:
    """Subset of /trading/info/portfolio's clientPortfolio that the bot
    actually consumes. Raw dict preserved for callers that need extras."""
    credit:               Optional[float] = None
    bonus_credit:         Optional[float] = None
    unrealized_pnl:       Optional[float] = None
    account_currency_id:  Optional[int] = None
    positions:            List[Dict[str, Any]] = field(default_factory=list)
    orders:               List[Dict[str, Any]] = field(default_factory=list)
    entry_orders:         List[Dict[str, Any]] = field(default_factory=list)
    exit_orders:          List[Dict[str, Any]] = field(default_factory=list)
    mirrors:              List[Dict[str, Any]] = field(default_factory=list)
    stock_orders:         List[Dict[str, Any]] = field(default_factory=list)
    orders_for_open:      List[Dict[str, Any]] = field(default_factory=list)
    orders_for_close:     List[Dict[str, Any]] = field(default_factory=list)
    raw:                  Dict[str, Any] = field(default_factory=dict)


@dataclass
class InstrumentMatch:
    instrument_id:   Optional[int] = None
    raw:             Dict[str, Any] = field(default_factory=dict)


@dataclass
class Rate:
    instrument_id:        Optional[int] = None
    bid:                  Optional[float] = None
    ask:                  Optional[float] = None
    last_execution:       Optional[float] = None
    conversion_rate_bid:  Optional[float] = None
    conversion_rate_ask:  Optional[float] = None
    date:                 Optional[str] = None
    raw:                  Dict[str, Any] = field(default_factory=dict)


@dataclass
class HistoryItem:
    position_id:     Optional[int] = None
    parent_position_id: Optional[int] = None
    instrument_id:   Optional[int] = None
    order_id:        Optional[int] = None
    is_buy:          Optional[bool] = None
    units:           Optional[float] = None
    open_rate:       Optional[float] = None
    close_rate:      Optional[float] = None
    open_timestamp:  Optional[str] = None
    close_timestamp: Optional[str] = None
    net_profit:      Optional[float] = None
    fees:            Optional[float] = None
    investment:      Optional[float] = None
    initial_investment: Optional[float] = None
    leverage:        Optional[int] = None
    stop_loss_rate:  Optional[float] = None
    take_profit_rate: Optional[float] = None
    trailing_stop_loss: Optional[float] = None
    raw:             Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Read adapter
# ---------------------------------------------------------------------------
class EtoroReadAdapter:
    """High-level typed wrappers over verified read endpoints.

    All methods call `client.get(...)`. None calls any write method —
    there is no write method on the client to call.
    """

    # eToro caps batch size for rates
    _RATES_BATCH_MAX = 100

    def __init__(self, client: EtoroClient):
        if client is None:
            raise ValueError('client is required')
        self._client = client

    # -- /me --
    def get_identity(self) -> IdentityResult:
        d = self._client.get('/me') or {}
        return IdentityResult(
            gcid=d.get('gcid'),
            realCid=d.get('realCid'),
            demoCid=d.get('demoCid'),
        )

    # -- /trading/info/portfolio --
    def get_portfolio(self) -> PortfolioSnapshot:
        d = self._client.get('/trading/info/portfolio') or {}
        return self._parse_portfolio(d)

    # -- /trading/info/real/pnl --
    def get_real_pnl(self) -> PortfolioSnapshot:
        d = self._client.get('/trading/info/real/pnl') or {}
        return self._parse_portfolio(d)

    @staticmethod
    def _parse_portfolio(d: Dict[str, Any]) -> PortfolioSnapshot:
        cp = (d or {}).get('clientPortfolio') or {}
        def _lst(k: str) -> List[Dict[str, Any]]:
            v = cp.get(k)
            return v if isinstance(v, list) else []
        return PortfolioSnapshot(
            credit=cp.get('credit'),
            bonus_credit=cp.get('bonusCredit'),
            unrealized_pnl=cp.get('unrealizedPnL'),
            account_currency_id=cp.get('accountCurrencyId'),
            positions=_lst('positions'),
            orders=_lst('orders'),
            entry_orders=_lst('entryOrders'),
            exit_orders=_lst('exitOrders'),
            mirrors=_lst('mirrors'),
            stock_orders=_lst('stockOrders'),
            orders_for_open=_lst('ordersForOpen'),
            orders_for_close=_lst('ordersForClose'),
            raw=cp,
        )

    # -- /market-data/search --
    def search_instrument(self, query: str,
                          limit: Optional[int] = None) -> List[InstrumentMatch]:
        params: Dict[str, Any] = {'search': query}
        if limit is not None:
            params['pageSize'] = int(limit)
        d = self._client.get('/market-data/search', params=params) or {}
        items = d.get('items') if isinstance(d, dict) else None
        if not isinstance(items, list):
            return []
        out: List[InstrumentMatch] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            iid = item.get('instrumentId') or item.get('instrumentID')
            out.append(InstrumentMatch(
                instrument_id=int(iid) if isinstance(iid, (int, str)) and str(iid).isdigit() else None,
                raw=item,
            ))
        return out

    # -- /market-data/instruments/rates  (docs-corrected from R4) --
    def get_rates(self, instrument_ids: List[int]) -> List[Rate]:
        """Batched. eToro caps at 100 IDs per call (per OpenAPI spec)."""
        if not instrument_ids:
            return []
        out: List[Rate] = []
        for i in range(0, len(instrument_ids), self._RATES_BATCH_MAX):
            chunk = instrument_ids[i:i + self._RATES_BATCH_MAX]
            d = self._client.get(
                '/market-data/instruments/rates',
                params={'instrumentIds': chunk},
            ) or {}
            rates = d.get('rates') if isinstance(d, dict) else None
            if not isinstance(rates, list):
                continue
            for r in rates:
                if not isinstance(r, dict):
                    continue
                out.append(Rate(
                    instrument_id=r.get('instrumentID'),
                    bid=r.get('bid'),
                    ask=r.get('ask'),
                    last_execution=r.get('lastExecution'),
                    conversion_rate_bid=r.get('conversionRateBid'),
                    conversion_rate_ask=r.get('conversionRateAsk'),
                    date=r.get('date'),
                    raw=r,
                ))
        return out

    # -- /trading/info/trade/history --
    def get_trade_history(self, min_date: str,
                          page: Optional[int] = None,
                          page_size: Optional[int] = None) -> List[HistoryItem]:
        params: Dict[str, Any] = {'minDate': min_date}
        if page is not None:
            params['page'] = int(page)
        if page_size is not None:
            params['pageSize'] = int(page_size)
        d = self._client.get('/trading/info/trade/history', params=params)
        # eToro returned a top-level list during R5 discovery; tolerate dict-wrapped too.
        if isinstance(d, list):
            items = d
        elif isinstance(d, dict):
            items = d.get('items') or d.get('history') or d.get('data') or []
        else:
            items = []
        out: List[HistoryItem] = []
        for r in items:
            if not isinstance(r, dict):
                continue
            out.append(HistoryItem(
                position_id=r.get('positionId'),
                parent_position_id=r.get('parentPositionId'),
                instrument_id=r.get('instrumentId'),
                order_id=r.get('orderId'),
                is_buy=r.get('isBuy'),
                units=r.get('units'),
                open_rate=r.get('openRate'),
                close_rate=r.get('closeRate'),
                open_timestamp=r.get('openTimestamp'),
                close_timestamp=r.get('closeTimestamp'),
                net_profit=r.get('netProfit'),
                fees=r.get('fees'),
                investment=r.get('investment'),
                initial_investment=r.get('initialInvestment'),
                leverage=r.get('leverage'),
                stop_loss_rate=r.get('stopLossRate'),
                take_profit_rate=r.get('takeProfitRate'),
                trailing_stop_loss=r.get('trailingStopLoss'),
                raw=r,
            ))
        return out
