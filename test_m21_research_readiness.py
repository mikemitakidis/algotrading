"""test_m21_research_readiness.py — M21 WP11 real historical-data readiness tooling.

Pins the real exchange calendar parser, stamp-convention detection, the
start->close conversion that removes the M16->M17 lookahead, calendar-based
coverage, the adjusted-bar policy, liquidity completeness, point-in-time
membership and survivorship, scanner-replica parity on converted bars, and
the read-only CLI. Offline; every input is hand-built or synthetic, and
synthetic reports are labelled as such. Run via
``python _test_isolation.py test_m21_research_readiness``.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from research import boundary
from research.dataqa import readiness as R
from research.dataqa import stamps as ST
from research.dataqa.calendar import NY, calendar_from_rows, synthetic_calendar
from research.economics.model import CorporateActions, SplitEvent

REPO = Path(__file__).resolve().parent
HOL = {date(2026, 11, 26), date(2026, 12, 25)}
EARLY = {date(2026, 11, 27), date(2026, 12, 24)}


def ny_rows(start="2026-10-01", end="2026-12-31"):
    return [{"date": str(d.date()), "open": "09:30", "close": "13:00" if d.date() in EARLY else "16:00"}
            for d in pd.bdate_range(start, end) if d.date() not in HOL]


CAL = calendar_from_rows(ny_rows())
SESS = [d for d in pd.bdate_range("2026-10-01", "2026-12-31").date if d not in HOL]


def bounds(d):
    o, c = CAL.session_bounds(d)
    return pd.Timestamp(o), pd.Timestamp(c)


def frame(ts, close=None, volume=1e6):
    n = len(ts)
    c = np.asarray(close if close is not None else 100 + np.arange(n) * 0.1, dtype=float)
    return pd.DataFrame({"ts_utc": pd.to_datetime(list(ts), utc=True), "open": c, "high": c * 1.01,
                         "low": c * 0.99, "close": c, "volume": np.full(n, float(volume)) if np.isscalar(volume)
                         else np.asarray(volume, dtype=float), "quality_flags": np.zeros(n, dtype=np.int64)})


def yf_daily(days, **kw):
    """yfinance-style daily rows: stamped at local midnight (bar START)."""
    return frame([pd.Timestamp(d).tz_localize(NY).tz_convert("UTC") for d in days], **kw)


def close_daily(days, **kw):
    return frame([bounds(d)[1] for d in days], **kw)


def intraday(days, m, *, stamp="start", ext=False, clock=False):
    ts = []
    step = pd.Timedelta(minutes=m)
    for d in days:
        o, c = bounds(d)
        lo = o - pd.Timedelta(hours=5, minutes=30) if ext else o
        hi = c + pd.Timedelta(hours=4) if ext else c
        if clock:
            lo = lo.floor("%dmin" % m)
        t = lo
        while t < hi:
            ts.append(t if stamp == "start" else min(t + step, hi))
            t += step
    return frame(ts)


class R1_Calendar(unittest.TestCase):
    def test_holidays_early_closes_and_dst(self):
        self.assertIsNone(CAL.session_bounds(date(2026, 11, 26)))
        self.assertEqual(bounds(date(2026, 11, 27))[1], pd.Timestamp("2026-11-27 18:00", tz="UTC"))
        self.assertEqual(bounds(date(2026, 10, 30))[0], pd.Timestamp("2026-10-30 13:30", tz="UTC"))   # EDT
        self.assertEqual(bounds(date(2026, 11, 2))[0], pd.Timestamp("2026-11-02 14:30", tz="UTC"))    # EST

    def test_bad_rows_refused(self):
        ok = {"date": "2026-11-02", "open": "09:30", "close": "16:00"}
        for rows in ([ok, ok], [dict(ok, date="2026-11-01")], [dict(ok, open="10:00")],
                     [dict(ok, close="16:30")], [dict(ok, close="9:00")], [{"date": "x"}], []):
            with self.assertRaises(ValueError, msg=rows):
                calendar_from_rows(rows)

    def test_synthetic_calendar_is_the_research_grid(self):
        s = synthetic_calendar(date(2018, 1, 1), date(2018, 1, 31))
        o, c = s.session_bounds(date(2018, 1, 2))
        self.assertEqual((pd.Timestamp(o).strftime("%H:%M"), pd.Timestamp(c).strftime("%H:%M")), ("14:30", "21:00"))


class R2_Detection(unittest.TestCase):
    D = SESS[20:30]

    def test_daily_conventions(self):
        self.assertEqual(ST.detect_convention(yf_daily(self.D)["ts_utc"], "1D", CAL).convention, ST.START)
        self.assertEqual(ST.detect_convention(close_daily(self.D)["ts_utc"], "1D", CAL).convention, ST.CLOSE)
        mixed = pd.concat([yf_daily(self.D[:3]), close_daily(self.D[5:8])])
        self.assertEqual(ST.detect_convention(mixed["ts_utc"], "1D", CAL).convention, ST.MIXED)

    def test_intraday_conventions_regular_session(self):
        self.assertEqual(ST.detect_convention(intraday(self.D, 15)["ts_utc"], "15m", CAL).convention, ST.START)
        self.assertEqual(ST.detect_convention(intraday(self.D, 15, stamp="close")["ts_utc"], "15m", CAL).convention,
                         ST.CLOSE)

    def test_extended_hours_make_boundary_stamps_neutral(self):
        r = ST.detect_convention(intraday(self.D, 15, ext=True)["ts_utc"], "15m", CAL)
        self.assertEqual(r.convention, ST.UNKNOWN)
        self.assertGreater(r.outside_session, 0)
        clock = ST.detect_convention(intraday(self.D, 60, ext=True, clock=True)["ts_utc"], "1H", CAL)
        self.assertEqual(clock.convention, ST.START)                  # 09:00 ET bars straddle the 09:30 open

    def test_uncovered_rows_counted_and_bad_timeframe_refused(self):
        r = ST.detect_convention(yf_daily([date(2027, 1, 4)])["ts_utc"], "1D", CAL)
        self.assertEqual((r.uncovered, r.convention), (1, ST.UNKNOWN))
        with self.assertRaises(ValueError):
            ST.detect_convention([], "2H", CAL)


class R3_Conversion(unittest.TestCase):
    def test_daily_start_to_close_with_early_close_dst_and_holiday(self):
        days = [date(2026, 10, 30), date(2026, 11, 2), date(2026, 11, 26), date(2026, 11, 27)]
        out, rep = ST.to_close_stamped(yf_daily(days), "1D", CAL, ST.START)
        self.assertEqual(list(out["ts_utc"]), [pd.Timestamp(x, tz="UTC") for x in
                                               ("2026-10-30 20:00", "2026-11-02 21:00", "2026-11-27 18:00")])
        self.assertEqual(list(out["bar_open_ts"].dt.strftime("%H:%M")), ["13:30", "14:30", "14:30"])
        self.assertEqual(rep.dropped_outside_session, 1)              # the Thanksgiving row

    def test_intraday_start_to_close_and_round_trip(self):
        d = [date(2026, 11, 27)]
        out, _ = ST.to_close_stamped(intraday(d, 15), "15m", CAL, ST.START)
        self.assertEqual((out["ts_utc"].iloc[0], out["ts_utc"].iloc[-1], len(out)),
                         (pd.Timestamp("2026-11-27 14:45", tz="UTC"), pd.Timestamp("2026-11-27 18:00", tz="UTC"), 14))
        back, rep = ST.to_close_stamped(intraday(d, 15, stamp="close"), "15m", CAL, ST.CLOSE)
        pd.testing.assert_series_equal(back["ts_utc"], out["ts_utc"])
        pd.testing.assert_series_equal(back["bar_open_ts"], out["bar_open_ts"])
        self.assertEqual(rep.dropped_inconsistent, 0)

    def test_extended_hours_removed_and_clock_grid_open_clamped(self):
        d = [date(2026, 11, 3)]
        out, rep = ST.to_close_stamped(intraday(d, 15, ext=True), "15m", CAL, ST.START)
        self.assertEqual(len(out), 26)
        self.assertGreater(rep.dropped_outside_session, 0)
        h, _ = ST.to_close_stamped(intraday(d, 60, ext=True, clock=True), "1H", CAL, ST.START)
        self.assertEqual((h["bar_open_ts"].iloc[0], h["ts_utc"].iloc[0]),
                         (pd.Timestamp("2026-11-03 14:30", tz="UTC"), pd.Timestamp("2026-11-03 15:00", tz="UTC")))
        self.assertEqual(len(h), 7)
        s, _ = ST.to_close_stamped(intraday(d, 60), "1H", CAL, ST.START)   # session-anchored: 15:30 ET bar
        self.assertEqual(s["ts_utc"].iloc[-1], pd.Timestamp("2026-11-03 21:00", tz="UTC"))   # cut at the close

    def test_utc_grid_4h_bucket_gets_conservative_close(self):
        f = frame([pd.Timestamp("2026-11-03 12:00", tz="UTC"), pd.Timestamp("2026-11-03 16:00", tz="UTC"),
                   pd.Timestamp("2026-11-03 20:00", tz="UTC")])
        out, rep = ST.to_close_stamped(f, "4H", CAL, ST.START)
        self.assertEqual(list(out["ts_utc"].dt.strftime("%H:%M")), ["17:00", "21:00", "21:00"][:2])
        self.assertEqual((rep.inexact_close, rep.duplicate_close), (3, 1))   # 20:00 bucket ends at the 21:00 close too

    def test_inconsistent_mixed_and_duplicate_rows(self):
        _, rep = ST.to_close_stamped(yf_daily(SESS[:3]), "1D", CAL, ST.CLOSE)
        self.assertEqual((rep.dropped_inconsistent, rep.n_out), (3, 0))
        with self.assertRaises(ValueError):
            ST.to_close_stamped(yf_daily(SESS[:3]), "1D", CAL, ST.MIXED)
        twice = pd.concat([yf_daily(SESS[:2]), frame([bounds(SESS[0])[0]])])
        out, rep = ST.to_close_stamped(twice, "1D", CAL, ST.START)
        self.assertEqual((len(out), rep.duplicate_close), (2, 1))

    def test_conversion_removes_the_m16_m17_lookahead(self):
        from bot.backtesting.mtf_context import MultiTimeframeContext
        t = pd.Timestamp("2026-11-03 17:10", tz="UTC")                # Nov 3 session still open
        days = SESS[SESS.index(date(2026, 10, 20)):SESS.index(date(2026, 11, 4)) + 1]
        raw = yf_daily(days)
        q = intraday(days, 15, stamp="close")
        q = q[q["ts_utc"] <= t]
        leak = MultiTimeframeContext({"15m": q, "1D": raw}, anchor_tf="15m").snapshot_at(t)["1D"]
        self.assertEqual(pd.Timestamp(raw["ts_utc"].iloc[leak.idx]).tz_convert(NY).date(), date(2026, 11, 3))
        conv, _ = ST.to_close_stamped(raw, "1D", CAL, ST.START)
        safe = MultiTimeframeContext({"15m": q, "1D": conv}, anchor_tf="15m").snapshot_at(t)["1D"]
        self.assertEqual(pd.Timestamp(conv["ts_utc"].iloc[safe.idx]), pd.Timestamp("2026-11-02 21:00", tz="UTC"))


class R4_Coverage(unittest.TestCase):
    def conv(self, days, tf="1D"):
        f = yf_daily(days) if tf == "1D" else intraday(days, ST.TF_MINUTES[tf])
        return ST.to_close_stamped(f, tf, CAL, ST.START)[0]

    def test_complete_daily_passes_and_holidays_are_not_missing(self):
        c = R.coverage_check(self.conv(SESS[30:70]), "1D", CAL)
        self.assertEqual((c.verdict, c.detail["bars_missing"]), (R.PASS, 0))

    def test_missing_sessions_fail_above_tolerance(self):
        days = SESS[10:70]
        holes = [d for i, d in enumerate(days) if i not in (20, 21)]
        c = R.coverage_check(self.conv(holes), "1D", CAL)
        self.assertEqual((c.verdict, c.detail["sessions_missing"]), (R.FAIL, 2))
        self.assertEqual(R.coverage_check(self.conv(holes), "1D", CAL, max_missing=0.05).verdict, R.PASS)

    def test_early_close_expects_fewer_intraday_bars(self):
        d = [date(2026, 11, 27), date(2026, 11, 30)]
        c = R.coverage_check(self.conv(d, "15m"), "15m", CAL)
        self.assertEqual((c.verdict, c.detail["bars_expected"]), (R.PASS, 14 + 26))
        short = self.conv(d, "15m").iloc[3:]
        self.assertEqual(R.coverage_check(short, "15m", CAL).detail["sessions_short"], 1)

    def test_window_edges_and_uncovered_calendar(self):
        c = R.coverage_check(self.conv(SESS[10:40]), "1D", CAL, window_start=SESS[0], window_end=SESS[60],
                             max_missing=1.0)
        self.assertTrue(any(r.startswith("late_start") for r in c.reasons))
        self.assertTrue(any(r.startswith("early_end") for r in c.reasons))
        u = R.coverage_check(self.conv(SESS[10:40]), "1D", CAL, window_end=date(2027, 3, 1))
        self.assertEqual(u.verdict, R.INCONCLUSIVE)
        self.assertEqual(R.coverage_check(self.conv([])[:0], "1D", CAL).verdict, R.INCONCLUSIVE)

    def test_rows_on_closed_days_fail(self):
        f = pd.DataFrame({"ts_utc": [pd.Timestamp("2026-11-26 21:00", tz="UTC")],
                          "bar_open_ts": [pd.Timestamp("2026-11-26 14:30", tz="UTC")]})
        self.assertEqual(R.coverage_check(f, "1D", CAL).verdict, R.FAIL)

    def test_4h_expected_buckets_per_grid(self):
        est, edt = bounds(date(2026, 11, 2)), bounds(date(2026, 10, 30))
        self.assertEqual(R._expected_per_session(*est, "4H", "utc"), 3)
        self.assertEqual(R._expected_per_session(*edt, "4H", "utc"), 2)
        self.assertEqual(R._expected_per_session(*est, "4H", "session"), 2)
        self.assertEqual(R._expected_per_session(*est, "1H"), 7)


def split_daily(n=60, at=40, ratio=2.0):
    days = SESS[:n]
    c = np.full(n, 100.0)
    v = np.full(n, 1e6)
    c[at:] /= ratio
    v[at:] *= ratio
    f = close_daily(days, close=c, volume=v)
    f["open"] = f["close"]
    return f, days[at]


class R5_Basis(unittest.TestCase):
    def test_adjusted_basis_contradicted_by_a_split_jump(self):
        f, _ = split_daily()
        self.assertEqual(R.basis_check(f, "split_adjusted", CorporateActions(True)).verdict, R.FAIL)

    def test_raw_basis_needs_a_complete_record_that_explains_jumps(self):
        f, ex = split_daily()
        good = CorporateActions(True, splits=(SplitEvent(ex, 2.0),))
        self.assertEqual(R.basis_check(f, "raw", good).verdict, R.PASS)
        self.assertEqual(R.basis_check(f, "raw", CorporateActions(True)).verdict, R.FAIL)
        wrong_ratio = CorporateActions(True, splits=(SplitEvent(ex, 3.0),))
        self.assertEqual(R.basis_check(f, "raw", wrong_ratio).verdict, R.FAIL)
        self.assertEqual(R.basis_check(f, "raw", None).verdict, R.INCONCLUSIVE)
        self.assertEqual(R.basis_check(f, "raw", CorporateActions(False, splits=good.splits)).verdict,
                         R.INCONCLUSIVE)

    def test_adjusted_policy_and_unknown_basis(self):
        clean = close_daily(SESS[:60])
        self.assertEqual(R.basis_check(clean, "all_adjusted", CorporateActions(True)).verdict, R.PASS)
        self.assertEqual(R.basis_check(clean, "split_adjusted", None).verdict, R.INCONCLUSIVE)
        self.assertEqual(R.basis_check(clean, "unknown", CorporateActions(True)).verdict, R.INCONCLUSIVE)
        with self.assertRaises(ValueError):
            R.basis_check(clean, "dividend", None)


class R6_Liquidity(unittest.TestCase):
    def test_volume_completeness(self):
        days = SESS[:60]
        self.assertEqual(R.liquidity_check(close_daily(days)).verdict, R.PASS)
        v = np.full(60, 1e6)
        v[30] = np.nan
        self.assertEqual(R.liquidity_check(close_daily(days, volume=v)).verdict, R.FAIL)
        v = np.full(60, 1e6)
        v[::10] = 0
        self.assertEqual(R.liquidity_check(close_daily(days, volume=v)).verdict, R.FAIL)
        self.assertEqual(R.liquidity_check(close_daily(days[:10])).verdict, R.INCONCLUSIVE)
        self.assertEqual(R.liquidity_check(close_daily(days)[:0]).verdict, R.INCONCLUSIVE)


class R7_Universe(unittest.TestCase):
    W0, W1 = SESS[0], SESS[59]

    def daily(self):
        up = close_daily(SESS[:60], close=100 * np.exp(np.arange(60) * 0.002))
        dead = close_daily(SESS[:30], close=100 * np.exp(-np.arange(30) * 0.01))
        return {"UP": up, "UP2": up.copy(), "DEAD": dead}

    def test_no_membership_or_no_exits_is_inconclusive(self):
        d = self.daily()
        self.assertEqual(R.survivorship_check(None, d, window_start=self.W0, window_end=self.W1).verdict,
                         R.INCONCLUSIVE)
        static = pd.DataFrame({"symbol": ["UP", "UP2"], "start": [self.W0] * 2, "end": [None] * 2})
        self.assertEqual(R.survivorship_check(static, d, window_start=self.W0, window_end=self.W1).verdict,
                         R.INCONCLUSIVE)

    def test_member_without_bars_fails(self):
        m = pd.DataFrame({"symbol": ["UP", "GONE"], "start": [self.W0] * 2, "end": [None, SESS[20]]})
        c = R.survivorship_check(m, self.daily(), window_start=self.W0, window_end=self.W1)
        self.assertEqual((c.verdict, c.detail["members_without_bars"]), (R.FAIL, ["GONE"]))

    def test_exits_with_bars_quantify_the_bias(self):
        m = pd.DataFrame({"symbol": ["UP", "UP2", "DEAD"], "start": [self.W0] * 3, "end": [None, None, SESS[29]]})
        c = R.survivorship_check(m, self.daily(), window_start=self.W0, window_end=self.W1)
        self.assertEqual((c.verdict, c.detail["exited_in_window"]), (R.PASS, ["DEAD"]))
        self.assertGreater(c.detail["survivorship_bias_estimate"], 0)

    def test_point_in_time_membership_masks(self):
        b = {"X": close_daily(SESS[:10])}
        m = pd.DataFrame({"symbol": ["X", "X"], "start": [SESS[2], SESS[7]], "end": [SESS[4], None]})
        self.assertEqual(R.membership_arrays(m, b)["X"].tolist(),
                         [False, False, True, True, True, False, False, True, True, True])


class R8_ParityAndReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rep = R.synthetic_sample()

    def test_synthetic_sample_runs_every_check_and_is_labelled(self):
        r = self.rep
        self.assertEqual(r["origin"], "synthetic")
        self.assertFalse(r["real_data_ready"])
        self.assertTrue(all(v["verdict"] == R.PASS for per in r["symbols"].values() for v in per.values()))
        par = [c for c in r["panel"] if c["name"] == "parity"][0]
        self.assertEqual(par["verdict"], R.PASS)
        self.assertGreaterEqual(par["detail"]["compared"], 30)
        self.assertEqual(par["detail"]["mismatches"], 0)
        self.assertEqual(r["overall"], R.INCONCLUSIVE)                # static synthetic universe: no exits
        self.assertIn("SYNTHETIC SAMPLE", R.render_markdown(r))

    def test_report_is_deterministic(self):
        self.assertEqual(json.dumps(R._jsonable(self.rep), sort_keys=True),
                         json.dumps(R._jsonable(R.synthetic_sample()), sort_keys=True))

    def test_parity_refuses_real_provider_bars_without_bar_opens(self):
        conv = {s: {tf: f.drop(columns=["bar_open_ts"]) for tf, f in per.items()}
                for s, per in self.rep["_converted"].items()}
        c = R.parity_check(conv, provider="yfinance", adjusted=True)
        self.assertEqual(c.verdict, R.FAIL)
        self.assertIn("bar_open_ts", c.reasons[0])
        few = R.parity_check({s: per for s, per in list(self.rep["_converted"].items())[:1]}, provider="synthetic",
                             adjusted=True, min_compared=10**6)
        self.assertEqual(few.verdict, R.INCONCLUSIVE)

    def test_parity_mismatch_fails(self):
        from unittest import mock
        from research.evidence import parity
        bad = parity.ParityResult(n_compared=50, n_match=49, n_mismatch=1, n_known_divergence=0, n_skipped=0,
                                  mismatches=[{"candidate_id": "C-x", "why": "price"}])
        with mock.patch.object(parity, "check", return_value=bad):
            c = R.parity_check(self.rep["_converted"], provider="synthetic", adjusted=True)
        self.assertEqual((c.verdict, c.detail["mismatches"]), (R.FAIL, 1))

    def test_convention_contradiction_and_origin_rules(self):
        chk, use = R.convention_check(close_daily(SESS[:5]), "1D", CAL, ST.START)
        self.assertEqual((chk.verdict, use), (R.FAIL, None))
        chk, use = R.convention_check(intraday(SESS[:2], 15, ext=True), "15m", CAL, None)
        self.assertEqual(chk.verdict, R.INCONCLUSIVE)
        chk, use = R.convention_check(intraday(SESS[:2], 15, ext=True), "15m", CAL, ST.START)
        self.assertEqual((chk.verdict, use), (R.PASS, ST.START))
        with self.assertRaises(ValueError):
            R.assess({}, CAL, origin="demo", provider="x", declared_convention={}, declared_basis="raw")
        self.assertEqual(R.worst([R.PASS, R.INCONCLUSIVE, R.FAIL]), R.FAIL)
        self.assertEqual(R.worst([]), R.INCONCLUSIVE)


class R9_CLI(unittest.TestCase):
    def test_reads_an_m16_store_without_writing_to_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            store = tmp / "store" / "yfinance" / "1D"
            store.mkdir(parents=True)
            f = yf_daily(SESS[:40])
            f["adj_close"] = f["close"]
            f["adjustment_ratio"] = 1.0
            f.to_parquet(store / "AAA.parquet")
            (tmp / "cal.json").write_text(json.dumps(ny_rows()))
            before = hashlib.sha256((store / "AAA.parquet").read_bytes()).hexdigest()
            rc = R.main(["--store", str(tmp / "store"), "--calendar", str(tmp / "cal.json"), "--symbols", "AAA",
                         "--timeframes", "1D", "--out", str(tmp / "out"), "--start", str(SESS[0]),
                         "--end", str(SESS[39])])
            self.assertEqual(rc, 0)
            self.assertEqual(hashlib.sha256((store / "AAA.parquet").read_bytes()).hexdigest(), before)
            rep = json.loads((tmp / "out" / "report.json").read_text())
            self.assertEqual((rep["origin"], rep["declared_basis"]), ("real", "raw"))
            checks = {c["name"]: c["verdict"] for c in rep["symbols"]["AAA"]["1D"]["checks"]}
            self.assertEqual((checks["convention"], checks["coverage"]), (R.PASS, R.PASS))
            self.assertEqual(checks["basis"], R.INCONCLUSIVE)            # raw without a corporate-action record
            self.assertFalse(rep["real_data_ready"])
            self.assertTrue((tmp / "out" / "report.md").exists())

    def test_synthetic_sample_and_argument_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(R.main(["--synthetic-sample", "--out", tmp]), 0)
            self.assertIn("SYNTHETIC SAMPLE", (Path(tmp) / "report.md").read_text())
            with self.assertRaises(SystemExit):
                R.main(["--out", tmp])


class R11_RealDataEntrances(unittest.TestCase):
    """Review of PR #20: the real-data entrances that align timeframes (M17 load_multi_tf_bars, the M18
    dataset CLI loader) refuse M16 start-stamped bars; converted bars pass. One timeframe alone is never
    aligned, so it is not refused."""

    DAYS = SESS[20:25]

    def frames(self, stamp):
        raw = {"1D": yf_daily(self.DAYS), "4H": intraday(self.DAYS, 240), "1H": intraday(self.DAYS, 60),
               "15m": intraday(self.DAYS, 15)}                     # DAYS span the 1 Nov 2026 DST change
        if stamp == "start":
            return raw
        return {tf: ST.to_close_stamped(df, tf, CAL, ST.START)[0] for tf, df in raw.items()}

    def test_evidence_is_exact_for_start_and_zero_after_conversion(self):
        from bot.backtesting.mtf_context import start_stamp_evidence
        start, conv = self.frames("start"), self.frames("close")
        self.assertEqual(start_stamp_evidence(start["1D"]["ts_utc"]), len(self.DAYS))      # local midnight
        for tf in ("4H", "1H", "15m"):
            self.assertEqual(start_stamp_evidence(start[tf]["ts_utc"]), len(self.DAYS), tf)  # the 09:30 bar
        self.assertEqual({d.month for d in self.DAYS}, {10, 11})
        for tf, df in conv.items():
            self.assertEqual(start_stamp_evidence(df["ts_utc"]), 0, tf)

    def test_alignment_refuses_start_stamps_only_across_timeframes(self):
        from bot.backtesting.mtf_context import StartStampedBarsError, refuse_start_stamped
        with self.assertRaises(StartStampedBarsError) as cm:
            refuse_start_stamped(self.frames("start"), "test")
        self.assertIn("15m", str(cm.exception))
        refuse_start_stamped(self.frames("close"), "test")
        refuse_start_stamped({"1D": yf_daily(self.DAYS), "1H": None}, "single timeframe")

    def test_m18_cli_loader_refuses_m16_start_stamps(self):
        from unittest import mock
        from bot.backtesting.mtf_context import StartStampedBarsError
        from bot.ml import cli as M18
        from bot.ml.dataset import m16_loader
        for stamp, ok in (("start", False), ("close", True)):
            bars = self.frames(stamp)
            with mock.patch.object(m16_loader, "load_bars", side_effect=lambda sym, tf, adjusted: bars[tf]):
                if ok:
                    self.assertEqual(set(M18._default_bars_provider("AAA", list(bars))), set(bars))
                else:
                    with self.assertRaises(StartStampedBarsError):
                        M18._default_bars_provider("AAA", list(bars))

    def test_m17_loader_refuses_m16_start_stamps(self):
        from bot.backtesting import data_loader as DL
        src = Path(DL.__file__).read_text()
        i = src.index("def load_multi_tf_bars(")
        self.assertIn("refuse_start_stamped(per_tf_bars", src[i:])


class R10_Boundary(unittest.TestCase):
    def test_research_tree_clean_and_only_sessions_admitted(self):
        self.assertEqual([str(v) for v in boundary.scan_tree(REPO / "research")], [])
        self.assertEqual(boundary.classify("bot.scoring_truth.sessions"), "")
        for mod in ("bot.scoring_truth", "bot.scoring_truth.contract", "bot.scoring_truth.observations",
                    "bot.market_clock"):
            self.assertTrue(boundary.classify(mod), mod)


if __name__ == "__main__":
    unittest.main()
