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
  tolerance, outliers or stale runs;
- ``clean``: none of the above.

The audit only reads. It never repairs or adjusts data.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from bot.historical.quality import validate_batch

REQUIRED = ("ts_utc", "open", "high", "low", "close", "volume")
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
        q.flagged_rows = int((d["quality_flags"].fillna(0).astype(np.int64) != 0).sum())
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
        if warn:
            q.grade, q.reasons = "usable_with_warnings", warn
    if q.ended_early:
        q.reasons.append("series ends before the panel (possible delisting)")
    return q


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
    return {"symbols": len(per), "grades": grades, "calendar": "provided" if calendar is not None else "panel_union",
            "per_symbol": {s: asdict(q) for s, q in per.items()}}


__all__ = ["REQUIRED", "SPLIT_RATIOS", "GRADES", "SymbolQuality", "audit_symbol", "audit_panel"]
