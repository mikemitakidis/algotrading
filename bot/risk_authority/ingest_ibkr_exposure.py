"""bot/risk_authority/ingest_ibkr_exposure.py — M14.D IBKR exposure adapter.

Read-only. Returns BrokerExposureReading for an IBKR scope (paper or
live). Uses an injected positions_reader (and optional account_reader)
so unit tests run with mocks and the production wiring resolves the
existing M11/M12 IBKR Gateway connection separately.

This module NEVER imports, references, or calls execution methods:
  * NO IBKR order verbs (placeOrder, cancelOrder, modifyOrder, reqGlobalCancel).
  * NO HTTP write verbs (POST / DELETE / PUT / PATCH) on any client.

ib_insync does not currently expose a "read-only" connection flag for
positions/account queries (a connection used for reads is the same
connection that could place orders). We compensate by NEVER importing
or calling any execution method here. The AST-based test suite enforces
this invariant.

Honesty rules:
  * Bool is rejected as a numeric value (Python: bool ⊂ int).
  * If any same-snapshot position lacks symbol/side/qty/exposure_usd or
    has a non-numeric value, the WHOLE reading is UNKNOWN. Silent
    skipping would understate combined exposure (mirrors M14.C's
    blocker correction).
  * Non-USD position without a broker-provided USD notional → UNKNOWN.
    We do NOT invent FX rates.
  * Mark price missing but avg_price present → exposure derived from
    avg_cost; `raw_evidence.mark_source='avg_cost_fallback'` recorded
    on that position. Mark AND avg_price both missing on a position →
    that position is malformed → reading UNKNOWN.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, List, Optional

from bot.risk_authority.exposure_reading import (
    BrokerExposureReading,
    ExposureQuality,
    Position,
    _is_real_number,
    make_unknown_exposure,
)

log = logging.getLogger(__name__)

# Signature: reader() -> Iterable[dict]
# Each dict is one open-position record. Expected fields (best-effort):
#   symbol, side, qty, currency, exposure_usd?, mark_price?, avg_price?,
#   unrealised_pnl_usd?, opened_at?, instrument_id?, raw_currency_notional?,
#   broker_provided_usd_notional?
PositionsReader = Callable[[], Iterable[dict]]
AccountReader = Callable[[], dict]

_VALID_SCOPES = {"ibkr_live", "ibkr_paper"}


def _derive_position(raw: dict) -> tuple[Optional[Position], Optional[str]]:
    """Validate one raw IBKR position dict and produce a Position, or
    return (None, reason) if the dict is malformed."""
    sym = raw.get("symbol")
    if not isinstance(sym, str) or not sym:
        return None, f"position_missing_symbol:type={type(sym).__name__}"
    side = raw.get("side")
    if side not in ("long", "short"):
        return None, f"position_invalid_side:{side!r}"
    qty = raw.get("qty")
    if not _is_real_number(qty):
        return None, f"position_qty_non_numeric:type={type(qty).__name__}"

    # FX rule: any non-USD currency requires either a broker-provided
    # USD notional (`broker_provided_usd_notional`) or USD exposure
    # already on the record. We never invent FX.
    currency = raw.get("currency")
    if currency is not None and not isinstance(currency, str):
        return None, f"position_currency_non_string:type={type(currency).__name__}"
    usd_explicit = raw.get("broker_provided_usd_notional")
    exposure_usd = raw.get("exposure_usd")

    # Prefer an explicit broker-provided USD notional if present.
    if _is_real_number(usd_explicit):
        exposure_val = float(usd_explicit)
        mark_source = "broker_usd_notional"
    elif _is_real_number(exposure_usd):
        # An exposure_usd field was provided directly. We accept this
        # only if currency is USD or unset (i.e. the broker already
        # quoted the USD value). Non-USD currency without broker USD
        # notional is UNKNOWN.
        if currency and currency.upper() != "USD":
            return None, (f"position_non_usd_without_fx:currency={currency}")
        exposure_val = float(exposure_usd)
        mark_source = "exposure_usd_field"
    else:
        # Derive from qty * mark, then fall back to qty * avg_cost.
        mark = raw.get("mark_price")
        avg = raw.get("avg_price")
        if currency and currency.upper() != "USD":
            return None, f"position_non_usd_without_fx:currency={currency}"
        if _is_real_number(mark):
            exposure_val = abs(float(qty) * float(mark))
            mark_source = "qty_x_mark"
        elif _is_real_number(avg):
            exposure_val = abs(float(qty) * float(avg))
            mark_source = "avg_cost_fallback"
        else:
            return None, ("position_no_mark_or_avg_price")

    pos = Position(
        symbol=sym,
        side=side,
        qty=float(qty),
        exposure_usd=exposure_val,
        avg_price=float(raw["avg_price"])
            if _is_real_number(raw.get("avg_price")) else None,
        mark_price=float(raw["mark_price"])
            if _is_real_number(raw.get("mark_price")) else None,
        unrealised_pnl_usd=float(raw["unrealised_pnl_usd"])
            if _is_real_number(raw.get("unrealised_pnl_usd")) else None,
        opened_at=raw.get("opened_at") if isinstance(raw.get("opened_at"), str)
            else None,
        instrument_id=int(raw["instrument_id"])
            if _is_real_number(raw.get("instrument_id")) else None,
        raw_evidence={"mark_source": mark_source,
                      "currency": (currency or "USD").upper()},
    )
    return pos, None


class IBKRExposureAdapter:
    """Read-only IBKR exposure adapter."""

    def __init__(
        self,
        broker_scope: str,
        positions_reader: PositionsReader,
        account_reader: Optional[AccountReader] = None,
    ):
        if broker_scope not in _VALID_SCOPES:
            raise ValueError(
                f"broker_scope must be one of {_VALID_SCOPES}, got {broker_scope!r}"
            )
        self.name = broker_scope
        self._positions_reader = positions_reader
        self._account_reader = account_reader

    def read(self, *, today: str) -> BrokerExposureReading:
        # 1. Call the injected positions reader. Transport/auth errors
        #    become UNKNOWN — never raise to the orchestrator.
        try:
            raws = list(self._positions_reader())
        except Exception as e:
            return make_unknown_exposure(
                self.name, trading_day=today,
                error=f"positions_reader_failed:{type(e).__name__}:{e}",
            )

        # 2. Validate every position. Any malformed entry => whole
        #    reading UNKNOWN (correction analogue of M14.C blocker fix).
        positions: List[Position] = []
        for raw in raws:
            if not isinstance(raw, dict):
                return make_unknown_exposure(
                    self.name, trading_day=today,
                    error=f"position_not_dict:type={type(raw).__name__}",
                )
            pos, err = _derive_position(raw)
            if err is not None:
                return make_unknown_exposure(
                    self.name, trading_day=today, error=err,
                )
            positions.append(pos)

        capital_deployed = sum(p.exposure_usd for p in positions)

        # 3. Opportunistic account_reader. Failure here does NOT
        #    downgrade to UNKNOWN — PARTIAL is acceptable.
        current_equity = None
        if self._account_reader is not None:
            try:
                acct = self._account_reader() or {}
                if isinstance(acct, dict):
                    eq = acct.get("equity_usd") or acct.get("NetLiquidation")
                    if _is_real_number(eq):
                        current_equity = float(eq)
            except Exception as e:
                log.warning("[ibkr_exposure] account_reader failed: %s", e)
                # Stays None → PARTIAL.

        # 4. unrealised_pnl: sum from positions when all have it, else None.
        ups = [p.unrealised_pnl_usd for p in positions
               if p.unrealised_pnl_usd is not None]
        unrealised_total = sum(ups) if (positions and len(ups) == len(positions)) \
            else (0.0 if not positions else None)

        return BrokerExposureReading(
            broker_scope=self.name,
            trading_day=today,
            fetched_at_utc=datetime.now(timezone.utc).isoformat(),
            data_source_success=True,
            positions=positions,
            open_positions_count=len(positions),
            capital_deployed_usd=capital_deployed,
            unrealised_pnl_usd=unrealised_total,
            current_equity_usd=current_equity,
            peak_equity_usd=None,   # ratcheted by orchestrator, not adapter
            source="ingested",
            raw_evidence={"positions_count_raw": len(raws)},
        )


__all__ = [
    "IBKRExposureAdapter",
    "PositionsReader",
    "AccountReader",
]
