"""research.dataqa.readiness — real historical-data readiness checks (M21 WP11; B01, E05, A17).

Decides, per symbol and timeframe and for the panel, whether a historical bar
snapshot may be used as research evidence. Every check returns PASS, FAIL or
INCONCLUSIVE with its reasons; the overall verdict is the worst of them.

Checks
  convention    bar stamps are bar START or CLOSE (``stamps.detect_convention``),
                cross-checked with the provider's declared convention; mixed
                or contradicted stamps FAIL
  conversion    bars are re-stamped to CLOSE with a ``bar_open_ts`` column
                (the M16->M17 lookahead fix); duplicate or inconsistent rows FAIL
  coverage      bars present vs the real exchange calendar (holidays, early
                closes); too many missing bars FAIL; data the calendar does not
                cover is INCONCLUSIVE; a series ending early is reported
  basis         declared price basis vs split-like jumps and the corporate-
                action record (adjusted-bar policy below)
  liquidity     volume completeness and how often the WP12 point-in-time
                liquidity snapshot is available
  survivorship  point-in-time membership: members without bars FAIL; a
                universe that records no exits is INCONCLUSIVE; otherwise the
                survivor-vs-all return gap is quantified
  parity        the lane A scanner replica and labeller agree exactly with the
                M17 simulator on the converted bars

Adjusted-bar policy (WP11):
* return- and ratio-based features may use split-adjusted (or total-return)
  bars; adjusted price LEVELS are not point-in-time (a later split rescales
  history), so price-level rules (minimum price, tick floors, share counts)
  use raw = adjusted x split factor, which needs a COMPLETE corporate-action
  record (``research.economics.model.CorporateActions``);
* raw bars are usable only with a complete record that explains every
  split-like jump;
* an unknown basis is INCONCLUSIVE.

Origin. ``origin="synthetic"`` marks reports on synthetic samples. They show
the machinery works; ``real_data_ready`` is always False for them.

Read-only: the CLI reads an M16 parquet store through ``bot.historical.store``
and a calendar file, and writes only to the ``--out`` directory it is given.
Running it on the production store is OP1 (operator approval).
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from bot.scoring_truth.sessions import SessionCalendar
from research.dataqa import stamps as ST
from research.dataqa.quality import _split_candidates
from research.economics.model import CorporateActions, SplitEvent, liquidity_asof

PASS, FAIL, INCONCLUSIVE = "PASS", "FAIL", "INCONCLUSIVE"
_RANK = {PASS: 0, INCONCLUSIVE: 1, FAIL: 2}
BASES = ("raw", "split_adjusted", "all_adjusted", "unknown")
SYNTHETIC_BANNER = ("SYNTHETIC SAMPLE: this report exercises the readiness tooling on generated data. "
                    "It says nothing about real-data readiness.")


@dataclass
class Check:
    name: str
    verdict: str
    reasons: List[str] = field(default_factory=list)
    detail: Dict = field(default_factory=dict)


def worst(verdicts: Iterable[str]) -> str:
    vs = list(verdicts)
    return max(vs, key=lambda v: _RANK[v]) if vs else INCONCLUSIVE


# ── convention ───────────────────────────────────────────────────────────────
def convention_check(df: pd.DataFrame, timeframe: str, cal: SessionCalendar,
                     declared: Optional[str]) -> Tuple[Check, Optional[str]]:
    rep = ST.detect_convention(df["ts_utc"], timeframe, cal)
    d = rep.as_dict()
    reasons: List[str] = []
    use: Optional[str] = None
    if rep.convention in (ST.START, ST.CLOSE):
        if declared in (ST.START, ST.CLOSE) and declared != rep.convention:
            return Check("convention", FAIL, ["declared_%s_but_bars_show_%s" % (declared, rep.convention)], d), None
        use, verdict = rep.convention, PASS
    elif rep.convention == ST.MIXED:
        return Check("convention", FAIL, ["mixed_start_and_close_stamps"], d), None
    elif declared in (ST.START, ST.CLOSE):
        use, verdict = declared, PASS
        reasons.append("not_identifiable_from_stamps_used_declared_%s" % declared)
    else:
        return Check("convention", INCONCLUSIVE, ["convention_unknown_and_not_declared"], d), None
    if rep.uncovered:
        verdict = worst([verdict, INCONCLUSIVE])
        reasons.append("calendar_does_not_cover_%d_rows" % rep.uncovered)
    return Check("convention", verdict, reasons, d), use


def conversion_check(rep: ST.ConversionReport) -> Check:
    d = rep.as_dict()
    reasons, verdict = [], PASS
    if rep.duplicate_close:
        verdict, reasons = FAIL, reasons + ["%d_rows_share_a_close" % rep.duplicate_close]
    if rep.dropped_inconsistent:
        verdict, reasons = FAIL, reasons + ["%d_rows_inconsistent_with_convention" % rep.dropped_inconsistent]
    if rep.dropped_uncovered:
        verdict, reasons = worst([verdict, INCONCLUSIVE]), reasons + ["%d_rows_outside_calendar_coverage"
                                                                       % rep.dropped_uncovered]
    if rep.dropped_outside_session:
        reasons.append("%d_extended_hours_or_closed_day_rows_removed" % rep.dropped_outside_session)
    if rep.inexact_close:
        reasons.append("%d_bucket_closes_are_conservative_upper_bounds" % rep.inexact_close)
    if rep.n_out == 0:
        verdict, reasons = worst([verdict, INCONCLUSIVE]), reasons + ["no_bars_after_conversion"]
    return Check("conversion", verdict, reasons, d)


# ── coverage ─────────────────────────────────────────────────────────────────
def _expected_per_session(o: pd.Timestamp, c: pd.Timestamp, timeframe: str, bucket_grid: str = "utc") -> int:
    if timeframe == "1D":
        return 1
    m = ST.TF_MINUTES[timeframe]
    if timeframe == "4H" and bucket_grid == "utc":   # UTC 4h buckets that overlap the session
        g = m * 60
        first = int(o.timestamp()) // g
        last = (int(c.timestamp()) - 1) // g
        return last - first + 1
    return int(math.ceil((c - o).total_seconds() / 60.0 / m))


def coverage_check(conv: pd.DataFrame, timeframe: str, cal: SessionCalendar, *,
                   window_start: Optional[date] = None, window_end: Optional[date] = None,
                   max_missing: float = 0.02, show: int = 5, bucket_grid: str = "utc") -> Check:
    """``bucket_grid`` (4H only): "utc" for 1H bars resampled into UTC 4-hour
    buckets (the live scanner and M16), "session" for session-anchored buckets
    (the synthetic research grid)."""
    if len(conv) == 0:
        return Check("coverage", INCONCLUSIVE, ["no_bars"])
    opens = pd.to_datetime(conv["bar_open_ts"], utc=True)
    days = opens.dt.tz_convert(cal.tz).dt.date
    first, last = days.min(), days.max()
    lo, hi = window_start or first, window_end or last
    if lo < cal.coverage_start or hi > cal.coverage_end:
        return Check("coverage", INCONCLUSIVE, ["calendar_coverage_%s..%s_does_not_span_%s..%s"
                                                 % (cal.coverage_start, cal.coverage_end, lo, hi)])
    per_day = days.value_counts().to_dict()
    expected = present = 0
    missing_days, short_days = [], []
    d = lo
    while d <= hi:
        b = cal.session_bounds(d)
        if b is not None:
            e = _expected_per_session(pd.Timestamp(b[0]), pd.Timestamp(b[1]), timeframe, bucket_grid)
            p = min(int(per_day.get(d, 0)), e)
            expected += e
            present += p
            if p == 0:
                missing_days.append(str(d))
            elif p < e:
                short_days.append(str(d))
        d = d + timedelta(days=1)
    extra = int(sum(n for dd, n in per_day.items() if cal.session_bounds(dd) is None))
    miss = expected - present
    ratio = miss / expected if expected else float("nan")
    detail = {"window": [str(lo), str(hi)], "data_first": str(first), "data_last": str(last),
              "bars_expected": expected, "bars_present": present, "bars_missing": miss,
              "missing_ratio": ratio, "sessions_missing": len(missing_days),
              "sessions_short": len(short_days), "first_missing_sessions": missing_days[:show],
              "rows_on_closed_days": extra}
    reasons = []
    verdict = PASS
    if extra:
        verdict, reasons = FAIL, reasons + ["%d_rows_on_closed_days" % extra]
    if expected and ratio > max_missing:
        verdict, reasons = FAIL, reasons + ["missing_ratio_%.4f_above_%.4f" % (ratio, max_missing)]
    if window_start and first > window_start:
        reasons.append("late_start_%s" % first)
    if window_end and last < window_end:
        reasons.append("early_end_%s_possible_delisting" % last)
    return Check("coverage", verdict, reasons, detail)


# ── basis ────────────────────────────────────────────────────────────────────
def basis_check(daily_conv: pd.DataFrame, declared_basis: str, actions: Optional[CorporateActions], *,
                tol_days: int = 3) -> Check:
    if declared_basis not in BASES:
        raise ValueError("basis must be one of %s" % (BASES,))
    jumps = _split_candidates(daily_conv) if len(daily_conv) else []
    detail = {"declared_basis": declared_basis, "split_like_jumps": jumps,
              "corporate_actions_complete": bool(actions is not None and actions.complete),
              "recorded_splits": [[str(s.ex_date), s.ratio] for s in (actions.splits if actions else ())]}
    if declared_basis == "unknown":
        return Check("basis", INCONCLUSIVE, ["price_basis_unknown"], detail)
    if declared_basis in ("split_adjusted", "all_adjusted"):
        if jumps:
            return Check("basis", FAIL, ["adjusted_basis_contradicted_by_%d_split_like_jumps" % len(jumps)], detail)
        if actions is None or not actions.complete:
            return Check("basis", INCONCLUSIVE, ["adjusted_ok_for_returns_but_price_levels_need_a_complete_"
                                                  "corporate_action_record"], detail)
        return Check("basis", PASS, [], detail)
    # raw
    if actions is None or not actions.complete:
        return Check("basis", INCONCLUSIVE, ["raw_prices_without_a_complete_corporate_action_record"], detail)
    unexplained = []
    for j in jumps:
        jd = pd.Timestamp(j["ts_utc"]).date()
        ok = any(abs((s.ex_date - jd).days) <= tol_days and abs(math.log(s.ratio) - math.log(j["ratio"]))
                 < 0.03 for s in actions.splits)
        if not ok:
            unexplained.append(j["ts_utc"])
    if unexplained:
        return Check("basis", FAIL, ["%d_split_like_jumps_not_in_the_record" % len(unexplained)], detail)
    return Check("basis", PASS, ["%d_recorded_splits_explain_the_jumps" % len(jumps)] if jumps else [], detail)


# ── liquidity ────────────────────────────────────────────────────────────────
def liquidity_check(daily_conv: pd.DataFrame, *, window: int = 20, min_sessions: int = 15,
                    max_zero_share: float = 0.05, min_ok_share: float = 0.95) -> Check:
    n = len(daily_conv)
    if n == 0:
        return Check("liquidity", INCONCLUSIVE, ["no_daily_bars"])
    v = daily_conv["volume"].to_numpy(dtype=float)
    bad = int(np.sum(~np.isfinite(v) | (v < 0)))
    zero = int(np.sum(v == 0))
    ts = pd.to_datetime(daily_conv["ts_utc"], utc=True)
    after = ts.iloc[min_sessions:]
    ok = sum(liquidity_asof(daily_conv, t, window=window, min_sessions=min_sessions).ok for t in after)
    share = ok / len(after) if len(after) else float("nan")
    detail = {"rows": n, "bad_volume_rows": bad, "zero_volume_rows": zero, "warmup_sessions": min_sessions,
              "snapshot_ok_share_after_warmup": share}
    if bad:
        return Check("liquidity", FAIL, ["%d_rows_with_missing_or_negative_volume" % bad], detail)
    if zero / n > max_zero_share:
        return Check("liquidity", FAIL, ["zero_volume_share_%.3f_above_%.3f" % (zero / n, max_zero_share)], detail)
    if not len(after):
        return Check("liquidity", INCONCLUSIVE, ["history_shorter_than_warmup"], detail)
    if share < min_ok_share:
        return Check("liquidity", INCONCLUSIVE, ["liquidity_snapshot_available_%.3f_below_%.3f"
                                                  % (share, min_ok_share)], detail)
    return Check("liquidity", PASS, [], detail)


# ── universe ─────────────────────────────────────────────────────────────────
def _membership(m: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame({"symbol": m["symbol"].astype(str),
                        "start": pd.to_datetime(m["start"]).dt.date,
                        "end": pd.to_datetime(m["end"]).dt.date if "end" in m.columns else None})
    out["end"] = [None if (e is None or pd.isna(e)) else e for e in out["end"]]
    return out


def membership_arrays(membership: pd.DataFrame, bars_by_symbol: Mapping[str, pd.DataFrame], *,
                      tz: str = "America/New_York") -> Dict[str, np.ndarray]:
    """Point-in-time universe masks for ``research.genome.engine.run(membership=...)``:
    True where the bar's session date lies inside one of the symbol's membership
    spells (start inclusive, end inclusive; open-ended when end is empty). The
    membership table itself must be point-in-time (as published, not as revised)."""
    mem = _membership(membership)
    out = {}
    for s, b in bars_by_symbol.items():
        d = pd.to_datetime(b["ts_utc"], utc=True).dt.tz_convert(tz).dt.date.to_numpy()
        mask = np.zeros(len(b), dtype=bool)
        for r in mem[mem["symbol"] == s].itertuples(index=False):
            mask |= np.array([(x >= r.start) and (r.end is None or x <= r.end) for x in d], dtype=bool)
        out[s] = mask
    return out


def survivorship_check(membership: Optional[pd.DataFrame], daily_by_symbol: Mapping[str, pd.DataFrame], *,
                       window_start: date, window_end: date, present: Optional[Iterable[str]] = None) -> Check:
    """``present``: symbols that have any bars at all (defaults to the keys of
    ``daily_by_symbol``). Returns use the converted daily bars."""
    if membership is None or len(membership) == 0:
        return Check("survivorship", INCONCLUSIVE, ["no_point_in_time_membership_survivorship_not_assessable"])
    mem = _membership(membership)
    mem = mem[(mem["start"] <= window_end) & mem["end"].map(lambda e: e is None or e >= window_start)]
    syms = sorted(set(mem["symbol"]))
    have = set(present) if present is not None else {s for s, b in daily_by_symbol.items() if len(b)}
    without = [s for s in syms if s not in have]
    exited = sorted(set(mem.loc[mem["end"].map(lambda e: e is not None and e < window_end), "symbol"]))
    survivors = [s for s in syms if s not in exited]
    rets = {}
    for s in syms:
        b = daily_by_symbol.get(s)
        if b is None or len(b) < 2:
            continue
        c = b["close"].to_numpy(dtype=float)
        lr = np.diff(np.log(c[np.isfinite(c) & (c > 0)]))
        if len(lr):
            rets[s] = float(np.mean(lr) * 252)
    all_mean = float(np.mean(list(rets.values()))) if rets else float("nan")
    surv = [rets[s] for s in survivors if s in rets]
    surv_mean = float(np.mean(surv)) if surv else float("nan")
    detail = {"members_in_window": len(syms), "members_without_bars": without, "exited_in_window": exited,
              "annualised_mean_log_return_all": all_mean, "annualised_mean_log_return_survivors": surv_mean,
              "survivorship_bias_estimate": surv_mean - all_mean if rets and surv else float("nan")}
    if without:
        return Check("survivorship", FAIL, ["%d_members_have_no_bars_(delisted_data_missing?)" % len(without)],
                     detail)
    if not exited:
        return Check("survivorship", INCONCLUSIVE, ["membership_records_no_exits_survivorship_cannot_be_ruled_out"],
                     detail)
    return Check("survivorship", PASS, ["bias_quantified"], detail)


# ── parity ───────────────────────────────────────────────────────────────────
def parity_check(per_tf_by_symbol: Mapping[str, Mapping[str, pd.DataFrame]], *, provider: str,
                 adjusted: Optional[bool], min_compared: int = 30, seed: int = 0) -> Check:
    from research.common.rng import stream
    from research.evidence import parity
    from research.evidence.candidates import Bracket
    from research.evidence.labeler import SourceInfo, label_candidates
    from research.evidence.replay import scanner_stage_candidates
    try:
        cands, b15 = [], {}
        for s, per_tf in sorted(per_tf_by_symbol.items()):
            cands += scanner_stage_candidates(dict(per_tf), s, rng=stream(seed, "wp11", s),
                                              bracket=Bracket(1.0, 1.5, 26), rejected_rate=0.0)
            b15[s] = per_tf["15m"]
        lab = label_candidates(cands, b15, timeframe="15m", source=SourceInfo(provider, "15m", adjusted))
        pr = parity.check(lab, b15, {c.candidate_id: c for c in cands}, timeframe="15m")
    except (ValueError, KeyError) as exc:
        return Check("parity", FAIL, ["parity_run_refused: %s" % str(exc)[:160]])
    detail = {"candidates": len(cands), "compared": pr.n_compared, "mismatches": pr.n_mismatch,
              "first_mismatches": pr.mismatches[:3]}
    if pr.n_mismatch:
        return Check("parity", FAIL, ["%d_parity_mismatches" % pr.n_mismatch], detail)
    if pr.n_compared < min_compared:
        return Check("parity", INCONCLUSIVE, ["only_%d_paths_compared_(min_%d)" % (pr.n_compared, min_compared)],
                     detail)
    return Check("parity", PASS, [], detail)


# ── assessment ───────────────────────────────────────────────────────────────
def _bucket_grid(ts_utc, convention: str) -> str:
    """4H stamps on the UTC 4-hour grid -> "utc"; otherwise "session". Start
    stamps of UTC buckets are all on the grid; close stamps are on it except
    where a session close cuts a bucket short, so a majority decides."""
    on = (pd.to_datetime(pd.Series(ts_utc), utc=True).astype("int64") // 10**9) % (4 * 3600) == 0
    if convention == ST.START:
        return "utc" if bool(on.all()) else "session"
    return "utc" if float(on.mean()) > 0.5 else "session"


def assess(bars: Mapping[str, Mapping[str, pd.DataFrame]], cal: SessionCalendar, *, origin: str,
           provider: str, declared_convention: Mapping[str, str], declared_basis: str,
           actions_by_symbol: Optional[Mapping[str, CorporateActions]] = None,
           membership: Optional[pd.DataFrame] = None, window_start: Optional[date] = None,
           window_end: Optional[date] = None, run_parity: bool = True, max_missing: float = 0.02,
           min_parity: int = 30) -> Dict:
    """``bars``: {symbol: {timeframe: raw store frame}}. Returns a JSON-able
    report and the converted (close-stamped) frames under ``"_converted"``."""
    if origin not in ("synthetic", "real"):
        raise ValueError("origin must be 'synthetic' or 'real'")
    actions_by_symbol = actions_by_symbol or {}
    per: Dict[str, Dict] = {}
    converted: Dict[str, Dict[str, pd.DataFrame]] = {}
    for sym in sorted(bars):
        per[sym], converted[sym] = {}, {}
        for tf in [t for t in ST.TIMEFRAMES if t in bars[sym]]:
            df = bars[sym][tf]
            checks = []
            conv_chk, use = convention_check(df, tf, cal, declared_convention.get(tf))
            checks.append(conv_chk)
            if use is not None:
                out, rep = ST.to_close_stamped(df, tf, cal, use)
                converted[sym][tf] = out
                checks.append(conversion_check(rep))
                grid = _bucket_grid(df["ts_utc"], use) if tf == "4H" else "utc"
                checks.append(coverage_check(out, tf, cal, window_start=window_start, window_end=window_end,
                                             max_missing=max_missing, bucket_grid=grid))
            if tf == "1D" and use is not None:
                checks.append(basis_check(converted[sym]["1D"], declared_basis, actions_by_symbol.get(sym)))
                checks.append(liquidity_check(converted[sym]["1D"]))
            per[sym][tf] = {"verdict": worst(c.verdict for c in checks), "checks": [asdict(c) for c in checks]}
    panel = []
    daily = {s: converted[s]["1D"] for s in converted if "1D" in converted[s]}
    if window_start and window_end:
        panel.append(survivorship_check(membership, daily, window_start=window_start, window_end=window_end,
                                        present=[s for s in bars if any(len(b) for b in bars[s].values())]))
    else:
        panel.append(Check("survivorship", INCONCLUSIVE, ["no_window_given"]))
    if run_parity:
        ready = {s: converted[s] for s in converted if all(t in converted[s] for t in ST.TIMEFRAMES)}
        if ready:
            panel.append(parity_check(ready, provider=provider,
                                      adjusted=None if declared_basis == "unknown" else declared_basis != "raw",
                                      min_compared=min_parity))
        else:
            panel.append(Check("parity", INCONCLUSIVE, ["no_symbol_has_all_four_converted_timeframes"]))
    overall = worst([v["verdict"] for p in per.values() for v in p.values()] + [c.verdict for c in panel])
    return {
        "origin": origin, "banner": SYNTHETIC_BANNER if origin == "synthetic" else "",
        "provider": provider, "calendar": {"name": cal.name, "source": cal.source,
                                           "coverage": [str(cal.coverage_start), str(cal.coverage_end)]},
        "declared_convention": dict(declared_convention), "declared_basis": declared_basis,
        "overall": overall, "real_data_ready": bool(origin == "real" and overall == PASS),
        "symbols": per, "panel": [asdict(c) for c in panel], "_converted": converted,
    }


def _jsonable(report: Dict) -> Dict:
    r = {k: v for k, v in report.items() if k != "_converted"}
    return json.loads(json.dumps(r, default=str))


def render_markdown(report: Dict) -> str:
    r = _jsonable(report)
    L = ["# Real-data readiness report", ""]
    if r["banner"]:
        L += ["> **%s**" % r["banner"], ""]
    L += ["- Origin: **%s**; provider: `%s`; calendar: %s (%s .. %s)" % (
        r["origin"], r["provider"], r["calendar"]["name"], *r["calendar"]["coverage"]),
          "- Declared convention: %s; declared basis: `%s`" % (r["declared_convention"], r["declared_basis"]),
          "- **Overall: %s**; real-data ready: **%s**" % (r["overall"], "yes" if r["real_data_ready"] else "no"),
          "", "| Symbol | TF | Verdict | Failing / inconclusive checks |", "|---|---|---|---|"]
    for s, per in r["symbols"].items():
        for tf, v in per.items():
            bad = ["%s: %s" % (c["name"], ", ".join(c["reasons"]) or c["verdict"]) for c in v["checks"]
                   if c["verdict"] != PASS]
            L.append("| %s | %s | %s | %s |" % (s, tf, v["verdict"], "; ".join(bad) or "—"))
    L += ["", "| Panel check | Verdict | Reasons |", "|---|---|---|"]
    for c in r["panel"]:
        L.append("| %s | %s | %s |" % (c["name"], c["verdict"], ", ".join(c["reasons"]) or "—"))
    return "\n".join(L) + "\n"


# ── synthetic sample and CLI ─────────────────────────────────────────────────
def synthetic_sample(seed: int = 11, n_symbols: int = 3, n_sessions: int = 60) -> Dict:
    """A deterministic, labelled synthetic run: close-stamped synthetic bars on
    the synthetic calendar, declared 'close'. Shows the machinery only."""
    from research.common.bars import multi_timeframe
    from research.common.synthetic import MarketSpec, generate
    from research.dataqa.calendar import synthetic_calendar
    m = generate(MarketSpec(n_symbols=n_symbols, n_sessions=n_sessions, timeframe="15m", seed=seed))
    bars = {s: multi_timeframe(b) for s, b in m.bars.items()}
    days = [pd.Timestamp(t).date() for b in m.bars.values() for t in (b["ts_utc"].iloc[0], b["ts_utc"].iloc[-1])]
    cal = synthetic_calendar(min(days), max(days))
    mem = pd.DataFrame({"symbol": sorted(bars), "start": [min(days)] * len(bars), "end": [None] * len(bars)})
    return assess(bars, cal, origin="synthetic", provider="synthetic",
                  declared_convention={tf: ST.CLOSE for tf in ST.TIMEFRAMES}, declared_basis="split_adjusted",
                  actions_by_symbol={s: CorporateActions(True) for s in bars}, membership=mem,
                  window_start=min(days), window_end=max(days))


def _load_actions(path: Optional[str]) -> Dict[str, CorporateActions]:
    if not path:
        return {}
    raw = json.loads(Path(path).read_text())
    return {s: CorporateActions(bool(v.get("complete")),
                                tuple(SplitEvent(date.fromisoformat(d), float(r)) for d, r in v.get("splits", [])))
            for s, v in raw.items()}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only real-data readiness report (M21 WP11).")
    ap.add_argument("--out", required=True, help="directory for report.json and report.md")
    ap.add_argument("--synthetic-sample", action="store_true", help="labelled synthetic demonstration run")
    ap.add_argument("--store", help="M16 parquet root (<root>/<provider>/<tf>/<SYM>.parquet)")
    ap.add_argument("--provider", default="yfinance")
    ap.add_argument("--calendar", help="JSON list of {date, open, close} calendar rows")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--timeframes", default="1D,4H,1H,15m")
    ap.add_argument("--adjusted", action="store_true", help="read adjusted bars (M16 adj_close: total return)")
    ap.add_argument("--convention", default="start", choices=("start", "close"),
                    help="provider's documented stamp convention (M16 providers stamp bar starts)")
    ap.add_argument("--actions", help="JSON {SYM: {complete: bool, splits: [[YYYY-MM-DD, ratio], ...]}}")
    ap.add_argument("--membership", help="CSV symbol,start,end (point-in-time universe)")
    ap.add_argument("--start")
    ap.add_argument("--end")
    a = ap.parse_args(argv)
    if a.synthetic_sample:
        rep = synthetic_sample()
    else:
        if not (a.store and a.calendar and a.symbols):
            ap.error("--store, --calendar and --symbols are required (or use --synthetic-sample)")
        from bot.historical.store import get_bars
        from research.dataqa.calendar import calendar_from_rows
        cal = calendar_from_rows(json.loads(Path(a.calendar).read_text()))
        tfs = [t for t in a.timeframes.split(",") if t]
        bars = {s: {tf: get_bars(s, tf, provider=a.provider, adjusted=a.adjusted, parquet_root=Path(a.store))
                    for tf in tfs} for s in a.symbols.split(",") if s}
        bars = {s: {tf: df for tf, df in per.items() if len(df)} for s, per in bars.items()}
        mem = pd.read_csv(a.membership) if a.membership else None
        rep = assess(bars, cal, origin="real", provider=a.provider,
                     declared_convention={tf: a.convention for tf in tfs},
                     declared_basis="all_adjusted" if a.adjusted else "raw",
                     actions_by_symbol=_load_actions(a.actions), membership=mem,
                     window_start=date.fromisoformat(a.start) if a.start else None,
                     window_end=date.fromisoformat(a.end) if a.end else None)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(_jsonable(rep), indent=2, sort_keys=True))
    (out / "report.md").write_text(render_markdown(rep))
    print("overall=%s real_data_ready=%s origin=%s" % (rep["overall"], rep["real_data_ready"], rep["origin"]))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["PASS", "FAIL", "INCONCLUSIVE", "Check", "worst", "convention_check", "conversion_check",
           "coverage_check", "basis_check", "liquidity_check", "membership_arrays", "survivorship_check",
           "parity_check", "assess", "render_markdown", "synthetic_sample", "main"]
