"""research.dataqa.converted — the authorised converted-data path for M17 multi-timeframe research and
M18 dataset builds (M21 E3).

M16 stores provider bars stamped at their START; M17 multi-timeframe alignment and the M18 assembler treat
``ts_utc`` as the bar CLOSE. Their default real-data entrances therefore REFUSE raw M16 multi-timeframe
bars (``bot.backtesting.mtf_context.refuse_start_stamped``; PR #20). This module is the one way to use that
data, and it requires evidence at every step:

1. **Calendar evidence.** A calendar file (Alpaca ``/v2/calendar`` rows plus ``source`` and
   ``fetched_at_utc``) builds the exchange session calendar (``calendar.calendar_from_rows``: regular-hours
   rows only, holidays inferred inside the covered range, early closes kept). Its row digest is recorded.
2. **Convention per timeframe.** ``stamps.detect_convention`` must find ONE convention (start or close).
   Mixed or unknown -> refused. Any row outside the calendar's coverage -> refused (never dropped silently).
3. **Conversion.** ``stamps.to_close_stamped``: ``ts_utc`` = the bar's real close (DST and early closes
   from the calendar), ``bar_open_ts`` = when its session trading began. Inconsistent rows -> refused;
   extended-hours rows are dropped and counted (regular-session policy).
4. **Completed bars only.** Bars whose close is after ``asof_utc`` (forming then, or later) are dropped and
   counted.
5. **The unchanged guard.** The converted frames still pass through ``refuse_start_stamped``.
6. **Evidence travels with the bars.** M17: ``MultiTfBars.conversion`` (the runner refuses injected bars
   without it). M18: ``<output>/conversion_evidence.json`` next to the dataset and manifest.

Nothing here weakens the default entrances: ``load_multi_tf_bars`` and the M18 default provider still
refuse raw M16 bars. Read-only on the M16 store; writes only where the operator points the output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from research.dataqa.calendar import calendar_from_rows
from research.dataqa.stamps import CLOSE, START, detect_convention, to_close_stamped

METHOD = "m21-e3-close-stamped/1"          # also checked by bot.backtesting.runner for injected bars


class ConversionRefused(ValueError):
    """The bars cannot be proven close-stamped and complete: nothing is converted."""


@dataclass(frozen=True)
class CalendarEvidence:
    name: str
    source: str
    fetched_at_utc: str
    coverage_start: str
    coverage_end: str
    n_rows: int
    rows_sha256: str


def _aware(ts: Any, what: str) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        raise ConversionRefused("%s must be timezone-aware" % what)
    return t.tz_convert("UTC")


def load_calendar_file(path: str):
    """-> (SessionCalendar, CalendarEvidence) from a JSON file
    ``{"source": str, "fetched_at_utc": ISO with offset, "rows": [{"date", "open", "close"}, ...]}``."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ConversionRefused("calendar file unreadable: %s" % type(exc).__name__)
    if not isinstance(raw, dict):
        raise ConversionRefused("calendar file must be a JSON object with source, fetched_at_utc and rows")
    source, fetched, rows = raw.get("source"), raw.get("fetched_at_utc"), raw.get("rows")
    if not isinstance(source, str) or not source.strip():
        raise ConversionRefused("calendar file has no source")
    if not isinstance(rows, list) or not rows:
        raise ConversionRefused("calendar file has no rows")
    fetched = _aware(fetched, "calendar fetched_at_utc")
    try:
        cal = calendar_from_rows(rows, source="%s fetched %s" % (source.strip(), fetched.isoformat()))
    except ValueError as exc:
        raise ConversionRefused("calendar rows rejected: %s" % exc)
    sha = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    return cal, CalendarEvidence(cal.name, source.strip(), fetched.isoformat(), cal.coverage_start.isoformat(),
                                 cal.coverage_end.isoformat(), len(rows), sha)


def convert_frames(per_tf_raw: Mapping[str, pd.DataFrame], cal, cal_evidence: CalendarEvidence, *,
                   asof_utc: Any, source: str) -> Tuple[Dict[str, pd.DataFrame], Dict[str, Any]]:
    """Raw M16 frames (one per timeframe) -> close-stamped COMPLETED frames + evidence (see the module doc)."""
    asof = _aware(asof_utc, "asof_utc")
    out, per_tf = {}, {}
    for tf in per_tf_raw:
        df = per_tf_raw[tf]
        if df is None or len(df) == 0:
            raise ConversionRefused("%s %s: no bars" % (source, tf))
        conv = detect_convention(df["ts_utc"], tf, cal)
        if conv.uncovered:
            raise ConversionRefused("%s %s: %d rows outside the calendar's coverage %s..%s"
                                    % (source, tf, conv.uncovered, cal_evidence.coverage_start,
                                       cal_evidence.coverage_end))
        if conv.convention not in (START, CLOSE):
            raise ConversionRefused("%s %s: timestamp convention is %s (start evidence %d, close evidence %d)"
                                    % (source, tf, conv.convention, conv.start_evidence, conv.close_evidence))
        frame, rep = to_close_stamped(df, tf, cal, conv.convention)
        if rep.dropped_uncovered or rep.dropped_inconsistent:
            raise ConversionRefused("%s %s: %d uncovered / %d inconsistent rows" % (
                source, tf, rep.dropped_uncovered, rep.dropped_inconsistent))
        closes = pd.to_datetime(frame["ts_utc"], utc=True)
        after = int((closes > asof).sum())          # forming at asof, or later sessions in the store
        frame = frame.loc[(closes <= asof).to_numpy()].reset_index(drop=True)
        if len(frame) == 0:
            raise ConversionRefused("%s %s: no completed bar at or before %s" % (source, tf, asof.isoformat()))
        out[tf] = frame
        per_tf[tf] = {"convention": conv.as_dict(), "conversion": rep.as_dict(), "after_asof_dropped": after,
                      "n_out": int(len(frame)), "first_close_utc": pd.Timestamp(frame["ts_utc"].iloc[0]).isoformat(),
                      "last_close_utc": pd.Timestamp(frame["ts_utc"].iloc[-1]).isoformat()}
    from bot.backtesting.mtf_context import refuse_start_stamped
    refuse_start_stamped(out, "converted %s" % source)        # the unchanged production guard, as a last check
    evidence = {"method": METHOD, "source": source, "asof_utc": asof.isoformat(),
                "calendar": asdict(cal_evidence), "per_tf": per_tf}
    return out, evidence


# ── M17 multi-timeframe ──────────────────────────────────────────────────────

def load_m17_multi_tf(cfg, timeframes: Sequence[str], cal, cal_evidence: CalendarEvidence, *, asof_utc):
    """M17.A per-timeframe loads (every M16 integrity gate fires as in ``load_multi_tf_bars``), then the
    conversion. Returns a ``MultiTfBars`` carrying its conversion evidence."""
    from bot.backtesting.data_loader import MultiTfBars, load_backtest_bars
    raw, coverage, warnings = {}, {}, []
    for tf in dict.fromkeys(timeframes):
        bars, cov, w = load_backtest_bars(replace(cfg, request=replace(cfg.request, timeframe=tf)))
        raw[tf], coverage[tf] = bars, cov
        warnings.extend(w)
    converted, evidence = convert_frames(raw, cal, cal_evidence, asof_utc=asof_utc,
                                         source="M17 %s" % cfg.request.symbol)
    return MultiTfBars(symbol=cfg.request.symbol, requested_timeframes=tuple(dict.fromkeys(timeframes)),
                       per_tf_bars=converted, per_tf_coverage=coverage, warnings=warnings,
                       allow_partial_tfs=False, conversion=evidence)


def run_m17(cfg, *, calendar_file: str, asof_utc):
    """One M17 multi-timeframe backtest on converted M16 bars."""
    from bot.backtesting import runner
    from bot.backtesting.strategy import MultiTimeframeStrategy, get_strategy
    strategy = get_strategy(cfg.strategy.name, cfg.strategy.params)
    if not isinstance(strategy, MultiTimeframeStrategy):
        raise ConversionRefused("the converted path is for multi-timeframe strategies; %r is single-timeframe"
                                % cfg.strategy.name)
    cal, ev = load_calendar_file(calendar_file)
    mtf = load_m17_multi_tf(cfg, strategy.params["timeframes"], cal, ev, asof_utc=asof_utc)
    return runner.run(cfg, _multi_tf_bars=mtf), mtf.conversion


# ── M18 dataset build ────────────────────────────────────────────────────────

def build_m18_dataset(argv: Sequence[str], *, calendar_file: str, asof_utc) -> int:
    """``bot.ml.cli`` ``build-dataset`` on converted M16 bars. ``argv`` are the M18 CLI arguments (they must
    name the ``build-dataset`` command). The conversion evidence is written next to the dataset."""
    from bot.ml import cli as M18
    args = M18.build_parser().parse_args(list(argv))
    if getattr(args, "command", None) != "build-dataset":
        raise ConversionRefused("only the M18 build-dataset command takes converted bars")
    if getattr(args, "dry_run", False):
        raise ConversionRefused("--dry-run loads no bars; run it on the default path")
    cal, ev = load_calendar_file(calendar_file)
    adjusted = bool(getattr(args, "adjusted", False))
    evidence: Dict[str, Any] = {}

    def provider(symbol, timeframes):
        from bot.ml.dataset import m16_loader
        raw = {tf: m16_loader.load_bars(symbol, tf, adjusted=adjusted) for tf in timeframes}
        frames, evidence["result"] = convert_frames(raw, cal, ev, asof_utc=asof_utc, source="M18 %s" % symbol)
        return frames

    rc = M18.main(list(argv), _bars_provider=provider)
    if rc == 0 and getattr(args, "output", None) and evidence:
        out = Path(args.output) / "conversion_evidence.json"
        out.write_text(json.dumps(dict(evidence["result"], adjusted=adjusted), sort_keys=True, indent=1))
    return rc


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m research.dataqa.converted",
                                 description="Run M17 multi-TF backtests or M18 dataset builds on converted M16 bars.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("m17", help="one M17 multi-timeframe backtest")
    a.add_argument("--config", required=True)
    a.add_argument("--calendar", required=True)
    a.add_argument("--asof", required=True, help="ISO time with offset; later bars count as forming")
    a.add_argument("--output-dir", required=True)
    b = sub.add_parser("m18", help="M18 build-dataset; pass the M18 arguments after --")
    b.add_argument("--calendar", required=True)
    b.add_argument("--asof", required=True)
    b.add_argument("m18_args", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "m17":
            from bot.backtesting.config import parse_config_file
            from bot.backtesting.output import write_results
            cfg = parse_config_file(args.config)
            result, evidence = run_m17(cfg, calendar_file=args.calendar, asof_utc=args.asof)
            run_dir = write_results(result, cfg, Path(args.output_dir))
            Path(run_dir, "conversion_evidence.json").write_text(json.dumps(evidence, sort_keys=True, indent=1))
            print(json.dumps({"run_dir": str(run_dir), "method": METHOD}))
            return 0
        rest = [x for x in args.m18_args if x != "--"]
        return build_m18_dataset(rest, calendar_file=args.calendar, asof_utc=args.asof)
    except ConversionRefused as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["METHOD", "ConversionRefused", "CalendarEvidence", "load_calendar_file", "convert_frames",
           "load_m17_multi_tf", "run_m17", "build_m18_dataset", "main"]
