"""M21 WP14 — shadow comparison before eligibility, and the auto-revert guard after a change.

``compare_shadow`` judges a proposal from LIVE paper signals: for each signal
the R that the CURRENT configuration produced (or would have) and the R the
PROPOSED configuration would have produced on the same signal, labelled from
bars by the shadow runner. It places no orders. Fewer than the tier's minimum
signals is INCONCLUSIVE, never eligible.

``check_decay`` watches realised paper outcomes after a change is applied and
decides whether to revert. A revert changes the CONFIGURATION only:
``RevertDecision.cancel_orders`` is always False. Open positions keep their
protective stop and target orders; nothing here can reach a broker.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple

import numpy as np

ELIGIBLE, INELIGIBLE, INCONCLUSIVE = "ELIGIBLE", "INELIGIBLE", "INCONCLUSIVE"


@dataclass(frozen=True)
class ShadowObservation:
    signal_id: str
    current_r: Optional[float]           # None: the configuration took no trade on this signal
    proposed_r: Optional[float]


@dataclass(frozen=True)
class ShadowVerdict:
    status: str
    n_signals: int
    mean_current_r: float
    mean_proposed_r: float
    loss_rate_current: float
    loss_rate_proposed: float
    loss_rate_not_higher: bool
    reasons: Tuple[str, ...]


def _mean(x):
    return float(np.mean(x)) if len(x) else 0.0


def compare_shadow(obs: Sequence[ShadowObservation], *, min_signals: int, tolerance_r: float = 0.05
                   ) -> ShadowVerdict:
    ids = [o.signal_id for o in obs]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate signal ids in the shadow record")
    cur = [o.current_r if o.current_r is not None else 0.0 for o in obs]
    pro = [o.proposed_r if o.proposed_r is not None else 0.0 for o in obs]
    bad = [o.signal_id for o in obs for v in (o.current_r, o.proposed_r) if v is not None and not math.isfinite(v)]
    if bad:
        raise ValueError("non-finite shadow R for %s" % bad[:3])
    lc = float(np.mean([r < 0 for r in cur])) if cur else 0.0
    lp = float(np.mean([r < 0 for r in pro])) if pro else 0.0
    mc, mp = _mean(cur), _mean(pro)
    not_higher = lp <= lc
    if len(obs) < min_signals:
        return ShadowVerdict(INCONCLUSIVE, len(obs), mc, mp, lc, lp, not_higher,
                             ("only_%d_live_signals_(min_%d)" % (len(obs), min_signals),))
    reasons = []
    if mp < mc - tolerance_r:
        reasons.append("proposed_mean_r_%.3f_below_current_%.3f" % (mp, mc))
    if not not_higher:
        reasons.append("loss_rate_rises_%.2f_to_%.2f" % (lc, lp))
    return ShadowVerdict(INELIGIBLE if reasons else ELIGIBLE, len(obs), mc, mp, lc, lp, not_higher, tuple(reasons))


@dataclass(frozen=True)
class RevertDecision:
    revert: bool
    reasons: Tuple[str, ...]
    n_trades: int
    mean_r: float
    drawdown_r: float
    cancel_orders: bool = field(default=False, init=False)   # a revert NEVER cancels protective orders


def check_decay(post_r: Sequence[float], *, baseline_mean_r: float, min_trades: int = 10, k_se: float = 2.0,
                max_drawdown_r: float = 6.0) -> RevertDecision:
    x = np.asarray([float(v) for v in post_r], dtype=float)
    if not np.isfinite(x).all():
        raise ValueError("non-finite outcome R")
    cum = np.cumsum(x) if len(x) else np.array([0.0])
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0.0], cum])) - np.concatenate([[0.0], cum])))
    reasons = []
    if dd >= max_drawdown_r:
        reasons.append("drawdown_%.1fR_at_or_above_%.1fR" % (dd, max_drawdown_r))
    m = float(x.mean()) if len(x) else 0.0
    if len(x) >= min_trades:
        se = float(x.std(ddof=1) / math.sqrt(len(x))) if len(x) > 1 else 0.0
        if m < baseline_mean_r - k_se * se:
            reasons.append("mean_r_%.3f_below_baseline_%.3f_minus_%gse" % (m, baseline_mean_r, k_se))
    return RevertDecision(bool(reasons), tuple(reasons), int(len(x)), m, dd)


__all__ = ["ELIGIBLE", "INELIGIBLE", "INCONCLUSIVE", "ShadowObservation", "ShadowVerdict", "compare_shadow",
           "RevertDecision", "check_decay"]
