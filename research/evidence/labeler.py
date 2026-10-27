"""research.evidence.labeler — counterfactual PATH labels with observability flags.

Answers, for any candidate: "had we entered at the next bar's open with this
bracket, what would the price path have done?" It does NOT claim what an
order would have filled at (Issue #2 critique #1). Every label says what was
and was not observable:

status
  definitive  the bars resolve the path: exactly one barrier inside the
              decisive bar, a gap through a barrier at a bar's open, or the
              time stop reached — and the window has no data-quality defect
  ambiguous   stop AND target both inside one bar's range: the order is
              unknowable from these bars (EOD OHLC can never resolve it).
              Pessimistic and optimistic outcomes are both reported.
  unobserved  the window has a data defect (missing bars, suspected
              unadjusted split, flagged bars), so the path is not observed
  unfillable  the hypothetical entry cannot be assumed fillable (no ATR,
              entry bar delayed by a data gap, zero volume, invalid prices)
  pending     the window extends past the available data and no barrier
              has been hit yet

flags: path_observed, ambiguous_intrabar, unfillable (+ reason),
simulated_execution (ALWAYS True here: every price is a modelled fill),
resolved_by_finer_data, gap_fill, data_flags.

Conventions: bars follow research.common.bars (``ts_utc`` = bar close).
The entry bar is the first bar that OPENS at or after the decision time;
``entry_ts_utc`` is that bar's OPEN (the modelled fill time). ``exit_ts_utc``
is the CLOSE of the exit bar: when the exit is first observable from bars,
not an intrabar fill time. Finer bars resolve an ambiguous bar only if they
cover it exactly and agree with it (``validate_finer``).
The holding window is ``max_holding_bars`` bars starting with the entry bar;
the time stop exits at the close of its last bar. Stops and targets are set
from the ENTRY price with the decision-time ATR (M17 ATR-mode semantics).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from research.common.bars import TF_MINUTES
from research.evidence.candidates import CandidateEvent
from research.evidence.costs import CostModel

STATUSES = ("definitive", "ambiguous", "unobserved", "unfillable", "pending")
BLOCKING_DATA_FLAGS = ("missing_bars_in_window", "split_suspect", "bar_quality_flag")
_SPLIT_RATIOS = np.log(np.array([2.0, 3.0, 4.0, 5.0, 1.5, 10.0]))


@dataclass(frozen=True)
class SourceInfo:
    provider: str = "synthetic"
    timeframe: str = "1D"
    adjusted: Optional[bool] = None      # None = unknown (reported as such)


@dataclass(frozen=True)
class PathLabel:
    candidate_id: str
    symbol: str
    direction: str
    stage: str
    strategy_id: str
    decision_ts_utc: pd.Timestamp
    status: str
    outcome: Optional[str]
    path_observed: bool
    ambiguous_intrabar: bool
    unfillable: bool
    unfillable_reason: str
    simulated_execution: bool
    resolved_by_finer_data: bool
    gap_fill: bool
    data_flags: Tuple[str, ...]
    entry_idx: int
    exit_idx: int
    entry_ts_utc: Optional[pd.Timestamp]
    exit_ts_utc: Optional[pd.Timestamp]
    holding_bars: int
    entry_price: float
    stop_price: float
    target_price: float
    exit_price: float
    r_gross: float
    r_gross_pessimistic: float
    r_gross_optimistic: float
    r_net: float
    r_net_pessimistic: float
    r_net_optimistic: float
    r_net_stress: float
    mfe_r: float
    mae_r: float
    cost_model: str
    stress_cost_model: str
    provider: str
    timeframe: str
    adjusted: Optional[bool]
    sector: str = ""
    reasons: Tuple[str, ...] = ()
    entry_time_basis: str = "entry_bar_open"           # entry_ts_utc = OPEN of the bar whose open is the fill price
    exit_time_basis: str = "exit_bar_close_observation"  # exit_ts_utc = CLOSE of the exit bar, when the exit is
                                                          # first observable; NOT an intrabar fill time


FinerBars = Callable[[str, pd.Timestamp, pd.Timestamp], Optional[pd.DataFrame]]


def bar_open_times(ts_close: pd.Series, timeframe: str) -> pd.Series:
    return pd.to_datetime(ts_close, utc=True) - pd.Timedelta(TF_MINUTES[timeframe], unit="m")


def _data_flags(o, h, l, c, v, q, ts, lo: int, hi: int, timeframe: str) -> Tuple[str, ...]:
    """Data-quality flags for the window from the decision bar (lo - 1) to the exit bar (hi)."""
    flags = []
    if hi >= lo + 1:
        dt = np.diff(ts[lo:hi + 1]).astype("timedelta64[m]").astype(np.int64)
        if timeframe == "1D":
            d = ts[lo:hi + 1].astype("datetime64[D]")
            between = np.busday_count(d[:-1] + np.timedelta64(1, "D"), d[1:])   # business days skipped
            missing = (between >= 2).any()             # one skipped day may be an exchange holiday
            if (between == 1).any():
                flags.append("possible_holiday_or_missing_bar")
        else:
            same_day = (ts[lo + 1:hi + 1].astype("datetime64[D]") == ts[lo:hi].astype("datetime64[D]"))
            missing = (same_day & (dt > TF_MINUTES[timeframe])).any()
        if missing:
            flags.append("missing_bars_in_window")
    # Unadjusted corporate action: a jump close->close or close->next open
    # whose size matches a common split ratio (includes the decision->entry gap).
    a = max(lo - 1, 0)
    if hi > a:
        cc = np.abs(np.diff(np.log(c[a:hi + 1])))
        co = np.abs(np.log(o[a + 1:hi + 1]) - np.log(c[a:hi]))
        big = np.concatenate([cc[cc > 0.3], co[co > 0.3]])
        if big.size and (np.abs(big[:, None] - _SPLIT_RATIOS[None, :]) < 0.10).any():
            flags.append("split_suspect")
    if (q[lo:hi + 1] != 0).any():
        flags.append("bar_quality_flag")
    if ((h[lo:hi + 1] == l[lo:hi + 1]) & (v[lo:hi + 1] == 0)).any():
        flags.append("stale_bar")
    return tuple(flags)


def validate_finer(fb: Optional[pd.DataFrame], bar_open: pd.Timestamp, bar_close: pd.Timestamp,
                   o: float, h: float, l: float, c: float, rel_tol: float = 1e-6) -> Tuple[bool, str]:
    """Finer bars may resolve an ambiguous coarse bar only if they cover it
    EXACTLY (first finer bar opens at the coarse open, last closes at the
    coarse close, uniform spacing, no gaps), are clean (valid OHLC, no quality
    flags) and are consistent with it (same open, close, high and low)."""
    if fb is None or len(fb) == 0:
        return False, "no_finer_bars"
    need = ("ts_utc", "open", "high", "low", "close")
    if any(k not in fb.columns for k in need):
        return False, "missing_columns"
    ts = pd.to_datetime(fb["ts_utc"], utc=True)
    if not ts.is_monotonic_increasing or ts.duplicated().any():
        return False, "unsorted_or_duplicate_timestamps"
    fo, fh, fl, fc = (fb[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    if not (np.isfinite(fo).all() and np.isfinite(fh).all() and np.isfinite(fl).all() and np.isfinite(fc).all()):
        return False, "non_finite_prices"
    if (fl > np.minimum(fo, fc) + 1e-12).any() or (fh < np.maximum(fo, fc) - 1e-12).any():
        return False, "invalid_ohlc"
    if "quality_flags" in fb.columns and (fb["quality_flags"].to_numpy() != 0).any():
        return False, "quality_flagged"
    if len(ts) >= 2:
        steps = np.diff(ts.to_numpy(dtype="datetime64[ns]")).astype(np.int64)
        if (steps != steps[0]).any():
            return False, "gap_or_uneven_spacing"
        step = pd.Timedelta(int(steps[0]), unit="ns")
    else:
        step = bar_close - bar_open
    if ts.iloc[0] - step != bar_open:
        return False, "does_not_start_at_coarse_open"
    if ts.iloc[-1] != bar_close:
        return False, "does_not_end_at_coarse_close"
    tol = rel_tol * max(abs(h), 1.0)
    if abs(fo[0] - o) > tol or abs(fc[-1] - c) > tol or abs(fh.max() - h) > tol or abs(fl.min() - l) > tol:
        return False, "inconsistent_with_coarse_bar"
    return True, "ok"


def _resolve(o, h, l, c, j0: int, j1: int, stop: float, target: float, long: bool, first_is_entry: bool):
    """Scan bars j0..j1 (inclusive). Returns (kind, j, price, gap) where kind in
    {'stop','target','ambiguous','none'}."""
    oo, hh, ll = o[j0:j1 + 1], h[j0:j1 + 1], l[j0:j1 + 1]
    if long:
        gs, gt = oo <= stop, oo >= target
        hs, ht = ll <= stop, hh >= target
    else:
        gs, gt = oo >= stop, oo <= target
        hs, ht = hh >= stop, ll <= target
    if first_is_entry:                      # entry fills AT the first open: no gap event there
        gs = gs.copy(); gt = gt.copy()
        gs[0] = gt[0] = False
    any_ = gs | gt | hs | ht
    if not any_.any():
        return "none", -1, float("nan"), False
    k = int(np.argmax(any_))
    j = j0 + k
    if gs[k]:
        return "stop", j, float(oo[k]), True
    if gt[k]:
        return "target", j, float(oo[k]), True
    if hs[k] and ht[k]:
        return "ambiguous", j, float("nan"), False
    if hs[k]:
        return "stop", j, float(stop), False
    return "target", j, float(target), False


def _r(entry: float, exit_: float, risk: float, long: bool) -> float:
    return float(((exit_ - entry) if long else (entry - exit_)) / risk)


def label_symbol(cands: List[CandidateEvent], bars: pd.DataFrame, *, timeframe: str,
                 cost: CostModel, stress_cost: CostModel, source: SourceInfo,
                 finer: Optional[FinerBars] = None, max_entry_delay: Optional[pd.Timedelta] = None,
                 data_end_utc: Optional[pd.Timestamp] = None) -> List[PathLabel]:
    ts = pd.to_datetime(bars["ts_utc"], utc=True)
    ts_np = ts.to_numpy(dtype="datetime64[ns]")
    open_t = bar_open_times(ts, timeframe).to_numpy(dtype="datetime64[ns]")
    o, h, l, c = (bars[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    v = bars["volume"].to_numpy(dtype=float)
    q = bars["quality_flags"].to_numpy() if "quality_flags" in bars else np.zeros(len(bars), dtype=np.int64)
    n = len(bars)
    if max_entry_delay is None:
        max_entry_delay = pd.Timedelta(4, unit="D")       # weekend + holiday; longer = data gap
    out: List[PathLabel] = []
    for cd in cands:
        long = cd.direction == "long"
        H = cd.bracket.max_holding_bars
        dts = np.datetime64(cd.decision_ts_utc.tz_convert("UTC").tz_localize(None), "ns")
        e = int(np.searchsorted(open_t, dts, side="left"))
        base = dict(candidate_id=cd.candidate_id, symbol=cd.symbol, direction=cd.direction, stage=cd.stage,
                    strategy_id=cd.strategy_id, decision_ts_utc=cd.decision_ts_utc, simulated_execution=True,
                    cost_model=cost.name, stress_cost_model=stress_cost.name, provider=source.provider,
                    timeframe=timeframe, adjusted=source.adjusted, sector=cd.sector, reasons=cd.reasons)
        nan = float("nan")
        empty = dict(outcome=None, path_observed=False, ambiguous_intrabar=False, resolved_by_finer_data=False,
                     gap_fill=False, data_flags=(), entry_idx=e, exit_idx=-1, entry_ts_utc=None, exit_ts_utc=None,
                     holding_bars=0, entry_price=nan, stop_price=nan, target_price=nan, exit_price=nan,
                     r_gross=nan, r_gross_pessimistic=nan, r_gross_optimistic=nan, r_net=nan,
                     r_net_pessimistic=nan, r_net_optimistic=nan, r_net_stress=nan, mfe_r=nan, mae_r=nan)

        def bad(reason: str, status: str = "unfillable", flags: Tuple[str, ...] = ()) -> None:
            out.append(PathLabel(**base, **{**empty, "status": status, "unfillable": status == "unfillable",
                                            "unfillable_reason": reason if status == "unfillable" else "",
                                            "data_flags": flags}))
        if n == 0:
            bad("", "unobserved", ("no_bars",))
            continue
        if e >= n:
            bad("", "pending")
            continue
        atr = cd.atr_at_decision
        if not (np.isfinite(atr) and atr > 0):
            bad("no_atr_at_decision")
            continue
        if pd.Timedelta(open_t[e] - dts) > max_entry_delay:
            bad("entry_delayed_by_data_gap")
            continue
        entry = o[e]
        if not (np.isfinite(entry) and entry > 0) or v[e] <= 0:
            bad("entry_bar_not_tradable")
            continue
        risk = cd.bracket.stop_atr_mult * atr
        stop = entry - risk if long else entry + risk
        target = entry + cd.bracket.target_atr_mult * atr if long else entry - cd.bracket.target_atr_mult * atr
        if (long and stop <= 0) or not np.isfinite(stop):
            bad("invalid_stop")
            continue
        last = e + H - 1
        j1 = min(last, n - 1)
        kind, j, px, gap = _resolve(o, h, l, c, e, j1, stop, target, long, first_is_entry=True)
        finer_used = False
        finer_flags: Tuple[str, ...] = ()
        pess = opt = nan
        if kind == "ambiguous":
            pess, opt = _r(entry, stop, risk, long), _r(entry, target, risk, long)
            if finer is not None:
                b_open, b_close = pd.Timestamp(open_t[j], tz="UTC"), pd.Timestamp(ts_np[j], tz="UTC")
                fb = finer(cd.symbol, b_open, b_close)
                ok, why = validate_finer(fb, b_open, b_close, o[j], h[j], l[j], c[j])
                if not ok:
                    finer_flags = ("finer_data_rejected:%s" % why,)
                else:
                    fo, fh, fl, fc = (fb[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
                    k2, _, px2, gap2 = _resolve(fo, fh, fl, fc, 0, len(fb) - 1, stop, target, long,
                                                first_is_entry=(j == e))
                    if k2 in ("stop", "target"):
                        kind, px, gap, finer_used = k2, px2, gap2, True
        if kind == "none":
            if last > n - 1:
                # The window runs past this symbol's data. If the DATASET continues
                # well beyond the symbol's last bar, the series ended early
                # (delisting / halt / feed loss): that is not "pending", and
                # dropping it would hide losers (survivorship bias).
                if data_end_utc is not None and pd.Timestamp(data_end_utc) - pd.Timestamp(ts_np[-1], tz="UTC") \
                        > pd.Timedelta(5, unit="D"):
                    bad("", "unobserved", ("series_ended_early",))
                else:
                    bad("", "pending")
                continue
            kind, j, px = "timeout", last, c[last]
        flags = _data_flags(o, h, l, c, v, q, ts_np, e, j, timeframe) + finer_flags
        blocking = [f for f in flags if f in BLOCKING_DATA_FLAGS]
        if kind == "ambiguous":
            status = "ambiguous"
        elif blocking:
            status = "unobserved"
        else:
            status = "definitive"
        r_g = nan if kind == "ambiguous" else _r(entry, px, risk, long)
        hold = j - e + 1

        def net(exit_price: float, model: CostModel, stop_gap: bool) -> float:
            if not np.isfinite(exit_price):
                return nan
            cost_ps = model.round_trip_cost(entry, exit_price, short=not long, holding_bars=hold, stop_gap=stop_gap)
            return _r(entry, exit_price, risk, long) - cost_ps / risk
        stop_gap = gap and kind == "stop"
        exit_px = px if kind != "ambiguous" else nan
        r_n = net(exit_px, cost, stop_gap)
        r_ns = net(exit_px, stress_cost, stop_gap)
        r_np = net(stop, cost, False) if kind == "ambiguous" else r_n
        r_no = net(target, cost, False) if kind == "ambiguous" else r_n
        if kind != "ambiguous":
            pess = opt = r_g
        hi_path = h[e:j] if j > e else np.array([])
        lo_path = l[e:j] if j > e else np.array([])
        if long:
            best = max([entry] + list(hi_path) + ([px] if np.isfinite(px) else []))
            worst = min([entry] + list(lo_path) + ([px] if np.isfinite(px) else []))
            mfe, mae = (best - entry) / risk, (worst - entry) / risk
        else:
            best = min([entry] + list(lo_path) + ([px] if np.isfinite(px) else []))
            worst = max([entry] + list(hi_path) + ([px] if np.isfinite(px) else []))
            mfe, mae = (entry - best) / risk, (entry - worst) / risk
        out.append(PathLabel(
            **base, status=status, outcome=None if kind == "ambiguous" else kind,
            path_observed=status == "definitive", ambiguous_intrabar=kind == "ambiguous", unfillable=False,
            unfillable_reason="", resolved_by_finer_data=finer_used, gap_fill=bool(gap), data_flags=flags,
            entry_idx=e, exit_idx=j, entry_ts_utc=pd.Timestamp(open_t[e], tz="UTC"),
            exit_ts_utc=pd.Timestamp(ts_np[j], tz="UTC"), holding_bars=hold, entry_price=float(entry),
            stop_price=float(stop), target_price=float(target), exit_price=float(exit_px), r_gross=r_g,
            r_gross_pessimistic=float(pess), r_gross_optimistic=float(opt), r_net=r_n,
            r_net_pessimistic=r_np, r_net_optimistic=r_no, r_net_stress=r_ns, mfe_r=float(mfe), mae_r=float(mae)))
    return out


def label_candidates(cands: Iterable[CandidateEvent], bars_by_symbol: Dict[str, pd.DataFrame], *,
                     timeframe: str, cost: CostModel = CostModel(), stress_cost: Optional[CostModel] = None,
                     source: SourceInfo = SourceInfo(), finer: Optional[FinerBars] = None,
                     data_end_utc: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """Label every candidate; returns one row per candidate (PathLabel fields).

    data_end_utc: the end of the whole dataset. Defaults to the latest bar of
    any symbol, so a symbol whose bars stop earlier is recognised as ended."""
    if data_end_utc is None:
        ends = [pd.to_datetime(b["ts_utc"], utc=True).max() for b in bars_by_symbol.values() if len(b)]
        data_end_utc = max(ends) if ends else None
    stress_cost = stress_cost or cost.scale(2.0)
    by_sym: Dict[str, List[CandidateEvent]] = {}
    for cd in cands:
        by_sym.setdefault(cd.symbol, []).append(cd)
    rows: List[PathLabel] = []
    for sym, cs in by_sym.items():
        bars = bars_by_symbol.get(sym)
        if bars is None or len(bars) == 0:
            for cd in cs:
                rows.extend(label_symbol([cd], _EMPTY, timeframe=timeframe, cost=cost, stress_cost=stress_cost,
                                         source=source))
            continue
        rows.extend(label_symbol(cs, bars, timeframe=timeframe, cost=cost, stress_cost=stress_cost,
                                 source=source, finer=finer, data_end_utc=data_end_utc))
    df = pd.DataFrame([asdict(r) for r in rows])
    if len(df):
        df["data_flags"] = df["data_flags"].map(lambda t: "|".join(t))
        df["reasons"] = df["reasons"].map(lambda t: "|".join(t))
    return df


_EMPTY = pd.DataFrame({"ts_utc": pd.to_datetime([], utc=True), "open": [], "high": [], "low": [], "close": [],
                       "volume": [], "quality_flags": []})


def atr_asof(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR (simple mean of true range, live-scanner parity) known at each bar close."""
    from bot.backtesting import indicators as ind
    return ind.atr(bars["high"].astype(float), bars["low"].astype(float), bars["close"].astype(float),
                   period=period, mode="sma_true_range")


def status_summary(labels: pd.DataFrame) -> Dict[str, float]:
    n = len(labels)
    out = {"n": float(n)}
    for s in STATUSES:
        out["share_" + s] = float((labels["status"] == s).mean()) if n else float("nan")
    return out


__all__ = ["STATUSES", "BLOCKING_DATA_FLAGS", "SourceInfo", "PathLabel", "bar_open_times", "validate_finer", "label_symbol",
           "label_candidates", "atr_asof", "status_summary"]
