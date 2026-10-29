"""test_m21_research_converted_inputs.py — M21 E3: the converted-data path for M17 multi-TF and M18 builds.

Fixtures are M16-shaped bars stamped at their START on a real NYSE calendar window (DST change on 1 Nov,
Thanksgiving 26 Nov, early close 27 Nov), served by a patched read-only M16 store. Offline only.
"""
from __future__ import annotations

import inspect
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

import numpy as np
import pandas as pd

import _test_isolation  # noqa: F401  (M21 C5a)

from bot.backtesting import data_loader as DL
from bot.backtesting import runner as RUN
from bot.backtesting.config import parse_config_dict
from bot.backtesting.errors import BacktestError, StrategyError
from bot.backtesting.mtf_context import StartStampedBarsError
from research import boundary
from research.dataqa import converted as CV

REPO = Path(__file__).resolve().parent
NY = "America/New_York"
FIRST, LAST = date(2025, 11, 3), date(2026, 11, 30)          # >= 200 sessions: the M18 per-TF minimum
HOLIDAYS = {date(2025, 11, 27), date(2025, 12, 25), date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
            date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
            date(2026, 11, 26)}
EARLY = {date(2025, 11, 28): "13:00", date(2025, 12, 24): "13:00", date(2026, 11, 27): "13:00"}
ASOF = pd.Timestamp("2026-11-25 18:00", tz="UTC")             # Wed 13:00 New York: today's bars are forming
LONG = {"rsi_min": 30, "rsi_max": 75, "macd_hist_gt": 0.0, "ema_tolerance": 0.005,
        "vwap_dev_min": -0.015, "vol_ratio_min": 0.6}
SHORT = {"rsi_min": 50, "macd_hist_lt": 0.0, "ema_tolerance": 0.005, "vwap_dev_max": 0.015, "vol_ratio_min": 0.6}


def sessions():
    d, out = FIRST, []
    while d <= LAST:
        if d.weekday() < 5 and d not in HOLIDAYS:
            out.append(d)
        d += timedelta(days=1)
    return out


def calendar_rows():
    return [{"date": d.isoformat(), "open": "09:30", "close": EARLY.get(d, "16:00")} for d in sessions()]


def _ny(d, hm):
    return pd.Timestamp("%s %s" % (d.isoformat(), hm), tz=NY).tz_convert("UTC")


def m16_bars():
    """START-stamped regular-session bars per timeframe, as the M16 store holds them."""
    starts = {"15m": [], "1H": [], "4H": [], "1D": []}
    for d in sessions():
        close = _ny(d, EARLY.get(d, "16:00"))
        t = _ny(d, "09:30")
        while t < close:
            starts["15m"].append(t)
            t += pd.Timedelta(minutes=15)
        t = _ny(d, "09:30")
        while t < close:
            starts["1H"].append(t)
            t += pd.Timedelta(hours=1)
        g = _ny(d, "09:30").floor("4h")                          # M16 resamples 4H on the UTC 4-hour grid
        while g < close:
            starts["4H"].append(g)
            g += pd.Timedelta(hours=4)
        starts["1D"].append(pd.Timestamp(d.isoformat(), tz=NY).tz_convert("UTC"))     # local midnight
    out = {}
    for tf, ts in starts.items():
        n = len(ts)
        px = np.linspace(100.0, 140.0, n) + np.sin(np.arange(n) / 7.0)
        out[tf] = pd.DataFrame({"ts_utc": pd.DatetimeIndex(ts), "open": px, "high": px + 0.5, "low": px - 0.5,
                                "close": px + 0.1, "volume": np.linspace(8e5, 1.5e6, n), "quality_flags": 0})
    return out


def coverage(tf, bars):
    return {"symbol": "AAPL", "timeframe": tf, "provider": "yfinance",
            "first_ts_utc": bars["ts_utc"].iloc[0], "last_ts_utc": bars["ts_utc"].iloc[-1],
            "bar_count": len(bars), "missing_count": 0, "duplicate_count": 0, "quality_status": "clean",
            "freshness_status": "fresh", "last_refresh_at_utc": pd.Timestamp("2026-11-30", tz="UTC"),
            "last_refresh_id": "fixture", "provider_limit_note": None, "source_timeframe": tf,
            "derivation_method": "native", "resample_rule_version": None}


def patched_store(raw):
    fake = MagicMock()
    fake.get_coverage = MagicMock(side_effect=lambda symbol, timeframe, *, provider=None:
                                  coverage(timeframe, raw[timeframe]))

    def get_bars(*, symbol, timeframe, start_utc, end_utc, provider, adjusted):
        b = raw[timeframe]
        ts = pd.to_datetime(b["ts_utc"], utc=True)
        return b.loc[((ts >= pd.Timestamp(start_utc)) & (ts <= pd.Timestamp(end_utc))).to_numpy()]
    fake.get_bars = MagicMock(side_effect=get_bars)
    return mock.patch.object(DL, "_m16_store", fake)


def m17_cfg():
    return parse_config_dict({
        "request": {"symbol": "AAPL", "timeframe": "15m", "start": "2026-11-02", "end": "2026-11-25"},
        "data": {"adjusted": True, "provider": "yfinance"},
        "strategy": {"name": "scanner_replica",
                     "params": {"timeframes": ["1D", "4H", "1H", "15m"], "anchor_tf": "15m",
                                "confluence": {"min_valid_tfs": 3}, "long": LONG, "short": SHORT}},
        "execution": {"allow_short": False, "max_position_pct": 1.0}})


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="e3_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cal_path = os.path.join(self.dir, "calendar.json")
        self.write_cal()
        self.cal, self.ev = CV.load_calendar_file(self.cal_path)

    def write_cal(self, **over):
        body = {"source": "alpaca /v2/calendar (fixture)", "fetched_at_utc": "2026-11-30T12:00:00+00:00",
                "rows": calendar_rows()}
        body.update(over)
        Path(self.cal_path).write_text(json.dumps(body))


class C1_CalendarEvidence(Base):
    def test_evidence_is_recorded(self):
        self.assertEqual((self.ev.coverage_start, self.ev.coverage_end), (FIRST.isoformat(), LAST.isoformat()))
        self.assertEqual(self.ev.n_rows, len(sessions()))
        self.assertEqual(len(self.ev.rows_sha256), 64)
        self.assertIn(date(2026, 11, 26), self.cal.holidays)

    def test_refusals(self):
        bad_rows = calendar_rows() + [{"date": "2026-11-28", "open": "09:30", "close": "16:00"}]     # Saturday
        for over in (dict(source=" "), dict(fetched_at_utc="2026-11-30T12:00:00"), dict(rows=[]),
                     dict(rows=bad_rows)):
            self.write_cal(**over)
            with self.assertRaises(CV.ConversionRefused):
                CV.load_calendar_file(self.cal_path)
        Path(self.cal_path).write_text("[]")
        with self.assertRaises(CV.ConversionRefused):
            CV.load_calendar_file(self.cal_path)


class C2_Conversion(Base):
    def convert(self, raw=None, asof=ASOF):
        return CV.convert_frames(raw or m16_bars(), self.cal, self.ev, asof_utc=asof, source="t")

    def test_bars_are_restamped_at_their_real_close_and_forming_bars_dropped(self):
        out, ev = self.convert()
        d = out["1D"].set_index(out["1D"]["ts_utc"].dt.strftime("%Y-%m-%d %H:%M"))
        self.assertIn("2026-10-30 20:00", d.index)                        # EDT close
        self.assertIn("2026-11-02 21:00", d.index)                        # EST close after the DST change
        self.assertNotIn("2026-11-25 21:00", d.index)                     # today's daily bar is forming
        self.assertEqual(ev["per_tf"]["1D"]["after_asof_dropped"], 3)     # 25 Nov (forming), 27 and 30 Nov
        self.assertEqual(ev["per_tf"]["15m"]["after_asof_dropped"], 12 + 14 + 26)   # 25 Nov 13:00-16:00 NY, ...
        self.assertEqual(pd.Timestamp(out["15m"]["ts_utc"].iloc[0]), _ny(FIRST, "09:45"))
        self.assertTrue((pd.to_datetime(out["15m"]["ts_utc"], utc=True) <= ASOF).all())
        self.assertTrue((out["1H"]["ts_utc"] > out["1H"]["bar_open_ts"]).all())
        self.assertEqual(ev["method"], CV.METHOD)
        self.assertEqual(ev["per_tf"]["1D"]["convention"]["convention"], "start")
        self.assertEqual(ev["calendar"]["rows_sha256"], self.ev.rows_sha256)

    def test_early_close_session(self):
        raw = m16_bars()
        out, _ = self.convert(raw, asof=pd.Timestamp("2026-11-30 22:00", tz="UTC"))
        d = out["1D"]["ts_utc"].dt.strftime("%Y-%m-%d %H:%M").tolist()
        self.assertIn("2026-11-27 18:00", d)                              # 13:00 New York
        self.assertNotIn("2026-11-26 21:00", d)                           # no session on Thanksgiving
        last_15m_27 = [t for t in out["15m"]["ts_utc"].dt.strftime("%Y-%m-%d %H:%M") if t.startswith("2026-11-27")]
        self.assertEqual(last_15m_27[-1], "2026-11-27 18:00")

    def test_refusals(self):
        raw = m16_bars()
        mixed = dict(raw, **{"1D": pd.concat([raw["1D"], raw["1D"].tail(1).assign(
            ts_utc=_ny(date(2026, 11, 30), "16:00"))], ignore_index=True)})
        beyond = dict(raw, **{"1D": pd.concat([raw["1D"], raw["1D"].tail(1).assign(
            ts_utc=pd.Timestamp("2026-12-01", tz=NY).tz_convert("UTC"))], ignore_index=True)})
        for bad, needle in ((mixed, "convention is mixed"), (beyond, "outside the calendar's coverage"),
                            (dict(raw, **{"1H": raw["1H"].iloc[0:0]}), "no bars")):
            with self.assertRaises(CV.ConversionRefused) as cm:
                self.convert(bad)
            self.assertIn(needle, str(cm.exception))
        with self.assertRaises(CV.ConversionRefused):
            self.convert(asof=pd.Timestamp("2026-11-25 18:00"))           # naive
        with self.assertRaises(CV.ConversionRefused) as cm:
            self.convert(asof=pd.Timestamp("2025-11-03 13:00", tz="UTC"))  # before the first close
        self.assertIn("no completed bar", str(cm.exception))


class C3_M17(Base):
    def test_converted_backtest_runs_and_the_default_entrance_still_refuses(self):
        raw = m16_bars()
        with patched_store(raw):
            result, ev = CV.run_m17(m17_cfg(), calendar_file=self.cal_path, asof_utc=ASOF)
            with self.assertRaises(StartStampedBarsError):
                DL.load_multi_tf_bars(m17_cfg(), ["1D", "4H", "1H", "15m"])
            with self.assertRaises(StartStampedBarsError):
                RUN.run(m17_cfg())
        self.assertGreater(result.bars_processed, 0)
        self.assertIn("final_equity", result.metrics)
        self.assertEqual(set(ev["per_tf"]), {"1D", "4H", "1H", "15m"})

    def test_runner_accepts_injected_bars_only_with_evidence(self):
        raw = m16_bars()
        with patched_store(raw):
            good = CV.load_m17_multi_tf(m17_cfg(), ["1D", "4H", "1H", "15m"], self.cal, self.ev, asof_utc=ASOF)
        no_ev = replace(good, conversion=None)
        wrong_method = replace(good, conversion=dict(good.conversion, method="other"))
        partial = replace(good, conversion=dict(good.conversion, per_tf={"15m": {}}))
        for bad in (no_ev, wrong_method, partial, replace(good, symbol="MSFT"), {"15m": raw["15m"]}):
            with self.assertRaises(BacktestError):
                RUN.run(m17_cfg(), _multi_tf_bars=bad)
        forged = replace(good, per_tf_bars=dict(raw))                     # start stamps with copied evidence
        with self.assertRaises(StartStampedBarsError):
            RUN.run(m17_cfg(), _multi_tf_bars=forged)
        single = replace(m17_cfg(), strategy=replace(m17_cfg().strategy, name="sma_crossover",
                                                     params={"fast_window": 5, "slow_window": 20}))
        with self.assertRaises(StrategyError):
            RUN.run(single, _multi_tf_bars=good)
        with patched_store(raw):
            with self.assertRaises(CV.ConversionRefused):
                CV.run_m17(single, calendar_file=self.cal_path, asof_utc=ASOF)


class C4_M18(Base):
    ARGS = ["--json", "build-dataset", "--symbol", "AAPL", "--fixture-mode", "--anchor-tf", "1H",
            "--timeframes", "1D,4H,1H,15m"]

    def loader(self, raw):
        return mock.patch("bot.ml.dataset.m16_loader.load_bars",
                          side_effect=lambda symbol, tf, adjusted=True, **kw: raw[tf].copy())

    def test_converted_build_writes_its_evidence_and_the_default_still_refuses(self):
        raw = m16_bars()
        out_dir = os.path.join(self.dir, "ds")
        buf, err = io.StringIO(), io.StringIO()
        with self.loader(raw), redirect_stdout(buf), redirect_stderr(err):
            rc = CV.build_m18_dataset(self.ARGS + ["--output", out_dir], calendar_file=self.cal_path,
                                      asof_utc=ASOF)
        self.assertEqual(rc, 0, err.getvalue() + buf.getvalue())
        ev = json.loads(Path(out_dir, "conversion_evidence.json").read_text())
        self.assertEqual((ev["method"], ev["adjusted"]), (CV.METHOD, False))
        self.assertTrue(Path(out_dir, "manifest.json").exists())
        from bot.ml import cli as M18
        buf2, err2 = io.StringIO(), io.StringIO()
        with self.loader(raw), redirect_stdout(buf2), redirect_stderr(err2):
            rc = M18.main(self.ARGS)
        self.assertNotEqual(rc, 0)
        self.assertIn("stamped at or before the 09:30 New York open", err2.getvalue())

    def test_refusals(self):
        for argv in (["train", "--symbol", "AAPL"], self.ARGS + ["--dry-run"]):
            with self.assertRaises((CV.ConversionRefused, SystemExit)):
                with redirect_stderr(io.StringIO()):
                    CV.build_m18_dataset(argv, calendar_file=self.cal_path, asof_utc=ASOF)
        err = io.StringIO()
        with redirect_stderr(err):
            rc = CV.main(["m18", "--calendar", os.path.join(self.dir, "missing.json"), "--asof", ASOF.isoformat(),
                          "--"] + self.ARGS)
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED: calendar file unreadable", err.getvalue())


class C5_Boundary(unittest.TestCase):
    def test_injection_is_internal_and_never_used_by_production(self):
        p = inspect.signature(RUN.run).parameters["_multi_tf_bars"]
        self.assertEqual((p.kind, p.default), (inspect.Parameter.KEYWORD_ONLY, None))
        self.assertEqual(RUN.CONVERTED_BARS_METHOD, CV.METHOD)
        for top in ("bot", "tools", "dashboard"):
            for f in (REPO / top).rglob("*.py"):
                if f.name == "runner.py" and f.parent.name == "backtesting":
                    continue
                self.assertIsNone(re.search(r"(?<![A-Za-z0-9])_multi_tf_bars\b", f.read_text(errors="ignore")),
                                  str(f))
        cli = (REPO / "bot/backtesting/cli.py").read_text()
        self.assertNotIn("multi-tf-bars", cli)

    def test_default_entrances_keep_their_guards(self):
        src = (REPO / "bot/backtesting/data_loader.py").read_text()
        i = src.index("def load_multi_tf_bars(")
        self.assertIn("refuse_start_stamped(per_tf_bars", src[i:])
        ml = (REPO / "bot/ml/cli.py").read_text()
        j = ml.index("def _default_bars_provider(")
        self.assertIn("refuse_start_stamped(bars", ml[j:j + 1200])

    def test_research_boundary(self):
        self.assertEqual([str(v) for v in boundary.scan_tree(REPO / "research" / "dataqa")], [])


if __name__ == "__main__":
    unittest.main()
