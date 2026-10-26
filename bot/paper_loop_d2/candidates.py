"""M21.1extra-D2.3 — trade candidates for the paper learning loop.

A candidate carries everything an entry needs: symbol, direction, score,
strategy id (for per-strategy outcome attribution), reference entry price and
the bracket's stop-loss / take-profit (the exits that close a trade and
produce a win or a loss). Validation is strict and fail-closed: a malformed
candidate is dropped and reported, never "repaired".
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Mapping, Optional, Sequence, Tuple


class InvalidCandidate(ValueError):
    pass


def _f(v: Any, name: str) -> float:
    if isinstance(v, bool):
        raise InvalidCandidate("%s is not a number" % name)
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise InvalidCandidate("%s is not a number" % name)
    if not math.isfinite(x) or x <= 0:
        raise InvalidCandidate("%s must be finite and > 0" % name)
    return x


@dataclass(frozen=True)
class LoopCandidate:
    symbol: str
    direction: str            # 'long' | 'short'
    score: float              # 0..100 (M19 final_score_100)
    strategy_id: str
    entry_ref_price: float
    stop_loss: float
    take_profit: float
    signal_ts_utc: Optional[str] = None

    @staticmethod
    def build(**kw: Any) -> "LoopCandidate":
        sym = kw.get("symbol")
        if not isinstance(sym, str) or not sym.strip() or sym != sym.strip().upper():
            raise InvalidCandidate("symbol must be a non-empty upper-case string")
        direction = kw.get("direction")
        if direction not in ("long", "short"):
            raise InvalidCandidate("direction must be 'long' or 'short'")
        strategy = kw.get("strategy_id")
        if not isinstance(strategy, str) or not strategy.strip():
            raise InvalidCandidate("strategy_id required (per-strategy attribution)")
        score = kw.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise InvalidCandidate("score must be a finite number")
        e = _f(kw.get("entry_ref_price"), "entry_ref_price")
        sl = _f(kw.get("stop_loss"), "stop_loss")
        tp = _f(kw.get("take_profit"), "take_profit")
        if direction == "long" and not (sl < e < tp):
            raise InvalidCandidate("long requires stop_loss < entry < take_profit")
        if direction == "short" and not (tp < e < sl):
            raise InvalidCandidate("short requires take_profit < entry < stop_loss")
        return LoopCandidate(symbol=sym, direction=direction, score=float(score),
                             strategy_id=strategy.strip(), entry_ref_price=e, stop_loss=sl,
                             take_profit=tp, signal_ts_utc=kw.get("signal_ts_utc"))


def _signal_age_minutes(c: "LoopCandidate", now: datetime) -> Optional[float]:
    if not c.signal_ts_utc:
        return None
    try:
        ts = datetime.fromisoformat(c.signal_ts_utc)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        return None
    return (now - ts).total_seconds() / 60.0


def select_candidate(items: Sequence[Any], *, min_score: float, allow_short: bool,
                     excluded_symbols: set, now: Optional[datetime] = None,
                     max_signal_age_minutes: Optional[float] = None
                     ) -> Tuple[Optional[LoopCandidate], List[str]]:
    """Highest score >= min_score (ties: symbol, then direction). Returns the
    pick and a list of rejection notes. Malformed items are rejected, not fatal.
    With ``max_signal_age_minutes`` set, a candidate whose signal timestamp is
    missing, naive, in the future or older than the limit is rejected — stale
    entry/stop/target prices must never reach the broker."""
    notes: List[str] = []
    valid: List[LoopCandidate] = []
    for it in items:
        try:
            c = it if isinstance(it, LoopCandidate) else LoopCandidate.build(**dict(it))
        except (InvalidCandidate, TypeError, ValueError) as exc:
            notes.append("invalid_candidate:%s" % exc)
            continue
        if max_signal_age_minutes is not None:
            age = _signal_age_minutes(c, now)
            if age is None:
                notes.append("signal_ts_unusable:%s" % c.symbol)
                continue
            if age < -1.0 or age > max_signal_age_minutes:
                notes.append("stale_signal:%s" % c.symbol)
                continue
        if c.direction == "short" and not allow_short:
            notes.append("short_disabled:%s" % c.symbol)
            continue
        if c.symbol in excluded_symbols:
            notes.append("symbol_busy:%s" % c.symbol)
            continue
        if c.score < min_score:
            continue
        valid.append(c)
    if not valid:
        return None, notes
    valid.sort(key=lambda c: (-c.score, c.symbol, c.direction))
    return valid[0], notes


def candidate_from_scored_signal(signal: Mapping[str, Any], scored_row: Mapping[str, Any],
                                 *, strategy_prefix: str = "scanner") -> LoopCandidate:
    """Map a scanner signal + its M19 score row to a candidate. The strategy id
    is the scanner strategy version (the only strategy identity the scanner has
    today) — honest attribution, not a template claim."""
    ver = signal.get("strategy_version")
    return LoopCandidate.build(
        symbol=signal.get("symbol"), direction=signal.get("direction"),
        score=scored_row.get("final_score_100"),
        strategy_id="%s_v%s" % (strategy_prefix, ver if ver is not None else "unknown"),
        entry_ref_price=signal.get("entry_price") or signal.get("price"),
        stop_loss=signal.get("stop_loss"), take_profit=signal.get("target_price"),
        signal_ts_utc=signal.get("timestamp"))
