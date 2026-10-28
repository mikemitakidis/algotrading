#!/usr/bin/env python3
"""M21 WP6 — completed-candle scanner (C01 §11, decision D-4), T-N matrix.

Deterministic fixtures only. Proves, for 15m, 1H (clock-hour and
session-open grids), 4H (known and unknown base grid) and 1D, at pre-open,
first 15 minutes, mid-session, the final partial hour, after the close, a
weekend, a holiday, an early-close day (before and after the early close)
and both DST transition weeks:

  * which bar is selected and its close; the in-progress row is excluded;
    extended-hours rows never enter; a bar whose completion cannot be proven
    is never used (fail closed);
  * the completed-bar staleness result (A01 completed basis);
  * the entry / no-entry outcome (session + first-interval rule);
  * the scanner end to end in completed mode (as-of record, decision-time
    timestamp, selection record) and that the default mode is unchanged;
  * parity: the selected bars equal the M17 multi-timeframe snapshot on the
    same bars stamped by their CLOSE (and why start-stamped bars would leak);
  * the explicit raw price basis of the Alpaca provider;
  * the exact, narrow protected-file transitions.
The calendar below is a TEST FIXTURE, not a verified exchange calendar.
"""
import ast
import hashlib
import sys
import types
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import _test_isolation  # noqa: E402  (M21 C5a)
_test_isolation.install()

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from bot.bar_completion import (  # noqa: E402
    COMPLETE, FORMING, OUTSIDE, UNKNOWN, regular_session_only, select_completed,
)
from bot.scoring_truth import ObservationState, TruthPolicy, classify_observation  # noqa: E402
from bot.scoring_truth.evidence_records import session_calendar_from_rows  # noqa: E402
from bot.scoring_truth.sessions import ANCHOR_CLOCK, ANCHOR_SESSION_OPEN  # noqa: E402
from tools.signal_scoring.completed_bars import CompletedBarPolicy, infer_hour_grid  # noqa: E402

REPO = Path(__file__).resolve().parent
UTC = timezone.utc
NY = ZoneInfo("America/New_York")
H2026 = {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
         date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
         date(2026, 11, 26), date(2026, 12, 25)}
EARLY = {date(2026, 11, 27): "13:00", date(2026, 12, 24): "13:00"}


def _rows():
    rows, d = [], date(2026, 1, 1)
    while d <= date(2026, 12, 31):
        if d.weekday() < 5 and d not in H2026:
            rows.append({"date": d.isoformat(), "open": "09:30", "close": EARLY.get(d, "16:00")})
        d += timedelta(days=1)
    return rows


CAL = session_calendar_from_rows(_rows(), coverage_start="2026-01-01",
                                 coverage_end="2026-12-31", source="test fixture",
                                 fetched_at_utc="2026-01-01T00:00:00+00:00")


def Z(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def sessions_before(t, n):
    out, d = [], t.astimezone(NY).date()
    while len(out) < n and d >= CAL.coverage_start:
        if CAL.session_bounds(d) is not None:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def _frame(starts, seed=0):
    rng = np.random.default_rng(seed)
    n = len(starts)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.004, n)))
    return pd.DataFrame({"open": close, "high": close * 1.002, "low": close * 0.998,
                         "close": close, "volume": rng.integers(1e5, 2e5, n).astype(float)},
                        index=pd.DatetimeIndex(starts))


def provider_intraday(t, minutes, *, grid="clock", days=30, extended=True):
    """Provider-like START-stamped bars up to the bar containing ``t`` (in
    progress), incl. extended hours 04:00-20:00 ET when ``extended``."""
    starts = []
    for d in sessions_before(t, days):
        if grid == "session":
            lt, end = datetime.combine(d, time(9, 30), tzinfo=NY), \
                datetime.combine(d, time(16, 0), tzinfo=NY)
        else:
            lt = datetime.combine(d, time(4, 0) if extended else time(9, 0), tzinfo=NY)
            end = datetime.combine(d, time(20, 0) if extended else time(16, 0), tzinfo=NY)
        while lt < end:
            s = lt.astimezone(UTC)
            if s <= t:
                starts.append(s)
            lt += timedelta(minutes=minutes)
    return _frame(starts)


def provider_daily(t, days=80):
    starts = []
    for d in sessions_before(t, days):
        o = CAL.session_bounds(d)[0]
        if o <= t:                                   # today's bar exists once open
            starts.append(datetime.combine(d, time(0), tzinfo=NY).astimezone(UTC))
    return _frame(starts, seed=1)


def resample_4h(df):
    from bot.data import resample_to_4h
    return resample_to_4h(df)


def select(t, tf, *, hour_grid=ANCHOR_CLOCK, grid="clock"):
    """Run the scanner's completed-bar path for one timeframe."""
    pol = CompletedBarPolicy(CAL, t, hour_grid=hour_grid)
    if tf == "1D":
        df = provider_daily(t)
    elif tf == "15m":
        df = provider_intraday(t, 15)
    else:
        df = provider_intraday(t, 60, grid=grid)
    df = regular_session_only(df, "1H" if tf == "4H" else tf, pol.classify)
    if tf == "4H":
        df = resample_4h(df)
    out, sel = select_completed(df, tf, pol.classify)
    return out, sel, pol


# (case, decision time, {tf: (selected start, selected close)}, entry possible)
# tf keys: "15m", "1H" (clock grid), "1Hs" (session-open grid), "4H" (clock
# base grid), "4Hu" (unknown base grid), "1D".
MATRIX = [
    ("pre_open_est", "2026-11-03T14:20:00",
     {"15m": ("2026-11-02T20:45", "2026-11-02T21:00"), "1H": ("2026-11-02T20:00", "2026-11-02T21:00"),
      "1Hs": ("2026-11-02T20:30", "2026-11-02T21:00"), "4H": ("2026-11-02T20:00", "2026-11-02T21:00"),
      "4Hu": ("2026-11-02T20:00", "2026-11-02T21:00"), "1D": ("2026-11-02T05:00", "2026-11-02T21:00")},
     False),
    ("first_15_minutes", "2026-11-03T14:40:00",
     {"15m": ("2026-11-02T20:45", "2026-11-02T21:00"), "1H": ("2026-11-02T20:00", "2026-11-02T21:00"),
      "1Hs": ("2026-11-02T20:30", "2026-11-02T21:00"), "1D": ("2026-11-02T05:00", "2026-11-02T21:00")},
     False),
    ("first_bar_done", "2026-11-03T14:46:00",
     {"15m": ("2026-11-03T14:30", "2026-11-03T14:45"), "1H": ("2026-11-02T20:00", "2026-11-02T21:00"),
      "1Hs": ("2026-11-02T20:30", "2026-11-02T21:00")},
     True),
    ("mid_session", "2026-11-03T17:10:00",
     {"15m": ("2026-11-03T16:45", "2026-11-03T17:00"), "1H": ("2026-11-03T16:00", "2026-11-03T17:00"),
      "1Hs": ("2026-11-03T15:30", "2026-11-03T16:30"), "4H": ("2026-11-03T12:00", "2026-11-03T16:00"),
      "4Hu": ("2026-11-03T12:00", "2026-11-03T17:00"), "1D": ("2026-11-02T05:00", "2026-11-02T21:00")},
     True),
    ("final_partial_hour", "2026-11-03T20:50:00",
     {"15m": ("2026-11-03T20:30", "2026-11-03T20:45"), "1H": ("2026-11-03T19:00", "2026-11-03T20:00"),
      "1Hs": ("2026-11-03T19:30", "2026-11-03T20:30")},
     True),
    ("after_close", "2026-11-03T21:10:00",
     {"15m": ("2026-11-03T20:45", "2026-11-03T21:00"), "1H": ("2026-11-03T20:00", "2026-11-03T21:00"),
      "1Hs": ("2026-11-03T20:30", "2026-11-03T21:00"), "4H": ("2026-11-03T20:00", "2026-11-03T21:00"),
      "1D": ("2026-11-03T05:00", "2026-11-03T21:00")},
     False),
    ("weekend", "2026-11-07T15:00:00",
     {"15m": ("2026-11-06T20:45", "2026-11-06T21:00"), "1D": ("2026-11-06T05:00", "2026-11-06T21:00")},
     False),
    ("holiday", "2026-11-26T16:00:00",
     {"15m": ("2026-11-25T20:45", "2026-11-25T21:00"), "1D": ("2026-11-25T05:00", "2026-11-25T21:00")},
     False),
    ("early_close_before", "2026-11-27T17:50:00",
     {"15m": ("2026-11-27T17:30", "2026-11-27T17:45"), "1H": ("2026-11-27T16:00", "2026-11-27T17:00"),
      "1D": ("2026-11-25T05:00", "2026-11-25T21:00")},
     True),
    ("early_close_after", "2026-11-27T18:10:00",
     {"15m": ("2026-11-27T17:45", "2026-11-27T18:00"), "1H": ("2026-11-27T17:00", "2026-11-27T18:00"),
      "1D": ("2026-11-27T05:00", "2026-11-27T18:00")},
     False),
    ("dst_autumn_first_est_day", "2026-11-02T14:40:00",
     {"15m": ("2026-10-30T19:45", "2026-10-30T20:00"), "1D": ("2026-10-30T04:00", "2026-10-30T20:00")},
     False),
    ("dst_spring_first_edt_day", "2026-03-09T13:40:00",
     {"15m": ("2026-03-06T20:45", "2026-03-06T21:00"), "1D": ("2026-03-06T05:00", "2026-03-06T21:00")},
     False),
    ("dst_spring_first_bar_done", "2026-03-09T13:46:00",
     {"15m": ("2026-03-09T13:30", "2026-03-09T13:45")},
     True),
]


class N1_TNMatrix(unittest.TestCase):
    def test_selected_bar_close_and_in_progress_exclusion(self):
        for name, t, exp, _entry in MATRIX:
            t = Z(t)
            for key, (start, close) in exp.items():
                tf = key[:-1] if key in ("1Hs", "4Hu") else key
                kw = {}
                if key == "1Hs":
                    kw = dict(grid="session", hour_grid=ANCHOR_SESSION_OPEN)
                if key == "4Hu":
                    kw = dict(hour_grid=None)
                with self.subTest(case=name, tf=key):
                    df, sel, _ = select(t, tf, **kw)
                    self.assertIsNotNone(df, sel)
                    self.assertEqual(sel.selected_start, Z(start).isoformat())
                    self.assertEqual(sel.selected_close, Z(close).isoformat())
                    self.assertLessEqual(Z(close), t)                       # never in progress
                    self.assertEqual(df.index[-1], Z(start))                # nothing after it

    def test_staleness_of_the_selected_bars_is_valid(self):
        for name, t, exp, _entry in MATRIX:
            t = Z(t)
            for key, (start, _close) in exp.items():
                if key in ("1Hs", "4Hu"):
                    continue
                pol = CompletedBarPolicy(CAL, t, hour_grid=ANCHOR_CLOCK)
                with self.subTest(case=name, tf=key):
                    o = classify_observation(key, start + ":00+00:00", evaluated_at=t,
                                             calendar=CAL, spec=pol.specs[key],
                                             basis="completed")
                    self.assertEqual(o.state, ObservationState.VALID, o)

    def test_entry_outcome(self):
        for name, t, exp, entry in MATRIX:
            t = Z(t)
            pol = CompletedBarPolicy(CAL, t)
            _df, sel, _ = select(t, "15m")
            ready, why = pol.first_interval_ready(sel.selected_start)
            b = CAL.session_bounds(CAL.local_date(t))
            in_session = b is not None and b[0] <= t < b[1]
            with self.subTest(case=name):
                self.assertEqual(in_session and ready, entry, (why, in_session))

    def test_extended_hours_never_enter_and_unknown_fails_closed(self):
        t = Z("2026-11-03T22:30:00")                               # post-market running
        df = provider_intraday(t, 15)
        pol = CompletedBarPolicy(CAL, t)
        self.assertEqual(pol.classify("15m", Z("2026-11-03T21:15:00")), (OUTSIDE, None))
        self.assertEqual(pol.classify("15m", Z("2026-11-03T13:00:00"))[0], OUTSIDE)
        rth = regular_session_only(df, "15m", pol.classify)
        for ts in rth.index:
            self.assertNotEqual(pol.classify("15m", ts)[0], OUTSIDE)
        self.assertLess(len(rth), len(df))
        out, sel = select_completed(df, "15m", pol.classify)
        self.assertEqual(sel.selected_start, Z("2026-11-03T20:45:00").isoformat())
        # completion that cannot be proven -> no bar
        late = CompletedBarPolicy(CAL, Z("2026-12-31T20:00:00"))
        self.assertEqual(late.classify("1D", Z("2027-01-04T05:00:00"))[0], FORMING)
        self.assertEqual(late.classify("4H", "2026-11-03T13:00:00+00:00")[0], OUTSIDE)
        self.assertEqual(late.classify("1H", "2026-11-03T14:00:00")[0], UNKNOWN)  # naive
        self.assertEqual(late.classify("2H", Z("2026-11-03T14:00:00"))[0], UNKNOWN)
        unknown = lambda tf, ts: (UNKNOWN, None)                     # noqa: E731
        out, sel = select_completed(df, "15m", unknown)
        self.assertIsNone(out)
        self.assertTrue(sel.reason.startswith("completion_unknown_at_"), sel.reason)
        self.assertEqual(sel.dropped_rows, 0)                     # stops, never skips
        boom = lambda tf, ts: 1 / 0                                  # noqa: E731
        self.assertIsNone(select_completed(df, "15m", boom)[0])
        self.assertIsNone(regular_session_only(df, "15m", boom))
        self.assertIsNone(select_completed(object(), "15m", pol.classify)[0])
        outside = lambda tf, ts: (OUTSIDE, None)                     # noqa: E731
        self.assertEqual(select_completed(df, "15m", outside)[1].reason,
                         "no_completed_bar_in_tail")
        with self.assertRaises(TypeError):
            CompletedBarPolicy(object(), t)
        with self.assertRaises(ValueError):
            CompletedBarPolicy(CAL, t, hour_grid="sometimes")

    def test_hour_grid_is_inferred_only_from_unambiguous_observations(self):
        t = Z("2026-11-03T17:10:00")
        clock = provider_intraday(t, 60, days=3)
        sess = provider_intraday(t, 60, grid="session", days=3)
        self.assertEqual(infer_hour_grid(clock.index, CAL), ANCHOR_CLOCK)
        self.assertEqual(infer_hour_grid(sess.index, CAL), ANCHOR_SESSION_OPEN)
        self.assertIsNone(infer_hour_grid(list(clock.index) + list(sess.index), CAL))
        self.assertIsNone(infer_hour_grid([], CAL))
        self.assertIsNone(infer_hour_grid([Z("2026-11-03T14:10:00")], CAL))
        self.assertIsNone(infer_hour_grid(["garbage"], CAL))


# ═════════════════════════════ scanner end to end ══════════════════════════

class _Sent:
    name = "test"

    def get_sentiment(self, sym):
        from bot.sentiment.base import SentimentResult
        return SentimentResult.unavailable("test", "test")


def scan(t, config, *, focus=("AAA",)):
    import bot.scanner as scanner
    from bot.strategy import DEFAULTS
    frames = {"1d": provider_daily(t), "1h": provider_intraday(t, 60),
              "15m": provider_intraday(t, 15)}

    def fetch(f, period, interval):
        return {s: frames[interval].copy() for s in f}
    with mock.patch.object(scanner, "load_strategy", lambda: DEFAULTS), \
            mock.patch.object(scanner, "fetch_bars", fetch), \
            mock.patch.object(scanner, "score_timeframe", lambda i, d, s: 1 if d == "long" else 0), \
            mock.patch.object(scanner, "get_sentiment_mode", lambda: "off"), \
            mock.patch.object(scanner, "get_sentiment_provider", lambda: _Sent()), \
            mock.patch.object(scanner, "apply_sentiment", lambda s, r, m: (s, True)):
        return scanner.scan_cycle(list(focus), config, conn=None, cycle_id=0), frames


class N2_Scanner(unittest.TestCase):
    T = Z("2026-11-03T17:10:00")

    def test_completed_mode_end_to_end(self):
        pol = CompletedBarPolicy(CAL, self.T, hour_grid=ANCHOR_CLOCK)
        (signals, meta), frames = scan(self.T, {"bar_basis": "completed", "bar_completion": pol})
        self.assertTrue(signals)
        exp = {"1D": "2026-11-02T05:00", "4H": "2026-11-03T12:00", "1H": "2026-11-03T16:00",
               "15m": "2026-11-03T16:45"}
        self.assertEqual(meta["bar_asof"]["AAA"], {k: Z(v).isoformat() for k, v in exp.items()})
        self.assertEqual(meta["bar_basis"], "completed")
        sel = meta["bar_selection"]["AAA"]
        self.assertEqual({k: v["selected_start"] for k, v in sel.items()},
                         {k: Z(v).isoformat() for k, v in exp.items()})
        for s in signals:
            self.assertEqual(s["timestamp"], self.T.isoformat())         # decision time
            self.assertFalse(any(k.startswith("bar_") for k in s))
        # the scored price is the selected completed 15m/1D bar's close, never the
        # in-progress row's
        live_last = frames["1d"]["close"].iloc[-1]
        self.assertNotEqual(signals[0]["price"], live_last)
        self.assertAlmostEqual(signals[0]["price"],
                               float(frames["1d"]["close"].loc[Z(exp["1D"])]))
        # A01 completed-basis truth over the scanner's own record is VALID
        for tf, start in meta["bar_asof"]["AAA"].items():
            o = classify_observation(tf, start, evaluated_at=self.T, calendar=CAL,
                                     spec=pol.specs[tf], basis="completed")
            self.assertEqual(o.state, ObservationState.VALID, (tf, o))

    def test_default_mode_unchanged_and_completed_needs_a_policy(self):
        (signals, meta), _ = scan(self.T, {})
        self.assertTrue(signals)
        self.assertEqual(set(meta), {"tfs_available", "tfs_list", "symbols_scanned", "bar_asof"})
        for cfg in ({"bar_basis": "completed"}, {"bar_basis": "completed", "bar_completion": None},
                    {"bar_basis": "latest"}):
            (signals, meta), _ = scan(self.T, cfg)
            self.assertEqual(signals, [])
            self.assertTrue(meta["blocked_bar_basis"])

    def test_no_completed_bar_means_no_timeframe(self):
        # a policy that can prove nothing makes every timeframe unavailable
        class Nothing:
            decision_time_utc = Z("2026-11-03T17:10:00")

            @staticmethod
            def classify(tf, ts):
                return UNKNOWN, None
        (signals, meta), _ = scan(self.T, {"bar_basis": "completed", "bar_completion": Nothing()})
        self.assertEqual(signals, [])
        self.assertEqual(meta.get("bar_asof", {}), {})

    def test_signal_count_report_completed_vs_current(self):
        """C01 §11 review aid: the same fixture scanned both ways. Completed
        mode can only drop or move signals; it never sees a later bar."""
        rows = []
        for t in ("2026-11-03T14:40:00", "2026-11-03T17:10:00", "2026-11-03T21:10:00"):
            t = Z(t)
            pol = CompletedBarPolicy(CAL, t, hour_grid=ANCHOR_CLOCK)
            (cur, _m1), _ = scan(t, {}, focus=("AAA", "BBB"))
            (done, m2), _ = scan(t, {"bar_basis": "completed", "bar_completion": pol},
                                 focus=("AAA", "BBB"))
            for sym, asof in m2["bar_asof"].items():
                for tf, start in asof.items():
                    self.assertLessEqual(Z(start[:19]), t)
            rows.append((t.isoformat(), len(cur), len(done)))
        self.assertEqual(len(rows), 3)


# ═════════════════════════════ M17 parity ═════════════════════════════════

def close_stamped(df, tf, pol):
    """Provider START-stamped rows -> M17-style rows stamped by their CLOSE
    (complete regular-session bars only)."""
    rows = []
    for ts, r in df.iterrows():
        v, close = pol.classify(tf, ts)
        if v == COMPLETE:
            rows.append(dict(ts_utc=close, start=ts, close=r["close"]))
    return pd.DataFrame(rows)


class N3_ReplicaParity(unittest.TestCase):
    def test_selected_bars_equal_the_m17_snapshot_on_close_stamped_bars(self):
        from bot.backtesting.mtf_context import MultiTimeframeContext
        for t in ("2026-11-03T14:46:00", "2026-11-03T17:10:00", "2026-11-03T21:10:00",
                  "2026-11-27T17:50:00", "2026-11-02T15:20:00"):
            t = Z(t)
            pol = CompletedBarPolicy(CAL, t, hour_grid=ANCHOR_CLOCK)
            frames = {"15m": regular_session_only(provider_intraday(t, 15), "15m", pol.classify),
                      "1H": regular_session_only(provider_intraday(t, 60), "1H", pol.classify),
                      "1D": regular_session_only(provider_daily(t), "1D", pol.classify)}
            frames["4H"] = resample_4h(frames["1H"])
            per_tf = {tf: close_stamped(df, tf, pol) for tf, df in frames.items()}
            ctx = MultiTimeframeContext(per_tf, anchor_tf="15m")
            snap = ctx.snapshot_at(pd.Timestamp(t))
            for tf, df in frames.items():
                _out, sel = select_completed(df, tf, pol.classify)
                with self.subTest(t=t.isoformat(), tf=tf):
                    row = per_tf[tf].iloc[snap[tf].idx]
                    self.assertEqual(row["start"].isoformat(), sel.selected_start)
                    self.assertEqual(pd.Timestamp(row["ts_utc"]).isoformat(), sel.selected_close)

    def test_start_stamped_bars_would_leak_into_m17_snapshots(self):
        """Why WP11/WP13 must convert M16's start-stamped rows: the M17 snapshot
        treats ts_utc as the CLOSE, so a start-stamped daily row of the CURRENT
        session (whose final close is not yet known) would be returned."""
        from bot.backtesting.mtf_context import MultiTimeframeContext
        t = Z("2026-11-03T17:10:00")
        daily = provider_daily(t)
        start_stamped = pd.DataFrame({"ts_utc": daily.index, "close": daily["close"].values})
        q = provider_intraday(t, 15)
        ctx = MultiTimeframeContext({"15m": pd.DataFrame({"ts_utc": q.index}),
                                     "1D": start_stamped}, anchor_tf="15m")
        leaked = ctx.snapshot_at(pd.Timestamp(t))["1D"]
        self.assertEqual(start_stamped["ts_utc"].iloc[leaked.idx], Z("2026-11-03T05:00:00"))
        self.assertGreater(CAL.session_bounds(date(2026, 11, 3))[1], t)  # not yet closed


# ═════════════════════════════ provider basis ══════════════════════════════

class N4_ProviderBasis(unittest.TestCase):
    def test_alpaca_requests_state_raw_explicitly(self):
        import bot.providers.alpaca_provider as ap
        self.assertEqual(ap.BAR_PRICE_ADJUSTMENT, "raw")
        seen = []

        class Req:
            def __init__(self, **kw):
                seen.append(kw)

        class TF:
            Day = Hour = "tf"

            def __init__(self, *a):
                pass

        class Client:
            def get_stock_bars(self, req):
                return types.SimpleNamespace(df=pd.DataFrame())
        req_mod = types.ModuleType("alpaca.data.requests")
        req_mod.StockBarsRequest = Req
        tf_mod = types.ModuleType("alpaca.data.timeframe")
        tf_mod.TimeFrame, tf_mod.TimeFrameUnit = TF, types.SimpleNamespace(Minute="m")
        mods = {"alpaca": types.ModuleType("alpaca"), "alpaca.data": types.ModuleType("alpaca.data"),
                "alpaca.data.requests": req_mod, "alpaca.data.timeframe": tf_mod}
        with mock.patch.dict(sys.modules, mods), \
                mock.patch.object(ap.AlpacaProvider, "_client", lambda self: Client()):
            p = ap.AlpacaProvider.__new__(ap.AlpacaProvider)
            p.fetch_bars(["AAA"], "3mo", "1d")
            p.fetch_bars_range("AAA", "1d", date(2026, 1, 2), date(2026, 2, 2))
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(kw.get("adjustment") == "raw" for kw in seen), seen)

    def test_evidence_reads_the_declared_basis(self):
        from tools.signal_scoring.evidence import runtime_price_basis
        self.assertEqual(runtime_price_basis("alpaca"),
                         ("raw", "alpaca_provider requests adjustment=raw"))


# ═══════════════════ evidence first-interval rule + WS2 wiring ═════════════

class N5_EvidenceAndWS2(unittest.TestCase):
    def test_first_interval_rule_blocks_selection(self):
        from tools.signal_scoring.evidence import score_with_evidence
        import test_m21_1_wp5_evidence as W
        ev = "2026-11-03T14:40:00+00:00"
        asof = {"1D": "2026-11-02T05:00:00+00:00", "4H": "2026-11-02T20:00:00+00:00",
                "1H": "2026-11-02T20:00:00+00:00", "15m": "2026-11-02T20:45:00+00:00"}
        ce = W.evidence(evaluated_at_utc=ev, bar_asof={"AAA": asof})
        c = score_with_evidence(W.sig("AAA", timestamp="2026-11-03T14:40:00+00:00"), ce)
        self.assertIn("first_interval_no_completed_bar_this_session", c.block_reasons)
        self.assertFalse(c.selectable)
        ev2 = "2026-11-03T14:46:00+00:00"
        asof2 = dict(asof, **{"15m": "2026-11-03T14:30:00+00:00"})
        ce2 = W.evidence(evaluated_at_utc=ev2, bar_asof={"AAA": asof2})
        c2 = score_with_evidence(W.sig("AAA", timestamp=ev2), ce2)
        self.assertNotIn("first_interval_no_completed_bar_this_session", c2.block_reasons)

    def test_ws2_provider_scans_completed_bars_with_the_verified_calendar(self):
        import os
        import test_m21_1_wp5_evidence as W
        from tools.paper_loop.m21_1extra_d2_3_scheduler import _scanner_provider
        seen = {}

        def fake_scan(focus, config, conn=None, cycle_id=0):
            seen["config"] = config
            return [], {"bar_asof": {}}
        fc, fa, fd = W.stub_fetchers()
        now = Z("2026-11-03T16:05:00")
        probe = {"AAA": provider_intraday(now, 60, days=3)}
        with mock.patch("bot.scanner.scan_cycle", side_effect=fake_scan), \
                mock.patch("bot.universe.active_selection.get_scan_ready_symbols",
                           return_value=["AAA"]), \
                mock.patch("bot.data.fetch_bars",
                           side_effect=lambda s, p, i: probe if i == "1h" else fd(s)), \
                mock.patch("bot.market_clock.calendar_feed.fetch_calendar", side_effect=fc), \
                mock.patch("bot.providers.alpaca_corporate_actions.fetch_price_scale_actions",
                           side_effect=fa), \
                mock.patch.dict(os.environ, {"DATA_PROVIDER": "alpaca"}):
            self.assertEqual(_scanner_provider(5)(now), [])
        cfg = seen["config"]
        self.assertEqual(cfg["bar_basis"], "completed")
        pol = cfg["bar_completion"]
        self.assertIsInstance(pol, CompletedBarPolicy)
        self.assertEqual(pol.decision_time_utc, now)
        self.assertEqual(pol.hour_grid, ANCHOR_CLOCK)

        fc_bad, _fa, _fd = W.stub_fetchers(cal_ok=False)
        with mock.patch("bot.scanner.scan_cycle", side_effect=fake_scan), \
                mock.patch("bot.universe.active_selection.get_scan_ready_symbols",
                           return_value=["AAA"]), \
                mock.patch("bot.data.fetch_bars", side_effect=lambda s, p, i: fd(s)), \
                mock.patch("bot.market_clock.calendar_feed.fetch_calendar", side_effect=fc_bad), \
                mock.patch("bot.providers.alpaca_corporate_actions.fetch_price_scale_actions",
                           side_effect=fa):
            _scanner_provider(5)(now)
        self.assertIsNone(seen["config"]["bar_completion"])        # scanner then emits nothing


# ═════════════════════ protected-file transitions (exact) ═══════════════════

A01_SCANNER_BLOB = "08dc7512f019ae9fa1706dcf7776c2b2036e1ee3"
PROVIDER_BASE_BLOB = "944f3918c0b10a60edca5e68dc3c4d153a1e9a93"


def _git(*args):
    import subprocess
    r = subprocess.run(["git", *args], cwd=str(REPO), capture_output=True, text=True, timeout=20)
    return r.returncode, r.stdout


def _diff_lines(a, b):
    rc, out = _git("diff", "--unified=0", a, b)
    removed = [l[1:].strip() for l in out.splitlines() if l.startswith("-") and not l.startswith("---")]
    added = [l[1:].strip() for l in out.splitlines() if l.startswith("+") and not l.startswith("+++")]
    return rc, [x for x in removed if x], [x for x in added if x]


class N6_ProtectedTransitions(unittest.TestCase):
    def test_scanner_wp6_transition_is_exactly_the_bar_basis_seam(self):
        import _protected_baselines as PB
        e = PB.APPROVED_CHANGES["bot/scanner.py"]
        rc, removed, added = _diff_lines(A01_SCANNER_BLOB, e.to_blob)
        self.assertEqual(rc, 0)
        self.assertEqual(removed, ["now_utc  = datetime.now(timezone.utc).isoformat()"])
        code = [x for x in added if not x.startswith("#")]
        self.assertEqual(code, [
            "from bot.bar_completion import (BASIS_COMPLETED, BASIS_IN_PROGRESS,",
            "regular_session_only, select_completed)",
            "bar_basis  = (config.get('bar_basis', BASIS_IN_PROGRESS)",
            "if isinstance(config, dict) else BASIS_IN_PROGRESS)",
            "completion = config.get('bar_completion') if bar_basis == BASIS_COMPLETED else None",
            "if bar_basis not in (BASIS_IN_PROGRESS, BASIS_COMPLETED) or \\",
            "(bar_basis == BASIS_COMPLETED and completion is None):",
            "log.warning('[CYCLE] bar basis %r without a completion policy - no signals',",
            "bar_basis)",
            "return [], {'tfs_available': 0, 'tfs_list': [], 'symbols_scanned': len(focus),",
            "'bar_basis': bar_basis, 'blocked_bar_basis': True}",
            "bar_selection = {}   # WP6: sym -> {tf_label -> selection} (completed basis)",
            "if completion is not None:     # WP6: regular-session rows only",
            "df = regular_session_only(df, '1H' if do_resample else tf_label,",
            "completion.classify)",
            "if df is None or len(df) == 0:",
            "continue",
            "if completion is not None:",
            "df, sel = select_completed(df, tf_label, completion.classify)",
            "bar_selection.setdefault(sym, {})[tf_label] = vars(sel)",
            "if df is None:",
            "continue",
            "now_utc  = (completion.decision_time_utc.isoformat() if completion is not None",
            "else datetime.now(timezone.utc).isoformat())",
            "if completion is not None:          # WP6: completed-bar selection record",
            "meta['bar_basis'] = bar_basis",
            "meta['bar_selection'] = bar_selection",
        ])
        self.assertIn("WP6", e.approval)
        self.assertEqual(_git("rev-parse", "6242bde81e5bf9fd194be5fbba41c8cb1c558e08:bot/scanner.py")[1]
                         .strip(), A01_SCANNER_BLOB)

    def test_provider_transition_is_exactly_the_explicit_raw_basis(self):
        import _protected_baselines as PB
        e = PB.APPROVED_CHANGES["bot/providers/alpaca_provider.py"]
        self.assertEqual(e.from_blob, PROVIDER_BASE_BLOB)
        self.assertEqual(_git("rev-parse", "e823fe6779deaccc7b8ff7859c17b4dab564b868:"
                              "bot/providers/alpaca_provider.py")[1].strip(), PROVIDER_BASE_BLOB)
        rc, removed, added = _diff_lines(e.from_blob, e.to_blob)
        self.assertEqual(rc, 0)
        self.assertEqual(removed, [])
        code = [x for x in added if not x.startswith("#")]
        self.assertEqual(code, ['BAR_PRICE_ADJUSTMENT = "raw"',
                                "adjustment=BAR_PRICE_ADJUSTMENT,",
                                "adjustment=BAR_PRICE_ADJUSTMENT,"])

    def test_completion_module_stays_out_of_the_scoring_contract(self):
        for rel in ("bot/bar_completion.py", "bot/scanner.py"):
            src = (REPO / rel).read_text()
            self.assertNotIn("scoring_truth", src)
            self.assertNotIn("scanner_bridge", src)
        tree = ast.parse((REPO / "bot/bar_completion.py").read_text())
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertTrue(mods <= {"__future__", "dataclasses", "typing"}, mods)

    def test_main_py_does_not_select_the_completed_basis(self):
        src = (REPO / "main.py").read_text()
        self.assertNotIn("bar_basis", src)
        self.assertNotIn("bar_completion", src)


if __name__ == "__main__":
    unittest.main()
