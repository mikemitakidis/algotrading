#!/usr/bin/env python3
"""M21 WP5 — truthful M19 evidence wiring and research-ranked selection.

Deterministic offline fixtures only (stubbed HTTP, temporary M14 fixture DB).
Proves:
  * a verified SessionCalendar is built from Alpaca-shaped trading-day rows
    (holidays, early closes, DST) and anything that does not fit is refused;
  * point-in-time calendar use (live vs replay) and that calendar facts after
    the decision cannot change it;
  * the calendar and corporate-action fetchers are fail-closed, paginate
    completely or report incomplete, and never leak credentials;
  * a corporate-action record is verified only from a complete fetch; an
    undated action counts as inside the window; no detected split is never
    proof of safety;
  * liquidity is measured only from completed daily sessions observed at
    decision time, with coverage and value validation;
  * end to end: scanner candidate -> observed evidence -> M19 score ->
    research rank -> fail-closed selection; a high research rank never
    overrides a data-quality block or a verified M14 block; ranking is
    deterministic; nothing is execution-eligible;
  * the WS2 scanner provider offers only selectable candidates.
The calendars and bars below are TEST FIXTURES, not market data.
"""
import ast
import os
import random
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import _test_isolation  # noqa: E402  (M21 C5a)
_test_isolation.install()

import pandas as pd  # noqa: E402

from bot.market_clock.alpaca_clock import HttpError, HttpResponse  # noqa: E402
from bot.market_clock.calendar_feed import fetch_calendar  # noqa: E402
from bot.market_clock.sources import STATUS_NO_CREDENTIALS  # noqa: E402
from bot.providers.alpaca_corporate_actions import (  # noqa: E402
    CorporateActionsFetch, fetch_price_scale_actions,
)
from bot.scoring_truth import ObservationState, TruthPolicy, classify_observation  # noqa: E402
from bot.scoring_truth.corporate_actions import (  # noqa: E402
    CorporateActionState, classify_corporate_action,
)
from bot.scoring_truth.evidence_records import (  # noqa: E402
    CorporateActionEvent, EvidenceError, calendar_fit_for_decision,
    corporate_action_record_from_events, measure_liquidity, session_calendar_from_rows,
)
from bot.signal_scoring import keys as K  # noqa: E402
from tools.signal_scoring.evidence import (  # noqa: E402
    CycleEvidence, build_cycle_evidence, loop_candidates, rank_candidates,
    research_report, runtime_price_basis, score_with_evidence,
)
from test_m21_1_a01_scoring_truth import approval, store  # noqa: E402

REPO = Path(__file__).resolve().parent
UTC = timezone.utc
NY = ZoneInfo("America/New_York")
HOLIDAYS = {date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26)}
EARLY = {date(2026, 11, 27): "13:00"}
FETCHED = "2026-10-30T12:00:00+00:00"
EV = "2026-11-03T16:05:00+00:00"          # Tue, EST session 14:30-21:00Z
SIG_TS = "2026-11-03T16:00:00+00:00"
ASOF = {"1D": "2026-11-02T05:00:00+00:00", "4H": "2026-11-02T20:00:00+00:00",
        "1H": "2026-11-03T14:30:00+00:00", "15m": "2026-11-03T15:45:00+00:00"}
DONE = TruthPolicy(bar_basis="completed")
FAKE_KEY, FAKE_SECRET = "TESTKEYID0000", "TESTSECRETVALUE0000"


def U(s):
    return datetime.fromisoformat(s)


def cal_rows(start=date(2026, 6, 1), end=date(2026, 11, 30), drop=()):
    rows, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in HOLIDAYS and d not in drop:
            rows.append({"date": d.isoformat(), "open": "09:30", "close": EARLY.get(d, "16:00")})
        d += timedelta(days=1)
    return rows


def make_cal(rows=None, fetched=FETCHED):
    return session_calendar_from_rows(rows if rows is not None else cal_rows(),
                                      coverage_start="2026-06-01", coverage_end="2026-11-30",
                                      source="fixture alpaca rows", fetched_at_utc=fetched)


CAL = make_cal()


def sessions(until=date(2026, 11, 3), start=date(2026, 6, 1)):
    out, d = [], start
    while d <= until:
        if d.weekday() < 5 and d not in HOLIDAYS:
            out.append(d)
        d += timedelta(days=1)
    return out


def daily_bars(symbol="AAA", until=date(2026, 11, 3), split_on=None, base=100.0):
    """(bar_start, close, volume) per session; start = local midnight."""
    rows = []
    for i, d in enumerate(sessions(until)):
        close = base * (1 + 0.0005 * i)
        if split_on is not None and d >= split_on:
            close /= 2.0
        vol = 1_000_000 + 1_000 * i
        start = datetime.combine(d, time(0), tzinfo=NY).astimezone(UTC).isoformat()
        rows.append((start, close, vol))
    return rows


def frame(rows):
    idx = pd.DatetimeIndex([pd.Timestamp(r[0]) for r in rows])
    return pd.DataFrame({"open": [r[1] for r in rows], "high": [r[1] for r in rows],
                         "low": [r[1] for r in rows], "close": [r[1] for r in rows],
                         "volume": [r[2] for r in rows]}, index=idx)


def clean_record(symbol, complete=True):
    lb = datetime(2026, 8, 1, tzinfo=UTC)
    return corporate_action_record_from_events(symbol, [], window_start=lb, window_end=U(EV),
                                               fetch_complete=complete, source="fixture",
                                               calendar=CAL)


def sig(symbol="AAA", direction="long", **kw):
    if direction == "long":
        d = dict(entry_price=100.0, stop_loss=95.0, target_price=115.0)
    else:
        d = dict(entry_price=100.0, stop_loss=105.0, target_price=85.0)
    d.update(timestamp=SIG_TS, symbol=symbol, direction=direction, rsi=62.0, macd_hist=0.9,
             bb_pos=0.6, vwap_dev=0.004, vol_ratio=1.4, valid_count=4, available_tfs=4,
             atr=2.0, price=100.0, avg_volume_20d=500000, strategy_version=1)
    d.update(kw)
    return d


def evidence(symbols=("AAA", "BBB"), **over):
    kw = dict(evaluated_at_utc=EV, calendar=CAL,
              calendar_fit=calendar_fit_for_decision(CAL, fetched_at_utc=FETCHED,
                                                     evaluated_at=EV, live=True),
              price_basis="raw", price_basis_source="fixture: explicit raw request",
              daily_bars={s: daily_bars(s) for s in symbols},
              corporate_actions={s: clean_record(s) for s in symbols},
              bar_asof={s: dict(ASOF) for s in symbols}, policy=DONE)
    kw.update(over)
    return CycleEvidence(**kw)


class Resp:
    def __init__(self, code, body=None, ok_json=True):
        self.r = HttpResponse(status_code=code, json_body=body, ok_json=ok_json)

    def __call__(self, *a, **k):
        return self.r


def creds():
    return mock.patch.dict(os.environ, {"ALPACA_KEY": FAKE_KEY, "ALPACA_SECRET": FAKE_SECRET})


# ═══════════════════════════════ calendar ══════════════════════════════════

class E1_CalendarFromRows(unittest.TestCase):
    def test_holidays_early_closes_and_dst_from_rows(self):
        self.assertEqual(CAL.tz, "America/New_York")
        self.assertTrue(CAL.holidays >= HOLIDAYS)
        self.assertEqual(dict(CAL.early_closes), {date(2026, 11, 27): time(13, 0)})
        self.assertIsNone(CAL.session_bounds(date(2026, 11, 26)))          # Thanksgiving
        self.assertIsNone(CAL.session_bounds(date(2026, 11, 28)))          # Saturday
        o, c = CAL.session_bounds(date(2026, 11, 27))
        self.assertEqual(c, datetime(2026, 11, 27, 18, 0, tzinfo=UTC))     # 13:00 EST
        self.assertEqual(CAL.session_bounds(date(2026, 10, 30))[1],
                         datetime(2026, 10, 30, 20, 0, tzinfo=UTC))        # EDT
        self.assertEqual(CAL.session_bounds(date(2026, 11, 2))[1],
                         datetime(2026, 11, 2, 21, 0, tzinfo=UTC))         # EST
        self.assertIn("fixture alpaca rows", CAL.source)
        self.assertIn("2026-10-30", CAL.source)

    def test_rows_that_do_not_fit_are_refused(self):
        bad = [
            cal_rows() + [{"date": "2026-06-01", "open": "09:30", "close": "16:00"}],
            cal_rows() + [{"date": "2026-06-06", "open": "09:30", "close": "16:00"}],   # Sat
            cal_rows() + [{"date": "2026-12-01", "open": "09:30", "close": "16:00"}],   # range
            [dict(r, open="10:00") if r["date"] == "2026-07-06" else r for r in cal_rows()],
            [dict(r, close="16:30") if r["date"] == "2026-07-06" else r for r in cal_rows()],
            [dict(r, close="9pm") if r["date"] == "2026-07-06" else r for r in cal_rows()],
            [dict(r, date="2026-13-01") if r["date"] == "2026-07-06" else r for r in cal_rows()],
            [],
            ["not a row"],
        ]
        for rows in bad:
            with self.assertRaises(EvidenceError):
                make_cal(rows)
        with self.assertRaises(EvidenceError):
            session_calendar_from_rows(cal_rows(), coverage_start="2026-06-01",
                                       coverage_end="2026-11-30", source=" ",
                                       fetched_at_utc=FETCHED)
        with self.assertRaises(ValueError):                       # naive fetch time
            session_calendar_from_rows(cal_rows(), coverage_start="2026-06-01",
                                       coverage_end="2026-11-30", source="x",
                                       fetched_at_utc="2026-10-30T12:00:00")

    def test_built_calendar_drives_the_a01_completed_basis(self):
        o = classify_observation("1D", ASOF["1D"], evaluated_at=U(EV), calendar=CAL,
                                 basis="completed")
        self.assertEqual((o.state, o.lag_minutes), (ObservationState.VALID, 0.0))
        o = classify_observation("1D", "2026-11-25T05:00:00+00:00",
                                 evaluated_at=U("2026-11-27T18:31:00+00:00"),
                                 calendar=CAL, basis="completed")
        self.assertEqual(o.state, ObservationState.STALE)        # half day closed 18:00Z


class E2_CalendarPointInTime(unittest.TestCase):
    def test_live_and_replay_fitness(self):
        f = lambda **k: calendar_fit_for_decision(CAL, **k)     # noqa: E731
        self.assertTrue(f(fetched_at_utc=FETCHED, evaluated_at=EV, live=True).usable)
        late = f(fetched_at_utc="2026-11-04T00:00:00+00:00", evaluated_at=EV, live=True)
        self.assertFalse(late.usable)
        self.assertIn("after_the_decision", late.reason)
        rep = f(fetched_at_utc="2026-11-04T00:00:00+00:00", evaluated_at=EV, live=False)
        self.assertTrue(rep.usable and rep.retrospective)
        old = f(fetched_at_utc="2026-10-01T00:00:00+00:00", evaluated_at=EV, live=True)
        self.assertFalse(old.usable)
        out = f(fetched_at_utc=FETCHED, evaluated_at="2026-12-20T15:00:00+00:00", live=False)
        self.assertFalse(out.usable)                              # outside coverage
        self.assertFalse(calendar_fit_for_decision(None, fetched_at_utc=FETCHED,
                                                   evaluated_at=EV, live=True).usable)
        self.assertFalse(f(fetched_at_utc="garbage", evaluated_at=EV, live=True).usable)

    def test_calendar_facts_after_the_decision_cannot_change_it(self):
        later_holiday = make_cal(cal_rows(drop={date(2026, 11, 10), date(2026, 11, 4)}))
        for tf, start in ASOF.items():
            a = classify_observation(tf, start, evaluated_at=U(EV), calendar=CAL, basis="completed")
            b = classify_observation(tf, start, evaluated_at=U(EV), calendar=later_holiday,
                                     basis="completed")
            self.assertEqual(a, b, tf)
        bars = daily_bars("AAA")
        self.assertEqual(measure_liquidity(bars, evaluated_at=EV, calendar=CAL),
                         measure_liquidity(bars, evaluated_at=EV, calendar=later_holiday))


# ═══════════════════════════════ fetchers ══════════════════════════════════

class E3_CalendarFeed(unittest.TestCase):
    NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)

    def fetch(self, get):
        with creds():
            return fetch_calendar(date(2026, 6, 1), date(2026, 11, 30), now_utc=self.NOW,
                                  http_get=get)

    def test_ok_rows_and_digest(self):
        seen = {}

        def get(url, *, headers, params, timeout):
            seen.update(url=url, params=params)
            return HttpResponse(200, list(reversed(cal_rows())))
        r = self.fetch(get)
        self.assertTrue(r.ok)
        self.assertEqual(seen["url"], "https://api.alpaca.markets/v2/calendar")
        self.assertEqual(seen["params"], {"start": "2026-06-01", "end": "2026-11-30"})
        self.assertEqual([x["date"] for x in r.rows], sorted(x["date"] for x in r.rows))
        self.assertEqual(r.digest, self.fetch(Resp(200, cal_rows())).digest)
        make_cal(r.rows)                                       # rows build a calendar

    def test_fail_closed_and_secret_safe(self):
        with mock.patch.dict(os.environ, {"ALPACA_KEY": "", "ALPACA_SECRET": ""}):
            r = fetch_calendar(date(2026, 6, 1), date(2026, 11, 30), now_utc=self.NOW,
                               http_get=Resp(200, cal_rows()))
        self.assertEqual((r.ok, r.status), (False, STATUS_NO_CREDENTIALS))

        def boom(*a, **k):
            raise HttpError("ConnectTimeout")
        cases = [Resp(401), Resp(429), Resp(503), Resp(200, None, ok_json=False),
                 Resp(200, []), Resp(200, {"x": 1}), Resp(200, [{"date": "2026-06-01"}]),
                 Resp(200, ["row"]), boom]
        for get in cases:
            r = self.fetch(get)
            self.assertFalse(r.ok)
            self.assertEqual(r.rows, ())
            self.assertNotIn(FAKE_KEY, repr(r))
            self.assertNotIn(FAKE_SECRET, repr(r))
        with creds():
            r = fetch_calendar(date(2026, 6, 1), date(2029, 1, 1), now_utc=self.NOW,
                               http_get=Resp(200, cal_rows()))
        self.assertFalse(r.ok)                                   # range too long


class E4_CorporateActionsFeed(unittest.TestCase):
    NOW = datetime(2026, 11, 3, 16, 5, tzinfo=UTC)

    def fetch(self, get, **kw):
        with creds():
            return fetch_price_scale_actions(["bbb", "AAA"], date(2026, 8, 1),
                                             date(2026, 11, 3), now_utc=self.NOW,
                                             http_get=get, **kw)

    def test_paginates_to_completion(self):
        calls = []
        pages = [
            {"corporate_actions": {"forward_splits": [
                {"symbol": "AAA", "old_rate": 1, "new_rate": 4, "ex_date": "2026-09-10",
                 "process_date": "2026-09-10"}]}, "next_page_token": "p2"},
            {"corporate_actions": {"reverse_splits": [
                {"symbol": "BBB", "old_rate": 10, "new_rate": 1, "process_date": "2026-10-01"}],
                "cash_dividends": [{"symbol": "AAA", "rate": 0.2}]}, "next_page_token": None},
        ]

        def get(url, *, headers, params, timeout):
            calls.append(dict(params))
            return HttpResponse(200, pages[len(calls) - 1])
        r = self.fetch(get)
        self.assertTrue(r.complete)
        self.assertEqual(r.pages, 2)
        self.assertEqual(calls[0]["symbols"], "AAA,BBB")
        self.assertEqual(calls[0]["data_quality"], "all")
        self.assertEqual(calls[0]["limit"], 1000)
        self.assertEqual(set(calls[0]["types"].split(",")),
                         {"forward_split", "reverse_split", "unit_split", "stock_dividend",
                          "spin_off"})
        self.assertNotIn("page_token", calls[0])
        self.assertEqual(calls[1]["page_token"], "p2")
        a, b = r.rows_for("AAA"), r.rows_for("BBB")
        self.assertEqual((a[0].action_type, a[0].ex_date), ("forward_split", date(2026, 9, 10)))
        self.assertEqual((b[0].action_type, b[0].ex_date), ("reverse_split", None))
        self.assertEqual(len(r.rows), 2)                          # cash dividend ignored

    def test_incomplete_and_secret_safe(self):
        endless = Resp(200, {"corporate_actions": {}, "next_page_token": "again"})
        r = self.fetch(endless, max_pages=3)
        self.assertFalse(r.complete)
        self.assertIn("page limit", r.note)
        pages = [{"corporate_actions": {}, "next_page_token": "p2"}]

        def fails_second(url, *, headers, params, timeout):
            if "page_token" in params:
                return HttpResponse(500)
            return HttpResponse(200, pages[0])
        self.assertFalse(self.fetch(fails_second).complete)
        for get in (Resp(403), Resp(200, None, ok_json=False), Resp(200, {"x": 1}),
                    Resp(200, {"corporate_actions": {"forward_splits": [{"old_rate": 1}]}}),
                    Resp(200, {"corporate_actions": {"forward_splits": "nope"}}),
                    Resp(200, {"corporate_actions": {}, "next_page_token": 7})):
            r = self.fetch(get)
            self.assertFalse(r.complete, get)
            self.assertNotIn(FAKE_SECRET, repr(r))
        with mock.patch.dict(os.environ, {"ALPACA_KEY": "", "ALPACA_SECRET": ""}):
            self.assertFalse(fetch_price_scale_actions(["AAA"], date(2026, 8, 1),
                                                       date(2026, 11, 3), now_utc=self.NOW,
                                                       http_get=Resp(200, {})).complete)


class E5_CorporateActionRecord(unittest.TestCase):
    def truth(self, rec, basis="raw", detector=False):
        return classify_corporate_action("AAA", basis, detector_suspected=detector, record=rec,
                                         lookback_start="2026-08-03T13:30:00+00:00",
                                         evaluated_at=U(EV))

    def test_verified_only_from_a_complete_fetch(self):
        self.assertTrue(clean_record("AAA").verified)
        self.assertEqual(self.truth(clean_record("AAA")).state, CorporateActionState.VERIFIED_CLEAN)
        self.assertFalse(clean_record("AAA", complete=False).verified)
        self.assertEqual(self.truth(clean_record("AAA", complete=False)).state,
                         CorporateActionState.UNVERIFIED)
        self.assertEqual(self.truth(clean_record("AAA"), basis="unknown").state,
                         CorporateActionState.UNVERIFIED)
        self.assertEqual(self.truth(None).state, CorporateActionState.UNVERIFIED)  # no record

    def test_events_place_the_action_in_the_window(self):
        lb = datetime(2026, 8, 1, tzinfo=UTC)
        mk = lambda evs: corporate_action_record_from_events(       # noqa: E731
            "AAA", evs, window_start=lb, window_end=U(EV), fetch_complete=True,
            source="fixture", calendar=CAL)
        dated = mk([CorporateActionEvent("AAA", "forward_split", date(2026, 9, 10))])
        self.assertEqual(dated.events_utc, ("2026-09-10T13:30:00+00:00",))   # EDT open
        self.assertEqual(self.truth(dated).state, CorporateActionState.SUSPECTED)
        undated = mk([CorporateActionEvent("AAA", "reverse_split", None)])
        self.assertFalse(undated.verified)                       # cannot be placed in time
        self.assertIn("undated", undated.source)
        self.assertEqual(self.truth(undated).state, CorporateActionState.UNVERIFIED)
        ignored = mk([CorporateActionEvent("AAA", "cash_dividend", date(2026, 9, 10))])
        self.assertEqual(self.truth(ignored).state, CorporateActionState.VERIFIED_CLEAN)
        with self.assertRaises(EvidenceError):
            mk([CorporateActionEvent("BBB", "forward_split", date(2026, 9, 10))])

    def test_no_detected_split_is_not_proof(self):
        self.assertEqual(self.truth(None, detector=False).state, CorporateActionState.UNVERIFIED)
        self.assertEqual(self.truth(clean_record("AAA"), detector=True).state,
                         CorporateActionState.SUSPECTED)


# ═══════════════════════════════ liquidity ═════════════════════════════════

class E6_Liquidity(unittest.TestCase):
    def expected(self, until=date(2026, 11, 2)):
        rows = daily_bars("AAA")
        sess = sessions(until)
        idx = {d: i for i, d in enumerate(sessions())}
        last20 = sess[-20:]
        return sum(rows[idx[d]][1] * rows[idx[d]][2] for d in last20) / 20.0

    def test_measured_from_completed_sessions_only(self):
        r = measure_liquidity(daily_bars("AAA"), evaluated_at=EV, calendar=CAL)
        self.assertTrue(r.measured)
        self.assertEqual(r.sessions_used, 20)
        self.assertAlmostEqual(r.avg_dollar_volume_20d, self.expected(), places=6)
        # today's (in-progress) bar and any later bar never change it
        more = daily_bars("AAA", until=date(2026, 11, 20))
        self.assertEqual(measure_liquidity(more, evaluated_at=EV, calendar=CAL), r)
        # a bad LATER observation (today's forming bar, or later) cannot poison it
        today = more[[i for i, x in enumerate(more) if x[0].startswith("2026-11-03")][0]][0]
        for bad in ((today, float("nan"), 1.0), (today, -1.0, 1.0),
                    ("2026-11-05T05:00:00+00:00", 10.0, -3.0)):
            rows = [x for x in more if x[0] != bad[0]] + [bad]
            self.assertEqual(measure_liquidity(rows, evaluated_at=EV, calendar=CAL), r, bad)

    def test_unknown_instead_of_estimated(self):
        rows = daily_bars("AAA")
        gap = [r for r in rows if not r[0].startswith("2026-10-2")]
        cases = {
            "missing": gap,
            "nan": rows[:-3] + [(rows[-3][0], float("nan"), 1.0)] + rows[-2:],
            "negative": rows[:-3] + [(rows[-3][0], 10.0, -5)] + rows[-2:],
            "closed_day": rows + [("2026-11-26T05:00:00+00:00", 10.0, 1.0)],
            "duplicate": rows + [rows[-5]],
            "short": rows[-10:],
            "bool": rows[:-3] + [(rows[-3][0], True, 1.0)] + rows[-2:],
        }
        for name, bars in cases.items():
            r = measure_liquidity(bars, evaluated_at=EV, calendar=CAL)
            self.assertIsNone(r.avg_dollar_volume_20d, name)
        self.assertIsNone(measure_liquidity(rows, evaluated_at=EV, calendar=None)
                          .avg_dollar_volume_20d)
        zero = [(r[0], r[1], 0) if r[0].startswith("2026-10-30") else r for r in rows]
        z = measure_liquidity(zero, evaluated_at=EV, calendar=CAL)
        self.assertEqual(z.zero_volume_sessions, 1)


# ═════════════════════ scoring, ranking and selection ══════════════════════

class E7_ScoringAndSelection(unittest.TestCase):
    def test_end_to_end_fixture(self):
        """scanner candidate -> observed evidence -> M19 score -> rank -> decision"""
        signals = [sig("BBB", vol_ratio=0.5), sig("AAA")]
        ranked = rank_candidates(signals, evidence())
        self.assertEqual([c.symbol for c in ranked], ["AAA", "BBB"])
        self.assertGreater(ranked[0].research_rank, ranked[1].research_rank)
        for c in ranked:
            self.assertTrue(c.selectable, c.block_reasons)
            self.assertEqual(set(c.accepted_pre_risk),
                             {"missing:risk_preview.risk_authority_status",
                              "missing:ml_context.production_thinness_status",
                              "review:model_readiness_failed",
                              "review:calibration_unavailable"})
            self.assertIs(c.truthful.execution_eligible, False)
            ci_states = c.truthful.truth.observation_states()
            self.assertEqual(set(ci_states.values()), {"VALID"})
            self.assertEqual(c.truthful.truth.corporate_action.state.value, "VERIFIED_CLEAN")
            notes = {n.item: n.status for n in c.evidence}
            self.assertEqual(notes["avg_dollar_volume_20d"], "MEASURED")
            self.assertEqual(notes["split_detector"], "MEASURED")
        rep = research_report(ranked)
        self.assertEqual((rep["selectable"], rep["blocked"]), (2, 0))
        cands, notes = loop_candidates(ranked)
        self.assertEqual([(c.symbol, round(c.score, 6)) for c in cands],
                         [(c.symbol, round(c.research_rank, 6)) for c in ranked])
        self.assertEqual(cands[0].strategy_id, "scanner_v1")

    def test_data_quality_blocks_beat_any_rank(self):
        good = sig("AAA")
        syms = ("AAA", "STALE", "NOLIQ", "SPLT", "CAUNV")
        ce = evidence(
            symbols=syms,
            bar_asof={**{s: dict(ASOF) for s in syms},
                      "STALE": dict(ASOF, **{"15m": "2026-11-03T15:00:00+00:00"})},
            daily_bars={**{s: daily_bars(s) for s in syms if s != "NOLIQ"},
                        "SPLT": daily_bars("SPLT", split_on=date(2026, 10, 20))},
            corporate_actions={**{s: clean_record(s) for s in syms},
                               "CAUNV": clean_record("CAUNV", complete=False)})
        good = sig("AAA", vol_ratio=0.5)                      # lower rank than the blocked
        signals = [sig(s) for s in syms[1:]] + [good]
        order = rank_candidates(signals, ce)
        self.assertEqual(order[0].symbol, "AAA")              # selectable first, any rank
        self.assertLess(order[0].research_rank, max(c.research_rank for c in order[1:]))
        self.assertFalse(any(c.selectable for c in order[1:]))
        ranked = {c.symbol: c for c in order}
        self.assertTrue(ranked["AAA"].selectable)
        expect = {"STALE": "stale_data", "NOLIQ": "missing:liquidity_context.avg_dollar_volume_20d",
                  "SPLT": "adjusted_price_pit_risk",
                  "CAUNV": "missing:advisory_context.adjusted_price_pit_risk"}
        for s, reason in expect.items():
            self.assertFalse(ranked[s].selectable, s)
            self.assertIn(reason, ranked[s].block_reasons, (s, ranked[s].block_reasons))
            self.assertGreaterEqual(ranked[s].research_rank, 0.0)
        adj = score_with_evidence(sig("ADJ"), evidence(symbols=("ADJ",),
                                                         price_basis="total_return_adjusted"))
        self.assertFalse(adj.selectable)
        nocal = score_with_evidence(sig("AAA"), evidence(
            calendar_fit=calendar_fit_for_decision(None, fetched_at_utc=FETCHED,
                                                   evaluated_at=EV, live=True)))
        self.assertFalse(nocal.selectable)
        self.assertTrue(any(r.startswith("missing:data_quality_context") for r in nocal.block_reasons))
        rep = research_report(list(ranked.values()))
        self.assertEqual(rep["selectable"], 1)
        self.assertEqual(rep["blocked"], 4)
        cands, notes = loop_candidates(list(ranked.values()))
        self.assertEqual([c.symbol for c in cands], ["AAA"])
        self.assertEqual(len([n for n in notes if n.startswith("not_selectable")]), 4)

    def test_verified_m14_block_beats_rank_and_allow_is_recorded(self):
        blocked = approval("AAA", "long", at="2026-11-03T16:02:00+00:00", result="block")
        c = score_with_evidence(sig("AAA", rsi=70.0, macd_hist=3.0),
                                evidence(risk_decisions={("AAA", "long"): blocked},
                                         risk_decision_store=store()))
        self.assertFalse(c.selectable)
        self.assertIn("risk_authority_blocked", c.block_reasons)
        allow = approval("AAA", "long", at="2026-11-03T16:02:00+00:00")
        c = score_with_evidence(sig("AAA"), evidence(risk_decisions={("AAA", "long"): allow},
                                                     risk_decision_store=store()))
        self.assertTrue(c.selectable)
        self.assertEqual(c.truthful.truth.risk.state.value, "OK")
        self.assertNotIn("missing:risk_preview.risk_authority_status", c.accepted_pre_risk)

    def test_shorts_and_determinism(self):
        short = score_with_evidence(sig("AAA", "short"), evidence())
        self.assertFalse(short.selectable)
        self.assertIn("short_disabled", short.block_reasons)
        ok = score_with_evidence(sig("AAA", "short"), evidence(), allow_short=True)
        self.assertTrue(ok.selectable, ok.block_reasons)
        syms = ["AAA", "BBB", "CCC", "DDD"]
        signals = [sig(s, atr=2.0 + 0.5 * i) for i, s in enumerate(syms)]
        signals.append(sig("EEE", atr=3.5))                          # tie with DDD
        ce = evidence(symbols=tuple(syms + ["EEE"]))
        base = [(c.symbol, c.research_rank) for c in rank_candidates(signals, ce)]
        rng = random.Random(7)
        for _ in range(5):
            shuffled = list(signals)
            rng.shuffle(shuffled)
            self.assertEqual([(c.symbol, c.research_rank)
                              for c in rank_candidates(shuffled, ce)], base)

    def test_bridge_never_falls_back_to_scanner_liquidity_on_the_evidence_path(self):
        c = score_with_evidence(sig("AAA"), evidence(daily_bars={}))
        self.assertIn("missing:liquidity_context.avg_dollar_volume_20d", c.block_reasons)
        from tools.signal_scoring.scanner_bridge import enrich_signal_with_truth
        ci, _ = enrich_signal_with_truth(sig("AAA"))                 # legacy default unchanged
        self.assertIn(K.LIQ_AVG_DOLLAR_VOLUME_20D, ci.liquidity_context)


# ═══════════════════════ runtime assembly + WS2 wiring ══════════════════════

def stub_fetchers(*, cal_ok=True, ca_complete=True, daily_raises=False):
    def fetch_cal(start, end, *, now_utc):
        get = Resp(200, cal_rows()) if cal_ok else Resp(503)
        with creds():
            return fetch_calendar(date(2026, 6, 1), date(2026, 11, 30),
                                  now_utc=datetime(2026, 10, 30, 12, tzinfo=UTC), http_get=get)

    def fetch_ca(symbols, start, end, *, now_utc):
        return CorporateActionsFetch(ca_complete, (), tuple(symbols), start.isoformat(),
                                     end.isoformat(), now_utc.isoformat(), 1)

    def fetch_daily(symbols):
        if daily_raises:
            raise RuntimeError("provider down")
        return {s: frame(daily_bars(s)) for s in symbols}
    return fetch_cal, fetch_ca, fetch_daily


class E8_BuildCycleEvidence(unittest.TestCase):
    def build(self, provider="alpaca", **kw):
        fc, fa, fd = stub_fetchers(**kw)
        with mock.patch("bot.providers.alpaca_provider.BAR_PRICE_ADJUSTMENT", "raw",
                        create=True):
            return build_cycle_evidence(
                signals=[sig("AAA"), sig("BBB")], meta={"bar_asof": {"AAA": ASOF, "BBB": ASOF}},
                now_utc=U(EV), fetch_daily=fd, fetch_calendar=fc, fetch_actions=fa,
                provider_name=provider, policy=DONE)

    def test_observed_evidence_makes_candidates_selectable(self):
        ce = self.build()
        self.assertTrue(ce.calendar_fit.usable, ce.calendar_fit.reason)
        self.assertEqual(ce.price_basis, "raw")
        self.assertTrue(all(r.verified for r in ce.corporate_actions.values()))
        ranked = rank_candidates([sig("AAA"), sig("BBB")], ce)
        self.assertTrue(all(c.selectable for c in ranked), [c.block_reasons for c in ranked])

    def test_every_missing_source_fails_closed(self):
        for kw in (dict(cal_ok=False), dict(ca_complete=False), dict(daily_raises=True)):
            ce = self.build(**kw)
            ranked = rank_candidates([sig("AAA")], ce)
            self.assertFalse(ranked[0].selectable, kw)
        for prov in ("yfinance", "nonsense"):
            ranked = rank_candidates([sig("AAA")], self.build(provider=prov))
            self.assertFalse(ranked[0].selectable, prov)

    def test_alpaca_basis_requires_the_provider_to_declare_raw(self):
        with mock.patch("bot.providers.alpaca_provider.BAR_PRICE_ADJUSTMENT", None,
                        create=True):
            self.assertEqual(runtime_price_basis("alpaca")[0], "unknown")
        with mock.patch("bot.providers.alpaca_provider.BAR_PRICE_ADJUSTMENT", "raw",
                        create=True):
            self.assertEqual(runtime_price_basis("alpaca")[0], "raw")
        self.assertEqual(runtime_price_basis("yfinance")[0], "total_return_adjusted")


class E9_WS2ProviderWiring(unittest.TestCase):
    def test_provider_offers_only_selectable_candidates_ranked(self):
        from tools.paper_loop.m21_1extra_d2_3_scheduler import _scanner_provider
        fc, fa, fd = stub_fetchers()
        signals = [sig("BBB", vol_ratio=0.5), sig("AAA"), sig("STALE")]
        meta = {"bar_asof": {"AAA": ASOF, "BBB": ASOF,
                             "STALE": dict(ASOF, **{"15m": "2026-11-03T15:00:00+00:00"})}}
        policy_patch = mock.patch("tools.signal_scoring.evidence.TruthPolicy",
                                  lambda: DONE)
        with mock.patch("bot.scanner.scan_cycle", return_value=(signals, meta)), \
                mock.patch("bot.universe.active_selection.get_scan_ready_symbols",
                           return_value=["AAA", "BBB", "STALE"]), \
                mock.patch("bot.data.fetch_bars", side_effect=lambda s, p, i: fd(s)), \
                mock.patch("bot.market_clock.calendar_feed.fetch_calendar", side_effect=fc), \
                mock.patch("bot.providers.alpaca_corporate_actions.fetch_price_scale_actions",
                           side_effect=fa), \
                mock.patch("bot.providers.alpaca_provider.BAR_PRICE_ADJUSTMENT", "raw",
                           create=True), \
                mock.patch.dict(os.environ, {"DATA_PROVIDER": "alpaca"}), policy_patch:
            cands = _scanner_provider(10)(U(EV))
        self.assertEqual([c.symbol for c in cands], ["AAA", "BBB"])
        self.assertGreater(cands[0].score, cands[1].score)

    def test_provider_source_uses_the_evidence_path(self):
        src = (REPO / "tools/paper_loop/m21_1extra_d2_3_scheduler.py").read_text()
        fn = src[src.index("def _scanner_provider"):src.index("def _ibkr_parts")]
        for needle in ("build_cycle_evidence", "rank_candidates", "loop_candidates"):
            self.assertIn(needle, fn)
        self.assertNotIn("final_score_100", fn)


class E10_Safety(unittest.TestCase):
    def test_new_runtime_modules_stay_out_of_the_scoring_contract_and_brokers(self):
        for rel in ("bot/market_clock/calendar_feed.py", "bot/providers/alpaca_corporate_actions.py"):
            src = (REPO / rel).read_text()
            self.assertNotIn("scoring_truth", src)
            self.assertNotIn("scanner_bridge", src)
            mods = {getattr(n, "module", None) or "" for n in ast.walk(ast.parse(src))
                    if isinstance(n, ast.ImportFrom)}
            mods |= {a.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import)
                     for a in n.names}
            for m in mods:
                self.assertFalse(any(b in m for b in ("ib_insync", "brokers", "kill_switch",
                                                      "notifier", "telegram")), (rel, m))

    def test_evidence_module_never_reaches_execution(self):
        src = (REPO / "tools/signal_scoring/evidence.py").read_text()
        tree = ast.parse(src)
        mods = {getattr(n, "module", "") or "" for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom)}
        for m in mods:
            self.assertFalse(any(b in m for b in ("brokers", "ibkr", "risk_authority",
                                                  "kill_switch", "notifier", "journal")), m)
        self.assertNotIn("execution_eligible=True", src)


if __name__ == "__main__":
    unittest.main()
