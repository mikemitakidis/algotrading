"""
M13.3 — eToro order schema validator.

Pure functions. No I/O. Validates an OrderIntent against the eToro
schema documented in docs/M13_1_order_schema_mapping.md.

Returned `rejection_reason` codes (used by PaperEtoroBroker and later
the live broker in M13.5):

  etoro_validation_direction         — direction not 'long' or 'short'
  etoro_validation_unresolved_symbol — symbol→instrumentId not resolved
  etoro_validation_no_rate           — no current rate available
  etoro_validation_stop_side         — stop_loss on wrong side of bid/ask
  etoro_validation_target_side       — target_price on wrong side of bid/ask
  etoro_validation_min_amount        — position_size below minimum USD
  etoro_validation_currency          — position_size not a positive USD value
  etoro_validation_leverage          — leverage other than 1 (v1 hard-coded)
  etoro_validation_no_stop           — stop_loss missing/zero without IsNoStopLoss flag

These are BROKER/PAYLOAD validation failures, NOT portfolio risk
rejections. Callers must return OrderResult(status='rejected', ...)
— never status='risk_rejected'.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ValidationResult:
    ok: bool
    rejection_reason: Optional[str] = None
    would_be_body: Dict[str, Any] = field(default_factory=dict)


def _build_would_be_body(intent_symbol: str,
                         direction: str,
                         instrument_id: Optional[int],
                         amount_usd: Optional[float],
                         stop_loss: Optional[float],
                         take_profit: Optional[float]) -> Dict[str, Any]:
    """Build the dict that WOULD be POSTed to eToro on submit.

    Includes fields even when None so audit captures the full attempt.
    Schema source: docs/M13_1_order_schema_mapping.md
    """
    is_buy = (direction == 'long') if direction in ('long', 'short') else None
    has_stop = stop_loss is not None and stop_loss > 0
    has_tp = take_profit is not None and take_profit > 0
    body: Dict[str, Any] = {
        'InstrumentID':   instrument_id,
        'IsBuy':          is_buy,
        'Leverage':       1,                     # v1 hard-coded
        'Amount':         amount_usd,
        'StopLossRate':   stop_loss if has_stop else None,
        'TakeProfitRate': take_profit if has_tp else None,
        'IsTslEnabled':   None,                  # trailing stops: future
        'IsNoStopLoss':   None if has_stop else True,
        'IsNoTakeProfit': None if has_tp else True,
    }
    return body


def validate_open(intent,
                  instrument_id: Optional[int],
                  current_rate,
                  min_amount_usd: float = 10.0,
                  leverage: int = 1) -> ValidationResult:
    """Validate an OrderIntent for an open-position eToro POST.

    Returns ValidationResult.ok=True with would_be_body when the intent
    is submittable, otherwise ok=False with rejection_reason and the
    same would_be_body (for audit).

    Order of checks chosen so the most informative reason wins when
    multiple rules would fail.
    """
    direction = getattr(intent, 'direction', None)
    symbol = getattr(intent, 'symbol', None)
    stop_loss = getattr(intent, 'stop_loss', None)
    target_price = getattr(intent, 'target_price', None)
    position_size = getattr(intent, 'position_size', None)
    route = getattr(intent, 'route', None)

    body = _build_would_be_body(
        intent_symbol=symbol,
        direction=direction,
        instrument_id=instrument_id,
        amount_usd=position_size,
        stop_loss=stop_loss,
        take_profit=target_price,
    )

    def _fail(reason: str) -> ValidationResult:
        return ValidationResult(ok=False, rejection_reason=reason, would_be_body=body)

    # 1. Direction valid
    if direction not in ('long', 'short'):
        return _fail('etoro_validation_direction')

    # 2. Leverage check (v1 is leverage=1)
    if leverage != 1:
        return _fail('etoro_validation_leverage')

    # 3. Currency / Amount sanity: must be positive USD
    if position_size is None or not isinstance(position_size, (int, float)) \
            or position_size <= 0:
        return _fail('etoro_validation_currency')

    # 4. Min amount
    if position_size < min_amount_usd:
        return _fail('etoro_validation_min_amount')

    # 5. Instrument resolved
    if instrument_id is None:
        return _fail('etoro_validation_unresolved_symbol')

    # 6. Stop loss present (we require explicit stops in v1)
    if stop_loss is None or stop_loss <= 0:
        return _fail('etoro_validation_no_stop')

    # 7. Current rate available for side checks
    if current_rate is None:
        return _fail('etoro_validation_no_rate')
    bid = getattr(current_rate, 'bid', None)
    ask = getattr(current_rate, 'ask', None)
    if bid is None or ask is None:
        return _fail('etoro_validation_no_rate')

    # 8. Stop on correct side
    #    long: stop must be BELOW current bid
    #    short: stop must be ABOVE current ask
    if direction == 'long' and stop_loss >= bid:
        return _fail('etoro_validation_stop_side')
    if direction == 'short' and stop_loss <= ask:
        return _fail('etoro_validation_stop_side')

    # 9. Target on correct side (only if target was set)
    if target_price is not None and target_price > 0:
        if direction == 'long' and target_price <= ask:
            return _fail('etoro_validation_target_side')
        if direction == 'short' and target_price >= bid:
            return _fail('etoro_validation_target_side')

    return ValidationResult(ok=True, rejection_reason=None, would_be_body=body)
