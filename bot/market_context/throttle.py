"""M21 WP15 — the shadow market-context throttle. It can only REDUCE risk.

``decide`` turns point-in-time features into a size multiplier in [0, 1] and
an ``abstain`` flag. Declared policy (reduce-only; the strictest rule wins):

  source outage   a REQUIRED feature (vix_level, spy_trend) is UNAVAILABLE
                  or STALE                                    -> abstain
  event window    within 2 h before to 1 h after FOMC / CPI / NFP -> abstain
  event calendar  unavailable                                 -> x 0.75
  VIX regime      high -> x 0.75; extreme -> x 0.5
  weak market     spy_trend "down" AND breadth < 0.30         -> x 0.75
  bearish news    news_risk below -0.5                        -> x 0.75

``can_grant_authority`` is always False. ``apply_to`` never turns a blocked
decision into an allowed one and never increases size. In this milestone the
decision is SHADOW only: it is logged, not enforced. Enforcement is a later,
approved change, evaluated walk-forward first (WP16).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Dict, Mapping, Tuple

from bot.market_context.features import OK, UNAVAILABLE, FeatureValue

REQUIRED = ("vix_level", "spy_trend")
MULT_VIX = {"high": 0.75, "extreme": 0.5}
MULT_NO_CALENDAR = 0.75
MULT_WEAK_MARKET = 0.75
MULT_BEARISH_NEWS = 0.75
WEAK_BREADTH = 0.30
BEARISH_NEWS = -0.5


@dataclass(frozen=True)
class ContextDecision:
    multiplier: float
    abstain: bool
    reasons: Tuple[str, ...]
    features: Dict[str, Tuple]
    can_grant_authority: bool = field(default=False, init=False)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, default=str)


def decide(ctx: Mapping[str, FeatureValue]) -> ContextDecision:
    mult, abstain, reasons = 1.0, False, []
    for r in REQUIRED:
        f = ctx.get(r)
        if f is None or f.status != OK:
            abstain = True
            reasons.append("source_outage:%s:%s" % (r, f.status if f else "missing"))
    ew = ctx.get("event_window")
    if ew is None or ew.status == UNAVAILABLE:
        mult = min(mult, MULT_NO_CALENDAR)
        reasons.append("event_calendar_unavailable")
    elif ew.value:
        abstain = True
        reasons.append("event_window:%s" % ",".join(ew.value))
    vr = ctx.get("vix_regime")
    if vr is not None and vr.status == OK and vr.value in MULT_VIX:
        mult = min(mult, MULT_VIX[vr.value])
        reasons.append("vix_%s" % vr.value)
    spy, br = ctx.get("spy_trend"), ctx.get("breadth_above_sma50")
    if spy is not None and spy.status == OK and spy.value == "down" and br is not None and br.status == OK \
            and br.value < WEAK_BREADTH:
        mult = min(mult, MULT_WEAK_MARKET)
        reasons.append("weak_market")
    nr = ctx.get("news_risk")
    if nr is not None and nr.status == OK and nr.value < BEARISH_NEWS:
        mult = min(mult, MULT_BEARISH_NEWS)
        reasons.append("bearish_news")
    mult = max(0.0, min(1.0, mult))
    feats = {k: (v.value, v.status, v.as_of) for k, v in sorted(ctx.items())}
    return ContextDecision(0.0 if abstain else mult, abstain, tuple(reasons), feats)


def apply_to(decision: ContextDecision, *, allowed: bool, size: float) -> Tuple[bool, float]:
    """What enforcement WOULD do: never unblocks, never enlarges."""
    if not allowed or decision.abstain:
        return False, 0.0
    return True, min(size, size * decision.multiplier)


__all__ = ["REQUIRED", "MULT_VIX", "MULT_NO_CALENDAR", "MULT_WEAK_MARKET", "MULT_BEARISH_NEWS", "WEAK_BREADTH",
           "BEARISH_NEWS", "ContextDecision", "decide", "apply_to"]
