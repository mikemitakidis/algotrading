"""
bot/etoro/order_poller.py — M13.5.B post-POST order status polling.

Spec (M13.4B §8.3 / M13.5.A §3):
  * Bounded retries: 5 attempts, 2 seconds apart by default.
  * On terminal status (filled / cancelled / broker_rejected) → return
    that status.
  * If still pending or partially executed after the budget is
    exhausted → return 'unverified'.
  * No automatic second POST. No exponential retry escalation.

The poller calls a single, injectable read callable. The default is
constructed by the live broker; the poller itself does not import any
HTTP client, which keeps tests trivial.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

from .response_parser import (
    OrderInfoResponse,
    ParserError,
    parse_order_info,
)

log = logging.getLogger(__name__)

# Signature: callable(order_id: int) -> dict (raw JSON body) OR raises.
OrderInfoReader = Callable[[int], dict]
Sleeper = Callable[[float], None]
Clock = Callable[[], float]


@dataclass
class PollResult:
    """Outcome of poll_until_terminal()."""
    status: str                        # 'filled' | 'cancelled' | 'broker_rejected' | 'unverified' | 'submitted'
    attempts: int
    last_response: Optional[OrderInfoResponse]
    last_error: Optional[str]
    elapsed_sec: float


_TERMINAL_STATUSES = {"filled", "cancelled", "broker_rejected"}


def poll_until_terminal(
    reader: OrderInfoReader,
    order_id: int,
    *,
    max_attempts: int = 5,
    interval_sec: float = 2.0,
    sleeper: Sleeper = time.sleep,
    clock: Clock = time.time,
) -> PollResult:
    """Poll the eToro order-info endpoint until a terminal status is
    reached, or until the retry budget is exhausted.

    On exhaustion with no terminal status observed → status='unverified'.
    On parser error → status='unverified' with last_error set.
    Reader exceptions count as one failed attempt; the poller does NOT
    retry exponentially or beyond max_attempts.
    """
    if not isinstance(order_id, int) or order_id <= 0:
        raise ValueError(f"order_id must be positive int, got {order_id!r}")
    if max_attempts <= 0:
        raise ValueError("max_attempts must be > 0")
    if interval_sec < 0:
        raise ValueError("interval_sec must be >= 0")

    start = clock()
    last_response: Optional[OrderInfoResponse] = None
    last_error: Optional[str] = None
    attempt_count = 0

    for attempt in range(1, max_attempts + 1):
        attempt_count = attempt
        try:
            body = reader(order_id)
        except Exception as e:
            last_error = f"reader_exception: {type(e).__name__}: {e}"
            log.warning("[poller] attempt %d: %s", attempt, last_error)
        else:
            try:
                parsed = parse_order_info(body)
            except ParserError as e:
                last_error = f"parser_error: {e}"
                log.warning("[poller] attempt %d parse failed: %s", attempt, e)
            else:
                last_response = parsed
                internal = parsed.internal_status
                # Filled requires actual fill evidence — eToro can report
                # statusID=1 (Executed) before positions[] populates in
                # some flows. Only call it 'filled' once positions[] has
                # at least one entry with a positionID.
                if internal == "filled" and not parsed.has_positions:
                    # Treat as still pending; keep polling.
                    log.debug("[poller] statusID=1 but no positions[]; "
                              "treating as pending")
                else:
                    if internal in _TERMINAL_STATUSES:
                        return PollResult(
                            status=internal,
                            attempts=attempt_count,
                            last_response=parsed,
                            last_error=None,
                            elapsed_sec=clock() - start,
                        )
                    # Non-terminal: 'submitted'. Continue polling.

        if attempt < max_attempts:
            try:
                sleeper(interval_sec)
            except Exception as e:
                # Sleeper interrupted (e.g. test sleeper raised) — bail
                # out as unverified rather than spinning.
                last_error = f"sleeper_interrupt: {type(e).__name__}: {e}"
                break

    # Budget exhausted — fail closed.
    return PollResult(
        status="unverified",
        attempts=attempt_count,
        last_response=last_response,
        last_error=last_error,
        elapsed_sec=clock() - start,
    )


__all__ = [
    "OrderInfoReader",
    "Sleeper",
    "PollResult",
    "poll_until_terminal",
]
