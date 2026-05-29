"""
bot/risk_authority/ingest_ibkr.py — M14.C IBKR PnL adapter.

Canonical realised PnL = sum of same-day `realizedPNL` from execution /
commission reports (per ChatGPT M14.C correction #3). Account-summary
delta is NOT used as the primary source in M14.C; it may be added as a
cross-check in a later milestone.

This adapter takes an **injectable** executions reader callable so tests
never touch a real IBKR Gateway. The production CLI wires the reader
against the existing M11/M12 IBKR connection — see
`tools/ingest_risk_state.py`.

Adapter never raises to the orchestrator. Any failure → UNKNOWN reading.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from .reading import (
    BrokerPnLReading,
    finalize_quality,
    make_unknown,
)

log = logging.getLogger(__name__)


# Signature for the injectable reader:
#   reader(today: str) -> Iterable[dict]
# Each dict represents one executed/closed trade (or one commission
# report) with at least:
#   - 'realized_pnl' (float, USD; may be 0.0)
#   - 'time_utc'     (ISO-8601 str; falls within `today` UTC for inclusion)
ExecutionsReader = Callable[[str], Iterable[dict]]


def _within_utc_day(ts: str, day: str) -> bool:
    if not isinstance(ts, str) or not ts:
        return False
    return ts[:10] == day


class IBKRPnLAdapter:
    """Read-only IBKR PnL adapter. No order placement, no cancel."""

    def __init__(self,
                 *,
                 broker_scope: str,
                 executions_reader: ExecutionsReader,
                 account_reader: Optional[Callable[[], dict]] = None):
        if broker_scope not in ("ibkr_live", "ibkr_paper"):
            raise ValueError(f"invalid IBKR scope {broker_scope!r}")
        self.name = broker_scope
        self._executions_reader = executions_reader
        self._account_reader = account_reader

    def read(self, *, today: str) -> BrokerPnLReading:
        try:
            execs = list(self._executions_reader(today))
        except Exception as e:
            return make_unknown(
                self.name, trading_day=today,
                error=f"executions_reader_failed:{type(e).__name__}:{e}",
            )

        # Filter to today UTC; sum realizedPNL. Missing fields → unknown.
        today_execs = []
        for ex in execs:
            if not isinstance(ex, dict):
                continue
            ts = ex.get("time_utc")
            if not _within_utc_day(ts or "", today):
                continue
            today_execs.append(ex)

        # If the reader returned but with malformed entries (no realized_pnl
        # in any of today's), treat as unknown — we can't trust the data.
        if today_execs:
            try:
                realised_total = sum(float(ex.get("realized_pnl", 0.0))
                                     for ex in today_execs)
            except (TypeError, ValueError) as e:
                return make_unknown(
                    self.name, trading_day=today,
                    error=f"executions_malformed:{e}",
                )
        else:
            # No trades today — KNOWN ZERO (not unknown). Distinguished
            # from unknown by quality and lifecycle.status downstream.
            realised_total = 0.0

        realised_loss = max(0.0, -realised_total)

        # Opportunistic: account summary for equity / open positions.
        # Failure here does NOT downgrade to UNKNOWN; PARTIAL is fine.
        open_positions = None
        capital_deployed = None
        peak_equity = None
        if self._account_reader is not None:
            try:
                acct = self._account_reader() or {}
                if isinstance(acct, dict):
                    op = acct.get("open_positions")
                    if isinstance(op, (int, float)):
                        open_positions = int(op)
                    cd = acct.get("capital_deployed")
                    if isinstance(cd, (int, float)):
                        capital_deployed = float(cd)
                    pe = acct.get("peak_equity")
                    if isinstance(pe, (int, float)):
                        peak_equity = float(pe)
            except Exception as e:
                log.debug("[ingest_ibkr] account_reader failed (opportunistic): %s", e)

        r = BrokerPnLReading(
            broker_scope=self.name,
            trading_day=today,
            fetched_at_utc=datetime.now(timezone.utc).isoformat(),
            success=True,
            realised_pnl_usd=realised_total,
            realised_daily_loss=realised_loss,
            open_positions=open_positions,
            capital_deployed=capital_deployed,
            peak_equity=peak_equity,
            source="ingested",
            evidence_summary={
                "execs_count_today": len(today_execs),
                "execs_count_total": len(execs),
            },
        )
        finalize_quality(r)
        return r


__all__ = ["IBKRPnLAdapter", "ExecutionsReader"]
