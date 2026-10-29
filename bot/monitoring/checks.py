"""M21 WP10 — unattended paper-loop health checks (pure functions).

Every check returns a ``CheckResult``: OK, ALARM, or UNKNOWN (cannot verify).
UNKNOWN is never treated as healthy: the monitor raises a "cannot verify"
alarm for it. Checks read only the values handed to them (journal rows,
gateway health dict, observation states); they do no I/O.

Checks
  loop_heartbeat        the loop process is ticking (last cycle start age)
  scheduler_liveness    cycles keep coming while the market is open; no
                        interrupted (crashed) cycle in the window
  data_freshness        share of stale / missing / invalid observations
  gateway               IBKR gateway service + API port, and PAPER mode
  unresolved_execution  NEEDS_REVIEW orders; PENDING_SUBMIT or close intents
                        older than allowed
  duplicate_orders      more than one active journal order per symbol, a
                        duplicated order_ref, or unattributed broker orders
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

OK, ALARM, UNKNOWN = "OK", "ALARM", "UNKNOWN"
WARN, CRITICAL = "WARN", "CRITICAL"

PAPER_PORT = 4002
GATEWAY_READY = "service_active_api_port_open"
BAD_OBSERVATIONS = ("STALE", "MISSING", "INVALID", "UNVERIFIED", "INCOMPLETE")
# A reading stamped further ahead of the monitor's clock than this is a clock
# or data contradiction: it cannot prove freshness, so it is UNKNOWN, never OK.
MAX_CLOCK_LEAD = timedelta(minutes=2)


@dataclass(frozen=True)
class CheckResult:
    check: str
    status: str            # OK | ALARM | UNKNOWN
    severity: str          # WARN | CRITICAL (meaningful when not OK)
    detail: str


def _parse(ts: Any) -> Optional[datetime]:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else None
    if isinstance(ts, str) and ts.strip():
        try:
            dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else None
    return None


def check_loop_heartbeat(last_cycle_started_utc: Any, now: datetime, *,
                         max_age: timedelta) -> CheckResult:
    t = _parse(last_cycle_started_utc)
    if t is None:
        return CheckResult("loop_heartbeat", UNKNOWN, CRITICAL, "no loop cycle recorded")
    if t - now > MAX_CLOCK_LEAD:
        return CheckResult("loop_heartbeat", UNKNOWN, CRITICAL,
                           "last cycle stamped %.1f min ahead of the monitor clock"
                           % ((t - now).total_seconds() / 60))
    age = now - t
    if age > max_age:
        return CheckResult("loop_heartbeat", ALARM, CRITICAL,
                           "last cycle started %.1f min ago (> %.1f)"
                           % (age.total_seconds() / 60, max_age.total_seconds() / 60))
    return CheckResult("loop_heartbeat", OK, CRITICAL, "last cycle %.1f min ago"
                       % (age.total_seconds() / 60))


def check_scheduler_liveness(cycles: Sequence[Mapping[str, Any]], now: datetime, *,
                             interval: timedelta, market_open: Optional[bool],
                             window: timedelta = timedelta(hours=8)) -> CheckResult:
    """``cycles``: loop_cycles rows (cycle_key, started_at_utc, state)."""
    recent = [c for c in cycles if (_parse(c.get("started_at_utc")) or now) >= now - window]
    crashed = [c["cycle_key"] for c in recent if c.get("state") == "INTERRUPTED"]
    if crashed:
        return CheckResult("scheduler_liveness", ALARM, WARN,
                           "interrupted cycles: %s" % ",".join(crashed[:5]))
    if market_open is None:
        return CheckResult("scheduler_liveness", UNKNOWN, WARN, "market state unknown")
    if market_open is False:
        return CheckResult("scheduler_liveness", OK, WARN, "market closed")
    starts = [_parse(c.get("started_at_utc")) for c in cycles]
    starts = [s for s in starts if s is not None]
    if not starts:
        return CheckResult("scheduler_liveness", ALARM, WARN, "no cycle while market open")
    gap = now - max(starts)
    if gap > 2 * interval + timedelta(minutes=2):
        return CheckResult("scheduler_liveness", ALARM, WARN,
                           "no cycle for %.1f min while market open"
                           % (gap.total_seconds() / 60))
    return CheckResult("scheduler_liveness", OK, WARN, "cycles on schedule")


def check_data_freshness(observations: Mapping[str, Mapping[str, str]], *,
                         market_open: Optional[bool],
                         max_bad_fraction: float = 0.2) -> CheckResult:
    """``observations``: {symbol: {timeframe: A01 observation state}}."""
    states = [s for per in observations.values() for s in per.values()]
    if not states:
        if market_open is False:
            return CheckResult("data_freshness", OK, WARN, "market closed, no observations")
        return CheckResult("data_freshness", UNKNOWN, WARN, "no observations")
    bad = sum(1 for s in states if s in BAD_OBSERVATIONS)
    frac = bad / len(states)
    if frac > max_bad_fraction:
        return CheckResult("data_freshness", ALARM, WARN,
                           "%d/%d observations stale or unusable (%.0f%%)"
                           % (bad, len(states), 100 * frac))
    return CheckResult("data_freshness", OK, WARN, "%d/%d observations usable"
                       % (len(states) - bad, len(states)))


def check_gateway(health: Optional[Mapping[str, Any]], now: datetime, *,
                  max_age: timedelta = timedelta(minutes=5)) -> CheckResult:
    """``health``: ``bot.gateway_health.assemble_health()`` output."""
    if not health:
        return CheckResult("gateway", UNKNOWN, CRITICAL, "no gateway health reading")
    t = _parse(health.get("as_of_utc"))
    if t is None or now - t > max_age:
        return CheckResult("gateway", UNKNOWN, CRITICAL, "gateway health reading stale")
    if t - now > MAX_CLOCK_LEAD:
        return CheckResult("gateway", UNKNOWN, CRITICAL,
                           "gateway health reading stamped ahead of the monitor clock")
    if health.get("expected_port") not in (None, PAPER_PORT):
        return CheckResult("gateway", ALARM, CRITICAL,
                           "gateway not in PAPER mode (expected port %s)"
                           % health.get("expected_port"))
    status = health.get("status")
    if status != GATEWAY_READY:
        return CheckResult("gateway", ALARM, CRITICAL, "gateway %s" % status)
    return CheckResult("gateway", OK, CRITICAL, "gateway API port open (paper)")


def check_unresolved_execution(orders: Iterable[Mapping[str, Any]], now: datetime, *,
                               pending_submit_max: timedelta = timedelta(minutes=30),
                               close_intent_max: timedelta = timedelta(minutes=30)
                               ) -> CheckResult:
    """``orders``: loop_orders rows. Any NEEDS_REVIEW is critical; an entry or
    a close whose broker outcome is still unknown past its limit alarms."""
    review, pending, closing = [], [], []
    for o in orders:
        st = o.get("state")
        upd = _parse(o.get("updated_at_utc")) or _parse(o.get("planned_at_utc"))
        if st == "NEEDS_REVIEW":
            review.append(o.get("client_ref"))
        elif st == "PENDING_SUBMIT" and upd is not None and now - upd > pending_submit_max:
            pending.append(o.get("client_ref"))
        if o.get("close_state") in ("INTENT", "SUBMITTED") and upd is not None \
                and now - upd > close_intent_max:
            closing.append(o.get("client_ref"))
    if review:
        return CheckResult("unresolved_execution", ALARM, CRITICAL,
                           "NEEDS_REVIEW: %s" % ",".join(map(str, review)))
    if pending or closing:
        return CheckResult("unresolved_execution", ALARM, CRITICAL,
                           "unknown broker outcome: pending_submit=%s close=%s"
                           % (",".join(map(str, pending)) or "-", ",".join(map(str, closing)) or "-"))
    return CheckResult("unresolved_execution", OK, CRITICAL, "no unresolved execution")


def check_duplicate_orders(orders: Iterable[Mapping[str, Any]],
                           broker_open_orders: Optional[Iterable[Mapping[str, Any]]] = None
                           ) -> CheckResult:
    active_states = ("PENDING_SUBMIT", "SUBMITTED", "OPEN", "NEEDS_REVIEW")
    per_symbol, refs, problems = {}, {}, []
    for o in orders:
        if o.get("state") in active_states:
            per_symbol.setdefault(o.get("symbol"), []).append(o.get("client_ref"))
        r = o.get("order_ref")
        if r:
            refs.setdefault(r, []).append(o.get("client_ref"))
    problems += ["symbol %s has %d active orders" % (s, len(v))
                 for s, v in sorted(per_symbol.items(), key=lambda x: str(x[0])) if len(v) > 1]
    problems += ["order_ref %s used by %s" % (r, v) for r, v in sorted(refs.items()) if len(v) > 1]
    if broker_open_orders is None:
        if problems:
            return CheckResult("duplicate_orders", ALARM, CRITICAL, "; ".join(problems))
        return CheckResult("duplicate_orders", UNKNOWN, CRITICAL, "broker open orders unavailable")
    seen = {}
    for b in broker_open_orders:
        ref = b.get("order_ref")
        if not ref:
            problems.append("unattributed broker order %s" % b.get("order_id"))
            continue
        key = (ref, b.get("leg"))
        seen[key] = seen.get(key, 0) + 1
    problems += ["broker has %d orders for %s" % (n, k) for k, n in sorted(seen.items(),
                                                                          key=str) if n > 1]
    if problems:
        return CheckResult("duplicate_orders", ALARM, CRITICAL, "; ".join(problems))
    return CheckResult("duplicate_orders", OK, CRITICAL, "no duplicate or unattributed orders")
