"""research.evidence.snapshots — read-only adapter for flywheel candidate_snapshots.

Maps rows of the production ``candidate_snapshots`` table (schema in
``bot/flywheel.py``: cycle_id, timestamp, symbol, direction, stage,
valid_count, tfs_passing, available_tfs, min_valid, rejection_reason, route,
strategy_version, signal_id, rsi, macd_hist, atr, bb_pos, vwap_dev,
vol_ratio) onto ``CandidateEvent``s so live decisions — including rejected
and partial-confluence candidates — can be labelled.

This module never opens a database. The caller passes a DataFrame obtained
by a separately APPROVED read-only export (canonical A02/B01; no private
VPS export is authorised by this lane). It is tested on a fixture with the
production column set.
"""
from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd

from research.evidence.candidates import Bracket, CandidateEvent, make_candidate_id

STAGE_MAP = {"final_signal": "eligible", "partial_confluence": "near_miss",
             "rejected": "rejected", "scanned": "scanned"}
STAGE_RANK = {"scanned": 0, "rejected": 1, "near_miss": 2, "eligible": 3}
REQUIRED = ("cycle_id", "timestamp", "symbol", "direction", "stage", "valid_count", "available_tfs",
            "min_valid", "rejection_reason", "strategy_version", "atr")


def from_candidate_snapshots(df: pd.DataFrame, *, timeframe: str = "15m", bracket: Bracket = Bracket(),
                             sector_by_symbol: dict = None) -> List[CandidateEvent]:
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError("candidate_snapshots export lacks columns %s" % missing)
    d = df.copy()
    d["stage_n"] = d["stage"].map(STAGE_MAP)
    unknown = d.loc[d["stage_n"].isna(), "stage"].unique()
    if len(unknown):
        raise ValueError("unknown candidate_snapshots stage(s): %s" % list(unknown))
    d["direction_n"] = d["direction"].astype(str).str.lower()
    bad_dir = d.loc[~d["direction_n"].isin(["long", "short"]), "direction"].unique()
    if len(bad_dir):
        raise ValueError("unknown direction(s): %s" % list(bad_dir))
    d["ts"] = pd.to_datetime(d["timestamp"], utc=True)
    d["rank"] = d["stage_n"].map(STAGE_RANK)
    # One candidate per (cycle, symbol, direction): keep the most advanced stage.
    d = d.sort_values(["cycle_id", "symbol", "direction_n", "rank"]).groupby(
        ["cycle_id", "symbol", "direction_n"], as_index=False).tail(1)
    out: List[CandidateEvent] = []
    for r in d.itertuples(index=False):
        sid = "scanner_v%s" % int(r.strategy_version)
        reasons = tuple(x for x in str(r.rejection_reason or "").split(",") if x.strip())
        feats = {k: (None if pd.isna(getattr(r, k, np.nan)) else getattr(r, k))
                 for k in ("rsi", "macd_hist", "bb_pos", "vwap_dev", "vol_ratio")
                 if hasattr(r, k)}
        feats.update({"valid_count": int(r.valid_count), "available_tfs": int(r.available_tfs),
                      "min_valid": int(r.min_valid), "cycle_id": int(r.cycle_id),
                      "snapshot_id": int(getattr(r, "id", -1)) if hasattr(r, "id") else -1,
                      "signal_id": None if pd.isna(getattr(r, "signal_id", np.nan)) else int(r.signal_id),
                      "tfs_passing": str(getattr(r, "tfs_passing", "") or ""),
                      "route": str(getattr(r, "route", "") or "")})
        atr = float(r.atr) if r.atr is not None and not pd.isna(r.atr) else float("nan")
        out.append(CandidateEvent(
            candidate_id=make_candidate_id(r.symbol, r.ts, r.direction_n, sid), symbol=str(r.symbol),
            decision_ts_utc=r.ts, direction=r.direction_n, stage=r.stage_n, source="scanner_live",
            timeframe=timeframe, strategy_id=sid, atr_at_decision=atr, bracket=bracket,
            reasons=reasons, sector=(sector_by_symbol or {}).get(r.symbol, ""), features=feats))
    return out


__all__ = ["STAGE_MAP", "REQUIRED", "from_candidate_snapshots"]
