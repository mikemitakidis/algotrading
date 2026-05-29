"""bot/risk_authority/ingest_etoro_exposure.py — M14.D eToro exposure adapter.

Read-only. Uses the existing M13.2 EtoroReadAdapter surface
(`get_portfolio() -> PortfolioSnapshot.positions`). The eToro
portfolio endpoint returns position dicts with non-normalised field
names; this adapter consumes ONLY the fields evidenced in the repo
(M13.1 docs: positionID, instrumentID, units, rate are reads M12
already performs on filled positions) and derives `exposure_usd`
defensively. If the broker does not expose either a numeric USD
notional or both numeric units AND rate, the position is malformed
and the WHOLE reading is UNKNOWN — silent skipping would understate
exposure.

NO POST/DELETE/PUT/PATCH endpoints introduced. NO demo fallback. NO
base-url override. NO order/cancel methods of any kind. NO modification
of any existing M13.2 / M13.3 / M13.5.B file.

Honesty rules:
  * `None` means unknown; never substitute 0.0.
  * Bool is rejected as numeric (bool ⊂ int).
  * Any same-snapshot position missing required fields → UNKNOWN reading.
  * Non-USD position without broker-provided USD notional → UNKNOWN.
  * Auth failure → 'auth_unavailable'; keys absent → 'keys_absent'.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, List, Optional

from bot.risk_authority.exposure_reading import (
    BrokerExposureReading,
    Position,
    _is_real_number,
    make_unknown_exposure,
)

log = logging.getLogger(__name__)

# Signature: reader() -> object exposing .positions (List[Dict]) and
# .unrealized_pnl, mirroring EtoroReadAdapter.PortfolioSnapshot.
PortfolioReader = Callable[[], Any]

_VALID_SCOPES = {"etoro_real", "etoro_paper"}


def _derive_etoro_position(raw: dict) -> tuple[Optional[Position], Optional[str]]:
    """Validate one raw eToro position dict and produce a Position.

    Evidenced fields (from M13.1 / M13.5.A docs): positionID,
    instrumentID, units (qty), rate (per-unit price), isBuy (direction).
    Optional USD-explicit fields are accepted if numeric; otherwise we
    derive `exposure_usd = |units * rate|`.
    """
    # Direction: eToro uses isBuy (bool) or sometimes 'isBuy' key
    # absent. We accept either, but require it.
    is_buy = raw.get("isBuy")
    if is_buy is None:
        is_buy = raw.get("is_buy")
    if not isinstance(is_buy, bool):
        return None, f"position_missing_isBuy:type={type(is_buy).__name__}"
    side = "long" if is_buy else "short"

    # Identifier: eToro positions identify instruments by instrumentID,
    # not symbol. We use a stringified instrumentID as the symbol when
    # no symbol is supplied; this is the same identifier the bot uses
    # elsewhere for eToro.
    sym = raw.get("symbol")
    instrument_id = raw.get("instrumentID")
    if instrument_id is None:
        instrument_id = raw.get("instrument_id")
    if not isinstance(sym, str) or not sym:
        # Fall back to instrumentID if numeric.
        if _is_real_number(instrument_id):
            sym = str(int(instrument_id))
        else:
            return None, "position_missing_symbol_and_instrument_id"

    units = raw.get("units")
    if not _is_real_number(units):
        return None, f"position_units_non_numeric:type={type(units).__name__}"

    # Prefer explicit USD notional if the broker provides it.
    usd_explicit = raw.get("broker_provided_usd_notional")
    invested = raw.get("amount")          # eToro 'Amount' (USD on real)
    if _is_real_number(usd_explicit):
        exposure_val = float(usd_explicit)
        mark_source = "broker_usd_notional"
    elif _is_real_number(invested):
        exposure_val = float(invested)
        mark_source = "amount_field"
    else:
        rate = raw.get("rate")
        if not _is_real_number(rate):
            # Neither USD notional nor numeric rate — malformed.
            return None, ("position_no_usd_notional_or_rate")
        exposure_val = abs(float(units) * float(rate))
        mark_source = "units_x_rate"

    pos = Position(
        symbol=sym,
        side=side,
        qty=float(units),
        exposure_usd=exposure_val,
        avg_price=float(raw["openRate"])
            if _is_real_number(raw.get("openRate")) else None,
        mark_price=float(raw["rate"])
            if _is_real_number(raw.get("rate")) else None,
        unrealised_pnl_usd=float(raw["profit"])
            if _is_real_number(raw.get("profit")) else None,
        opened_at=raw.get("openDateTime")
            if isinstance(raw.get("openDateTime"), str) else None,
        instrument_id=int(instrument_id)
            if _is_real_number(instrument_id) else None,
        raw_evidence={"mark_source": mark_source},
    )
    return pos, None


class EtoroExposureAdapter:
    """Read-only eToro exposure adapter."""

    def __init__(
        self,
        broker_scope: str,
        portfolio_reader: PortfolioReader,
    ):
        if broker_scope not in _VALID_SCOPES:
            raise ValueError(
                f"broker_scope must be one of {_VALID_SCOPES}, got {broker_scope!r}"
            )
        self.name = broker_scope
        self._portfolio_reader = portfolio_reader

    def read(self, *, today: str) -> BrokerExposureReading:
        # 1. Call the reader. Failures → UNKNOWN with classified error.
        try:
            snap = self._portfolio_reader()
        except Exception as e:
            name = type(e).__name__
            msg = str(e).lower()
            if "keys" in msg and "absent" in msg:
                err = "keys_absent"
            elif "auth" in msg or "401" in msg or "403" in msg:
                err = "auth_unavailable"
            else:
                err = f"portfolio_reader_failed:{name}:{e}"
            return make_unknown_exposure(self.name, trading_day=today,
                                         error=err)

        # 2. Extract positions list defensively. The M13.2
        #    PortfolioSnapshot exposes .positions (List[Dict]).
        raws = getattr(snap, "positions", None)
        if raws is None and isinstance(snap, dict):
            raws = snap.get("positions")
        if raws is None:
            return make_unknown_exposure(
                self.name, trading_day=today,
                error="portfolio_snapshot_missing_positions_field",
            )
        if not isinstance(raws, list):
            return make_unknown_exposure(
                self.name, trading_day=today,
                error=f"portfolio_positions_not_list:type={type(raws).__name__}",
            )

        # 3. Validate every position. Any malformed entry → whole
        #    reading UNKNOWN.
        positions: List[Position] = []
        for raw in raws:
            if not isinstance(raw, dict):
                return make_unknown_exposure(
                    self.name, trading_day=today,
                    error=f"position_not_dict:type={type(raw).__name__}",
                )
            pos, err = _derive_etoro_position(raw)
            if err is not None:
                return make_unknown_exposure(
                    self.name, trading_day=today, error=err,
                )
            positions.append(pos)

        capital_deployed = sum(p.exposure_usd for p in positions)

        # 4. Opportunistic equity from snapshot.credit + portfolio value.
        current_equity = None
        eq = getattr(snap, "credit", None)
        if _is_real_number(eq):
            current_equity = float(eq)

        # 5. unrealised_pnl: prefer snapshot.unrealized_pnl (eToro
        #    reports it directly).
        unrealised = getattr(snap, "unrealized_pnl", None)
        unrealised_total = float(unrealised) if _is_real_number(unrealised) else None

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
            peak_equity_usd=None,
            source="ingested",
            raw_evidence={"positions_count_raw": len(raws)},
        )


__all__ = [
    "EtoroExposureAdapter",
    "PortfolioReader",
]
