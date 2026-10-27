"""research.dataqa.quality — research-grade OHLCV quality audit (canonical B01/E05).

The M16 write-time gate (``bot.historical.quality.validate_batch``) supplies the
hard rules: NaN OHLC, inconsistent high/low, negative volume, non-positive
prices, non-UTC timestamps and duplicates. This module reuses it and adds the
checks research needs before data may be used for evidence:

* missing bars against an expected calendar (an exchange calendar, or the
  cross-sectional union of the panel's timestamps);
* series that end early (a possible delisting; information, not an error);
* suspected UNADJUSTED SPLITS: an overnight price jump that matches a split
  ratio (2, 3, 4, 5, 10 or their inverses within 3 %) while volume moves
  inversely;
* robust outlier returns (|log return| > k × MAD-scaled sigma);
* stale prices (an unchanged close for ≥ 5 bars) and zero-range bars;
* zero volume, and rows already carrying non-zero quality flags.

Each symbol gets a grade:
- ``unusable``: hard errors, or unsorted timestamps;
- ``usable_with_warnings``: suspected splits, missing bars above
  tolerance, outliers, stale runs, or ANY non-zero provider quality flag
  (second review, P1/P2), each reported per flag bit;
- ``clean``: none of the above.

Provider quality flags (M16 ``quality_flags`` bits) are split into WARNING
bits (zero volume, outlier warned, duplicate kept: the bar was observed) and
BLOCKING bits (backfilled gap: not an observed bar; and every bit this
module does not know, fail closed). ``admissible_mask`` / ``research_view``
remove blocking-flag rows and M16-hard-invalid rows from what research may
use; downstream lanes must consume bars through ``research_view``.

Knowledge timing. ``audit_symbol`` / ``audit_panel`` are RETROSPECTIVE: they
see the whole sample, and the split detector confirms a split with the
volume of the FOLLOWING bars. Their verdicts must never select a historical
universe. Point-in-time use goes through ``audit_symbol_asof`` (only bars up
to the as-of time) or ``causal_split_flags`` (bar i uses bars <= i only).

The audit only reads. It never repairs or adjusts data.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from bot.historical.quality import (QF_BACKFILLED_GAP, QF_DUPLICATE_KEPT, QF_OUTLIER_WARNED, QF_ZERO_VOLUME,
                                    validate_batch)

REQUIRED = ("ts_utc", "open", "high", "low", "close", "volume")
WARNING_FLAG_BITS = {QF_ZERO_VOLUME: "zero_volume", QF_OUTLIER_WARNED: "outlier_warned",
                     QF_DUPLICATE_KEPT: "duplicate_kept"}
BLOCKING_FLAG_BITS = {QF_BACKFILLED_GAP: "backfilled_gap"}        # plus every unknown bit
KNOWN_FLAG_MASK = sum(WARNING_FLAG_BITS) + sum(BLOCKING_FLAG_BITS)
SPLIT_RATIOS = (2.0, 3.0, 4.0, 5.0, 10.0)
GRADES = ("clean", "usable_with_warnings", "unusable")


@dataclass
class SymbolQuality:
    symbol: str
    rows: int
    first_ts: str
    last_ts: str
    grade: str
    hard_errors: Dict[str, int] = field(default_factory=dict)
    duplicates: int = 0
    unsorted: bool = False
    missing_bars: int = 0
    missing_share: float = 0.0
    missing_examples: List[str] = field(default_factory=list)
    ended_early: bool = False
    suspected_splits: List[Dict] = field(default_factory=list)
    outliers: int = 0
    stale_runs: int = 0
    zero_range_bars: int = 0
    zero_volume_bars: int = 0
    flagged_rows: int = 0
    warning_flags: Dict[str, int] = field(default_factory=dict)
    blocking_flags: Dict[str, int] = field(default_factory=dict)
    inadmissible_rows: int = 0
    knowledge: str = "retrospective_full_sample"     # or "as_of:<ts>"
    reasons: List[str] = field(default_factory=list)


def _split_candidates(df: pd.DataFrame, tol: float = 0.03) -> List[Dict]:
    c_prev = df["close"].shift(1).to_numpy(float)
    o = df["open"].to_numpy(float)
    v = df["volume"].to_numpy(float)
    vol_ref = pd.Series(v).shift(1).rolling(20, min_periods=5).median().to_numpy()
    vol_now = pd.Series(v).rolling(5, min_periods=1).median().shift(-4).to_numpy()   # forward 5-bar median: audit only
    out = []
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.log(o / c_prev)
    for i in np.flatnonzero(np.isfinite(lr) & (np.abs(lr) > np.log(1.5))):
        for k in SPLIT_RATIOS:
            for sgn in (-1, 1):
                if abs(lr[i] - sgn * np.log(k)) < tol:
                    vr = vol_now[i] / vol_ref[i] if np.isfinite(vol_ref[i]) and vol_ref[i] > 0 else np.nan
                    inverse_volume = bool(np.isfinite(vr) and (vr > np.sqrt(k) if sgn < 0 else vr < 1 / np.sqrt(k)))
                    out.append({"ts_utc": str(df["ts_utc"].iloc[i]), "ratio": k if sgn < 0 else 1.0 / k,
                                "log_jump": float(lr[i]), "volume_ratio": float(vr) if np.isfinite(vr) else None,
                                "volume_moves_inversely": inverse_volume})
    return out


def audit_symbol(df: pd.DataFrame, symbol: str, *, timeframe: str = "1D",
                 expected_index: Optional[pd.DatetimeIndex] = None, panel_end: Optional[pd.Timestamp] = None,
                 missing_tolerance: float = 0.005, outlier_k: float = 10.0, stale_len: int = 5) -> SymbolQuality:
    miss = [c for c in REQUIRED if c not in df.columns]
    if miss:
        return SymbolQuality(symbol, len(df), "", "", "unusable", reasons=["missing columns %s" % miss])
    ts = pd.to_datetime(df["ts_utc"], utc=True)
    q = SymbolQuality(symbol, len(df), str(ts.min()) if len(ts) else "", str(ts.max()) if len(ts) else "", "clean")
    q.unsorted = bool(not ts.is_monotonic_increasing)
    q.duplicates = int(ts.duplicated().sum())
    vb = validate_batch(df[list(REQUIRED) + ([c for c in ("quality_flags",) if c in df.columns])].copy(),
                        symbol=symbol, timeframe=timeframe, provider="research_audit")
    for e in vb.events:
        if e.severity == "error":
            q.hard_errors[e.kind] = q.hard_errors.get(e.kind, 0) + int(e.details.get("count", 1) or 1)
    d = df.assign(ts_utc=ts).drop_duplicates("ts_utc", keep="last").sort_values("ts_utc").reset_index(drop=True)
    if expected_index is not None and len(d):
        exp = expected_index[(expected_index >= d["ts_utc"].iloc[0]) & (expected_index <= d["ts_utc"].iloc[-1])]
        missing = exp.difference(pd.DatetimeIndex(d["ts_utc"]))
        q.missing_bars = int(len(missing))
        q.missing_share = float(len(missing) / max(len(exp), 1))
        q.missing_examples = [str(x) for x in missing[:5]]
    if panel_end is not None and len(d):
        q.ended_early = bool(d["ts_utc"].iloc[-1] < panel_end)
    q.suspected_splits = _split_candidates(d)
    c = d["close"].to_numpy(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.diff(np.log(c))
    lr = lr[np.isfinite(lr)]
    if len(lr) > 20:
        mad = np.median(np.abs(lr - np.median(lr))) * 1.4826
        if mad > 0:
            q.outliers = int((np.abs(lr - np.median(lr)) > outlier_k * mad).sum())
    same = np.r_[False, np.diff(c) == 0]
    runs = 0
    k = 0
    for s in same:
        k = k + 1 if s else 0
        if k == stale_len - 1:
            runs += 1
    q.stale_runs = runs
    q.zero_range_bars = int((d["high"] == d["low"]).sum())
    q.zero_volume_bars = int((d["volume"] == 0).sum())
    if "quality_flags" in d.columns:
        qf = d["quality_flags"].fillna(0).astype(np.int64).to_numpy()
        q.flagged_rows = int((qf != 0).sum())
        q.warning_flags, q.blocking_flags = flag_breakdown(qf)
    q.inadmissible_rows = int((~admissible_mask(df)).sum())
    hard = sum(q.hard_errors.values())
    if hard or q.unsorted:
        q.grade = "unusable"
        q.reasons += ["%d hard M16 errors" % hard] if hard else []
        q.reasons += ["timestamps not sorted"] if q.unsorted else []
    else:
        warn = []
        if q.suspected_splits:
            warn.append("%d suspected unadjusted split(s)" % len(q.suspected_splits))
        if q.missing_share > missing_tolerance:
            warn.append("missing %.2f %% of expected bars" % (100 * q.missing_share))
        if q.outliers:
            warn.append("%d robust outlier return(s)" % q.outliers)
        if q.stale_runs:
            warn.append("%d stale-price run(s)" % q.stale_runs)
        if q.duplicates:
            warn.append("%d duplicate timestamp(s)" % q.duplicates)
        for name, n in sorted(q.warning_flags.items()):
            warn.append("%d bar(s) flagged %s (warning bit)" % (n, name))
        for name, n in sorted(q.blocking_flags.items()):
            warn.append("%d bar(s) flagged %s (BLOCKING bit: excluded by research_view)" % (n, name))
        if warn:
            q.grade, q.reasons = "usable_with_warnings", warn
    if q.ended_early:
        q.reasons.append("series ends before the panel (possible delisting)")
    return q


def flag_breakdown(flags) -> "tuple[Dict[str, int], Dict[str, int]]":
    """Row counts per warning bit and per blocking bit (unknown bits are blocking: fail closed)."""
    qf = np.asarray(flags, dtype=np.int64)
    warn = {name: int(((qf & bit) != 0).sum()) for bit, name in WARNING_FLAG_BITS.items() if ((qf & bit) != 0).any()}
    block = {name: int(((qf & bit) != 0).sum()) for bit, name in BLOCKING_FLAG_BITS.items()
             if ((qf & bit) != 0).any()}
    unknown = (qf & ~np.int64(KNOWN_FLAG_MASK)) != 0
    if unknown.any():
        block["unknown_bits"] = int(unknown.sum())
    return warn, block


def admissible_mask(df: pd.DataFrame) -> np.ndarray:
    """True where a bar may enter research: finite positive OHLC, consistent high/low, FINITE non-negative
    volume (the M16 hard rules) and no blocking or unknown quality-flag bit.

    Volume must be finite and >= 0 (third review, PR #8): a missing (NaN), infinite or non-numeric volume
    (coerced to NaN) is not an observation. Zero volume is admissible for descriptive research and carries a
    WARNING bit only; it means no trade printed in the bar, so it is no evidence that a simulated fill at that
    bar was executable. The research engines do not yet model liquidity; that limitation is stated, not hidden."""
    o, h, l, c = (pd.to_numeric(df[k], errors="coerce").to_numpy(float) for k in ("open", "high", "low", "close"))
    v = pd.to_numeric(df["volume"], errors="coerce").to_numpy(float)
    with np.errstate(invalid="ignore"):
        ok = (np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c) & (o > 0) & (h > 0) & (l > 0)
              & (c > 0) & (h >= np.maximum(o, c)) & (l <= np.minimum(o, c)) & np.isfinite(v) & (v >= 0))
    if "quality_flags" in df.columns:
        qf = df["quality_flags"].fillna(0).astype(np.int64).to_numpy()
        blocking = sum(BLOCKING_FLAG_BITS)
        ok &= ((qf & np.int64(blocking)) == 0) & ((qf & ~np.int64(KNOWN_FLAG_MASK)) == 0)
    return ok


def research_view(df: pd.DataFrame) -> "tuple[pd.DataFrame, Dict[str, int]]":
    """The bars research may use: inadmissible rows removed (they become missing observations, which every
    downstream lane already treats as gaps). Returns (view, counts)."""
    m = admissible_mask(df)
    return df.loc[m].reset_index(drop=True), {"rows": int(len(df)), "excluded": int((~m).sum())}


def audit_symbol_asof(df: pd.DataFrame, symbol: str, as_of_utc, **kw) -> SymbolQuality:
    """Point-in-time audit: only bars whose close is at or before ``as_of_utc`` are visible."""
    t = pd.Timestamp(as_of_utc)
    visible = df.loc[pd.to_datetime(df["ts_utc"], utc=True) <= t].reset_index(drop=True)
    q = audit_symbol(visible, symbol, **kw)
    q.knowledge = "as_of:%s" % t.isoformat()
    return q


def causal_split_flags(df: pd.DataFrame, tol: float = 0.03) -> np.ndarray:
    """Bar i is flagged when its overnight jump (open_i / close_{i-1}) matches a split ratio AND its own volume
    moves inversely against the median of the 20 PRIOR bars. Uses bars <= i only, so the flag is knowable at
    bar i's close and can drive point-in-time eligibility."""
    c_prev = df["close"].astype(float).shift(1).to_numpy()
    o = df["open"].astype(float).to_numpy()
    v = df["volume"].astype(float).to_numpy()
    ref = pd.Series(v).shift(1).rolling(20, min_periods=5).median().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.log(o / c_prev)
        vr = v / ref
    out = np.zeros(len(df), dtype=bool)
    for i in np.flatnonzero(np.isfinite(lr) & (np.abs(lr) > np.log(1.5))):
        for k in SPLIT_RATIOS:
            if abs(lr[i] + np.log(k)) < tol and np.isfinite(vr[i]) and vr[i] > np.sqrt(k):
                out[i] = True
            elif abs(lr[i] - np.log(k)) < tol and np.isfinite(vr[i]) and vr[i] < 1 / np.sqrt(k):
                out[i] = True
    return out


def audit_panel(bars: Mapping[str, pd.DataFrame], *, timeframe: str = "1D",
                calendar: Optional[pd.DatetimeIndex] = None, **kw) -> Dict:
    """Audit every symbol. Without a calendar the expected index is the cross-sectional union."""
    idx = [pd.to_datetime(b["ts_utc"], utc=True) for b in bars.values() if "ts_utc" in b.columns and len(b)]
    union = pd.DatetimeIndex(sorted(set().union(*[set(x) for x in idx]))) if idx else pd.DatetimeIndex([])
    exp = calendar if calendar is not None else union
    end = union.max() if len(union) else None
    per = {s: audit_symbol(b, s, timeframe=timeframe, expected_index=exp, panel_end=end, **kw)
           for s, b in sorted(bars.items())}
    grades = {g: sum(q.grade == g for q in per.values()) for g in GRADES}
    return {"symbols": len(per), "grades": grades, "knowledge": "retrospective_full_sample",
            "calendar": "provided" if calendar is not None else "panel_union",
            "per_symbol": {s: asdict(q) for s, q in per.items()}}


__all__ = ["REQUIRED", "SPLIT_RATIOS", "GRADES", "WARNING_FLAG_BITS", "BLOCKING_FLAG_BITS", "SymbolQuality",
           "flag_breakdown", "admissible_mask", "research_view", "audit_symbol", "audit_symbol_asof",
           "causal_split_flags", "audit_panel"]
