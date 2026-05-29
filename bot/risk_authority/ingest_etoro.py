"""
bot/risk_authority/ingest_etoro.py — M14.C eToro PnL adapter.

Reads same-day closed-trade PnL via the existing M13.2 read surface
(`bot/etoro/read_adapter.EtoroReadAdapter.get_trade_history`). NO new
endpoints introduced. NO POST/DELETE/PUT/PATCH. NO demo fallback. NO
base-url override. (M14.C correction #4.)

Key design choice: this module takes an **injectable** trade-history
reader callable. Tests inject fakes; the production CLI wires the reader
to a real `EtoroReadAdapter`. Importing this module does NOT instantiate
the eToro client, does NOT read credentials, does NOT contact the
network.

Adapter never raises to the orchestrator. Any failure path → UNKNOWN
reading with an explicit error code (keys_absent / auth_unavailable /
adapter_error / parse_error).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, List, Optional

from .reading import (
    BrokerPnLReading,
    finalize_quality,
    make_unknown,
)

log = logging.getLogger(__name__)


# Signature: reader(min_date_iso: str) -> Iterable[history_item]
# Each history_item is an object (or dict) with at least:
#   - net_profit         (float; closed-trade net PnL in USD)
#   - close_timestamp    (str ISO-8601; UTC day prefix used for filtering)
TradeHistoryReader = Callable[[str], Iterable[Any]]


def _attr(o: Any, name: str, default: Any = None) -> Any:
    """Read either an attribute (dataclass/HistoryItem) or a dict key."""
    if isinstance(o, dict):
        return o.get(name, default)
    return getattr(o, name, default)


def _within_utc_day(ts: Optional[str], day: str) -> bool:
    if not isinstance(ts, str) or not ts:
        return False
    return ts[:10] == day


class EtoroPnLAdapter:
    """Read-only eToro PnL adapter. No write capability whatsoever."""

    def __init__(self,
                 *,
                 broker_scope: str,
                 history_reader: TradeHistoryReader):
        if broker_scope not in ("etoro_real", "etoro_paper"):
            raise ValueError(f"invalid eToro scope {broker_scope!r}")
        self.name = broker_scope
        self._history_reader = history_reader

    def read(self, *, today: str) -> BrokerPnLReading:
        # Read trade history for today only. The reader is expected to
        # accept an ISO-date min-date and return today's closed trades
        # (the production wiring passes the same `today` value).
        try:
            items = list(self._history_reader(today))
        except Exception as e:
            # Common explicit failure modes get explicit error codes so
            # M14.E can branch on them. Otherwise classify as adapter_error.
            name = type(e).__name__
            msg = str(e).lower()
            if "auth" in msg or "401" in msg or "403" in msg:
                err = "auth_unavailable"
            elif "key" in msg and "miss" in msg:
                err = "keys_absent"
            elif name in ("EtoroAuthError",):
                err = "auth_unavailable"
            else:
                err = f"adapter_error:{name}"
            return make_unknown(self.name, trading_day=today, error=err)

        # Filter to closed trades whose close_timestamp falls in today UTC.
        today_items: List[Any] = []
        for it in items:
            close_ts = _attr(it, "close_timestamp")
            if _within_utc_day(close_ts, today):
                today_items.append(it)

        if today_items:
            # Per ChatGPT M14.C blocker fix: if ANY same-day closed trade
            # is missing `net_profit` or has a non-numeric value, return
            # UNKNOWN. Skipping missing entries can silently undercount
            # loss. Empty same-day list (no closed trades today) is
            # already handled below as KNOWN ZERO. Date filter
            # (_within_utc_day above) ensures previous-day rows are
            # ignored before this validation runs.
            vals: List[float] = []
            for it in today_items:
                v = _attr(it, "net_profit")
                if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
                    return make_unknown(
                        self.name, trading_day=today,
                        error=f"parse_error:net_profit_missing_or_non_numeric:type={type(v).__name__}",
                    )
                try:
                    vals.append(float(v))
                except (TypeError, ValueError) as e:
                    return make_unknown(
                        self.name, trading_day=today,
                        error=f"parse_error:{e}",
                    )
            realised_total = sum(vals)
        else:
            # No closed trades today — KNOWN ZERO.
            realised_total = 0.0

        realised_loss = max(0.0, -realised_total)

        r = BrokerPnLReading(
            broker_scope=self.name,
            trading_day=today,
            fetched_at_utc=datetime.now(timezone.utc).isoformat(),
            success=True,
            realised_pnl_usd=realised_total,
            realised_daily_loss=realised_loss,
            source="ingested",
            evidence_summary={
                "history_items_total": len(items),
                "history_items_today": len(today_items),
            },
        )
        # Opportunistic position/equity data is NOT fetched here. Each
        # extra eToro read costs a rate-limited call. M14.D will own
        # exposure ingestion; this adapter focuses on PnL.
        finalize_quality(r)
        return r


__all__ = ["EtoroPnLAdapter", "TradeHistoryReader"]
