"""M21 PR-d — monitoring check: is the paper loop blocked by missing or stale risk data?

Since PR-c the loop refreshes the paper account's exposure and P&L before every M14
call, and M14 blocks when that data is missing or too old. Both failures are safe (no
trade) but silent: the loop just keeps recording a blocked entry. This check reads the
loop journal's cycle reasons (already loaded read-only by bot.monitoring.probe) and
alarms when the most recent cycles were all blocked for a data reason.

Pure function, no I/O. Severity WARN: stale data can stop trading, never cause it.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from bot.monitoring.checks import ALARM, OK, WARN, CheckResult

CHECK = "risk_data_blocks"
DATA_BLOCK_REASONS = (
    "exposure_stale", "exposure_unknown", "daily_pnl_unknown",
    "combined_exposure_unknown", "global_open_positions_unknown",
    "global_daily_loss_unknown",
)


def is_data_block(reason: Any) -> bool:
    """``reason`` is a loop_cycles.reason; the loop records a risk-gate refusal
    as ``risk:<gate reason>`` (bot.paper_loop_d2.scheduler)."""
    if not isinstance(reason, str):
        return False
    if reason.startswith("risk:"):
        reason = reason[len("risk:"):]
    if reason.startswith(("risk_refresh_block:", "risk_refresh_error:")):
        return True
    if reason.startswith("risk_authority_block:"):
        codes = reason.split(":", 1)[1].split(",")
        return any(c in DATA_BLOCK_REASONS for c in codes)
    return False


def check_risk_data_blocks(cycles: Sequence[Mapping[str, Any]], *,
                           market_open: Optional[bool], consecutive: int = 2) -> CheckResult:
    """ALARM when the ``consecutive`` most recent cycles were all blocked by missing or
    stale risk data (refresh refused, or M14 data/freshness reasons)."""
    if market_open is False:
        return CheckResult(CHECK, OK, WARN, "market closed")
    recent = sorted((c for c in cycles if c.get("started_at_utc")),
                    key=lambda c: str(c.get("started_at_utc")), reverse=True)[:consecutive]
    if len(recent) == consecutive and all(is_data_block(c.get("reason")) for c in recent):
        return CheckResult(CHECK, ALARM, WARN,
                           "last %d cycles blocked by risk data: %s"
                           % (consecutive, "; ".join(str(c.get("reason")) for c in recent)))
    return CheckResult(CHECK, OK, WARN, "recent cycles not blocked by risk data")


__all__ = ["check_risk_data_blocks", "is_data_block", "CHECK", "DATA_BLOCK_REASONS"]
