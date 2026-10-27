"""research.evidence.replay — candidate streams with decision stages.

``scanner_stage_candidates`` replays the live scanner's multi-timeframe
confluence rule through the M17 reference implementation
(``ScannerReplicaStrategy`` scoring helpers + ``MultiTimeframeContext``), and
records EVERY decision stage per anchor and direction, not only fires:

* eligible   long/short count >= min_valid (the scanner's final_signal)
* near_miss  count == min_valid - 1 (the scanner's partial_confluence)
* rejected   anything else, sampled at ``rejected_rate`` with an
             inverse-probability weight in ``features['sample_weight']``

``apply_gates`` then runs an explicit, auditable gate layer over eligible
candidates (ILLUSTRATIVE rules for research; the production gates live in
M19 / M14 and are not changed here) and records reason codes, so gate
economics can be measured. Symbol capacity uses labelled exits in bar time.
Counts produced by this replay (e.g. "executed") are scenario-dependent
illustrations, not a model of the production paper loop.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Union

import numpy as np
import pandas as pd

from bot.backtesting.mtf_context import MultiTimeframeContext
from bot.backtesting.strategy import ScannerReplicaStrategy
from research.evidence.candidates import Bracket, CandidateEvent, make_candidate_id

_TF_PREF = ("1D", "4H", "1H", "15m")


def scanner_stage_candidates(per_tf_bars: Dict[str, pd.DataFrame], symbol: str, *, rng: np.random.Generator,
                             params: Optional[dict] = None, bracket: Bracket = Bracket(),
                             strategy_id: str = "scanner_replica_v1", sector: str = "",
                             rejected_rate: float = 0.02, source: str = "historical_replay",
                             include_short: bool = True) -> List[CandidateEvent]:
    p = dict(ScannerReplicaStrategy.default_params)
    if params:
        p.update(params)
    strat = ScannerReplicaStrategy(p)
    ctx = MultiTimeframeContext(per_tf_bars, anchor_tf=p["anchor_tf"])
    pre = strat._precompute_indicators(ctx)
    cfg_min = int(p["confluence"]["min_valid_tfs"])
    total = len(p["timeframes"])
    anchor = per_tf_bars[p["anchor_tf"]]
    ts_all = pd.to_datetime(anchor["ts_utc"], utc=True)
    out: List[CandidateEvent] = []
    keys = ("rsi", "macd_hist", "ema20", "ema50", "vwap_dev", "vol_ratio", "atr", "price")
    for i in range(len(anchor)):
        a_ts = ts_all.iloc[i]
        snap = ctx.snapshot_at(a_ts)
        rows: Dict[str, Dict[str, float]] = {}
        for tf, sb in snap.items():
            if sb is None:
                continue
            row = {k: float(pre[tf][k].iloc[sb.idx]) for k in keys}
            if all(np.isfinite(v) for v in row.values()):
                rows[tf] = row
        if not rows:
            continue
        min_valid = strat.confluence_min_valid(len(rows), total, cfg_min)
        best = next(tf for tf in _TF_PREF if tf in rows)
        anchor_row = rows.get(p["anchor_tf"], rows[best])
        sides = [("long", sum(strat._score_timeframe_long(r, p["long"]) for r in rows.values()))]
        if include_short:
            sides.append(("short", sum(strat._score_timeframe_short(r, p["short"]) for r in rows.values())))
        for direction, cnt in sides:
            if cnt >= min_valid:
                stage, w = "eligible", 1.0
            elif cnt == min_valid - 1:
                stage, w = "near_miss", 1.0
            else:
                if rng.random() >= rejected_rate:
                    continue
                stage, w = "rejected", 1.0 / rejected_rate
            out.append(CandidateEvent(
                candidate_id=make_candidate_id(symbol, a_ts, direction, strategy_id), symbol=symbol,
                decision_ts_utc=a_ts, direction=direction, stage=stage, source=source, timeframe=p["anchor_tf"],
                strategy_id=strategy_id, atr_at_decision=rows[best]["atr"], bracket=bracket,
                reasons=() if stage == "eligible" else ("confluence_%d_of_%d" % (cnt, min_valid),),
                sector=sector,
                features={"valid_count": int(cnt), "min_valid": int(min_valid), "available_tfs": len(rows),
                          "atr_tf": best, "rsi": anchor_row["rsi"], "macd_hist": anchor_row["macd_hist"],
                          "vwap_dev": anchor_row["vwap_dev"], "vol_ratio": anchor_row["vol_ratio"],
                          "sample_weight": w}))
    return out


GateRule = Callable[[CandidateEvent], Optional[str]]


NO_POSITION = "no_position"      # release-map value: the candidate could never hold a position (unfillable)


def position_release_times(labels: pd.DataFrame) -> Dict[str, Union[None, str, pd.Timestamp]]:
    """When a hypothetically opened position would free its symbol slot, from
    LABELLED exits (bar time, not wall-clock): the exit bar's close for
    resolved, ambiguous or data-flagged windows; ``None`` (= never released
    within the data) for pending or series-ended windows; ``NO_POSITION`` for
    unfillable ones (they never held a position). Every labelled candidate
    gets an entry, so a missing entry always means missing information."""
    out: Dict[str, Union[None, str, pd.Timestamp]] = {}
    for r in labels.itertuples(index=False):
        if r.status == "unfillable":
            out[r.candidate_id] = NO_POSITION
            continue
        ex = r.exit_ts_utc
        out[r.candidate_id] = None if ex is None or pd.isna(ex) else pd.Timestamp(ex)
    return out


def apply_gates(events: Iterable[CandidateEvent], rules: Dict[str, GateRule], *,
                max_entries_per_day: Optional[int] = None, one_open_per_symbol: bool = True,
                release_times: Optional[Dict[str, Optional[pd.Timestamp]]] = None) -> List[CandidateEvent]:
    """Run rules over ELIGIBLE candidates in decision order.

    Rule failures -> blocked_gate (reason = rule name); an open position on the
    same symbol -> blocked_risk ('symbol_exposure'); exceeding the daily cap
    -> skipped_capacity ('daily_entry_cap'); a candidate whose release is
    ``NO_POSITION`` (unfillable) -> unfillable, using no slot and no daily
    capacity; otherwise -> executed (meaning
    "would have been sent to the paper loop" in this replay). Non-eligible
    candidates pass through unchanged.

    Capacity is bar-time (PR #6 review, P2): a position occupies its symbol
    until its LABELLED exit (``position_release_times``), across nights and
    weekends, never for a wall-clock duration. With one_open_per_symbol the
    release map is required AND must cover every candidate accepted for
    execution (second review, P1): a missing entry is unknown capacity, never
    free capacity, so it raises instead of silently re-opening the slot.
    ``executed`` here means "would have been sent"; it is a counterfactual
    replay stage, never a broker execution.
    """
    if one_open_per_symbol and release_times is None:
        raise ValueError("one_open_per_symbol needs labelled release_times (bar-time capacity)")
    evs = sorted(events, key=lambda e: (e.decision_ts_utc, e.symbol, e.direction))
    open_until: Dict[str, Optional[pd.Timestamp]] = {}
    per_day: Dict[pd.Timestamp, int] = {}
    out: List[CandidateEvent] = []
    for e in evs:
        if e.stage != "eligible":
            out.append(e)
            continue
        fails = [name for name, rule in rules.items() if rule(e)]
        if fails:
            out.append(replace(e, stage="blocked_gate", reasons=tuple(fails)))
            continue
        if one_open_per_symbol and e.symbol in open_until and (
                open_until[e.symbol] is None or e.decision_ts_utc < open_until[e.symbol]):
            out.append(replace(e, stage="blocked_risk", reasons=("symbol_exposure",)))
            continue
        if one_open_per_symbol and isinstance(release_times.get(e.candidate_id), str) \
                and release_times[e.candidate_id] == NO_POSITION:
            # Third review (PR #6): an unfillable candidate could never hold a position. It is recorded as
            # unfillable, never as executed, and it consumes no daily entry capacity.
            out.append(replace(e, stage="unfillable", reasons=("no_position_possible",)))
            continue
        day = e.decision_ts_utc.floor("D")
        if max_entries_per_day is not None and per_day.get(day, 0) >= max_entries_per_day:
            out.append(replace(e, stage="skipped_capacity", reasons=("daily_entry_cap",)))
            continue
        if one_open_per_symbol:
            if e.candidate_id not in release_times:
                raise ValueError("release map has no entry for accepted candidate %s (%s): capacity is unknown, "
                                 "refusing to treat the slot as free" % (e.candidate_id, e.symbol))
            open_until[e.symbol] = release_times[e.candidate_id]
        per_day[day] = per_day.get(day, 0) + 1
        out.append(replace(e, stage="executed"))
    return out


__all__ = ["NO_POSITION", "scanner_stage_candidates", "position_release_times", "apply_gates", "GateRule"]
