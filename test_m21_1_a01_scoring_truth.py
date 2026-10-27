#!/usr/bin/env python3
"""M21.1 A01 — truthful scanner -> M19 scoring contract.

Deterministic offline fixtures only (no network, no wall clock in assertions):
stale timestamps, partial timeframes, bad schemas, missing values, split risk,
trading holidays, early closes and DST transitions. Proves:

  * bar closes / freshness are derived per symbol and timeframe from the
    scanner's UTC bar starts and exchange-session semantics;
  * MISSING / INVALID / UNVERIFIED / STALE / INCOMPLETE / VALID are distinct;
  * no gate input is defaulted: unknown inputs are removed and M19 blocks;
  * Risk Authority is 'ok' only from a validated matching decision;
  * a split detector that finds nothing never proves corporate-action safety;
  * direction, components and research ranking are preserved;
  * scoring cannot authorise or reach execution;
  * the scanner change is exactly the approved, metadata-only C5b transition.

The calendars below are TEST FIXTURES, not verified exchange calendars.
"""
import ast
import dataclasses
import hashlib
import math
import socket
import subprocess
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest import mock

import _test_isolation  # noqa: E402  (M21 C5a)
_test_isolation.install()

import numpy as np
import pandas as pd

from bot.scoring_truth import (
    BarOutOfSession, CalendarError, CorporateActionRecord,
    CorporateActionState, ObservationState, PriceBasis, RiskAuthorityState,
    RiskDecisionRef, SessionCalendar, TimeframeSpec, TruthPolicy, UNKNOWN,
    bar_timing, build_truth, check_signal_schema, classify_corporate_action,
    classify_observation, provider_price_basis, suspect_split,
)
from bot.scoring_truth.sessions import ANCHOR_CLOCK, ANCHOR_SESSION_OPEN, BUCKET
from bot.signal_scoring import ScoringProfile, adapter_from_scanner_signal
from bot.signal_scoring import keys as K
from bot.signal_scoring import default_config, score_candidate
from tools.signal_scoring.scanner_bridge import (
    TruthfulScore, enrich_signal, enrich_signal_with_truth, score_signal,
    score_signal_with_truth,
)

REPO = Path(__file__).resolve().parent
UTC = timezone.utc


def U(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC) if "+" not in s else datetime.fromisoformat(s)


# 2026 fixture: Thanksgiving Thu 26 Nov closed, Fri 27 Nov early close 13:00;
# Independence Day observed Fri 3 Jul; DST ends Sun 1 Nov (EDT -> EST) and
# starts Sun 8 Mar (EST -> EDT).
CAL = SessionCalendar(
    name="fixture-us-2026", tz="America/New_York",
    regular_open=time(9, 30), regular_close=time(16, 0),
    coverage_start=date(2026, 1, 1), coverage_end=date(2026, 12, 31),
    holidays=frozenset({date(2026, 7, 3), date(2026, 11, 26), date(2026, 12, 25)}),
    early_closes=((date(2026, 11, 27), time(13, 0)),),
    source="test fixture")
CAL_NO_THANKSGIVING = dataclasses.replace(
    CAL, name="fixture-missing-holiday",
    holidays=frozenset({date(2026, 7, 3), date(2026, 12, 25)}),
    early_closes=())

TS = "2026-10-27T20:50:00+00:00"           # Tue, EDT, 10 min before close
EV = U("2026-10-27T21:00:00+00:00")         # after the 16:00 EDT close
ASOF = {"1D": "2026-10-27T04:00:00+00:00", "4H": "2026-10-27T16:00:00+00:00",
        "1H": "2026-10-27T19:30:00+00:00", "15m": "2026-10-27T19:45:00+00:00"}
LOOKBACK = "2026-07-01T00:00:00+00:00"


def sig(symbol="AAA", direction="long", **kw):
    if direction == "long":
        d = dict(entry_price=100.0, stop_loss=95.0, target_price=115.0)
    else:
        d = dict(entry_price=100.0, stop_loss=105.0, target_price=85.0)
    d.update(timestamp=TS, symbol=symbol, direction=direction, rsi=62.0,
             macd_hist=0.9, bb_pos=0.6, vwap_dev=0.004, vol_ratio=1.4,
             valid_count=4, available_tfs=4, atr=2.0, price=100.0,
             avg_volume_20d=500000)
    d.update(kw)
    return d


def approval(symbol="AAA", side="long", at="2026-10-27T20:55:00+00:00",
             source="m14_risk_authority", outcome="approved", did="dec-1"):
    return RiskDecisionRef(source, did, outcome, symbol, side, at)


def clean_record(symbol="AAA", events=()):
    return CorporateActionRecord(symbol, "2026-01-01T00:00:00+00:00",
                                 "2026-10-28T00:00:00+00:00", tuple(events),
                                 "fixture", True)


def full_evidence(symbol="AAA", side="long", **over):
    kw = dict(evaluated_at_utc=EV, bar_asof=dict(ASOF), calendar=CAL,
              risk_decision=approval(symbol, side), price_basis="raw",
              detector_suspected=False, corporate_action_record=clean_record(symbol),
              lookback_start_utc=LOOKBACK, production_thinness_status="ok")
    kw.update(over)
    return kw


def evidenced_score_signal(signal, *, profile=ScoringProfile.RESEARCH,
                           avg_dollar_volume=None):
    """TEST HELPER for consumer suites (A-path sim, B1 dry-run, WS3 alerts):
    real M19 scoring of a signal's CONTENT with complete A01 fixture evidence
    at the fixed fixture evaluation time. The timestamp is pinned to the
    fixture time so the fixture risk decision is valid for it. Production
    callers have no such evidence source yet and therefore fail closed."""
    s = dict(signal, timestamp=TS)
    side = s.get("direction") if s.get("direction") in ("long", "short") else "long"
    return score_signal(s, profile=profile, avg_dollar_volume=avg_dollar_volume,
                        **full_evidence(str(s.get("symbol")), side))


TRUTH_KEYS = {
    ("data_quality_context", K.DQ_SCHEMA_MATCH),
    ("data_quality_context", K.DQ_STALE_DATA_FLAG),
    ("data_quality_context", K.DQ_DATA_FRESHNESS_MINUTES),
    ("timeframe_context", K.TF_AVAILABLE),
    ("risk_preview", K.RISK_AUTHORITY_STATUS),
    ("ml_context", K.ML_PRICE_ADJUSTMENT_MODE),
    ("advisory_context", K.ADV_ADJUSTED_PRICE_PIT_RISK),
    ("ml_context", K.ML_PRODUCTION_THINNESS_STATUS),
}


def obs(tf, start, ev, cal=CAL, **kw):
    return classify_observation(tf, start, evaluated_at=U(ev) if isinstance(ev, str) else ev,
                                calendar=cal, **kw)


# ═══════════════════════════ sessions / bar close ═══════════════════════════

class T1_SessionsAndBarClose(unittest.TestCase):
    def test_daily_close_shifts_with_dst(self):
        edt = bar_timing(CAL, TimeframeSpec("1D", "daily"), U("2026-10-30T04:00:00+00:00"), EV)
        est = bar_timing(CAL, TimeframeSpec("1D", "daily"), U("2026-11-02T05:00:00+00:00"), EV)
        self.assertEqual(edt.close, U("2026-10-30T20:00:00+00:00"))
        self.assertEqual(est.close, U("2026-11-02T21:00:00+00:00"))
        spring = bar_timing(CAL, TimeframeSpec("1D", "daily"), U("2026-03-09T00:00:00+00:00"), EV)
        self.assertEqual(spring.session_open, U("2026-03-09T13:30:00+00:00"))
        winter = bar_timing(CAL, TimeframeSpec("1D", "daily"), U("2026-03-06T00:00:00+00:00"), EV)
        self.assertEqual(winter.session_open, U("2026-03-06T14:30:00+00:00"))

    def test_daily_index_conventions_resolve_to_same_session(self):
        for start in ("2026-10-27T00:00:00+00:00", "2026-10-27T04:00:00+00:00",
                      "2026-10-27T13:30:00+00:00"):
            self.assertEqual(bar_timing(CAL, TimeframeSpec("1D", "daily"), U(start), EV).close,
                             U("2026-10-27T20:00:00+00:00"), start)

    def test_4h_bucket_close_across_dst(self):
        so = TimeframeSpec("4H", BUCKET, 240, 60, ANCHOR_SESSION_OPEN)
        # EDT: 1H bars 13:30..19:30; bucket 12:00 holds 13:30,14:30,15:30 -> 16:30
        self.assertEqual(bar_timing(CAL, so, U("2026-10-27T12:00:00+00:00"), EV).close,
                         U("2026-10-27T16:30:00+00:00"))
        # EST: bars 14:30..20:30; bucket 20:00 holds 20:30 -> close 21:00
        t = bar_timing(CAL, so, U("2026-11-03T20:00:00+00:00"), U("2026-11-04T00:00:00+00:00"))
        self.assertEqual(t.close, U("2026-11-03T21:00:00+00:00"))
        # EDT has no 20:00 bucket: the session already closed at 20:00
        with self.assertRaises(BarOutOfSession):
            bar_timing(CAL, so, U("2026-10-27T20:00:00+00:00"), EV)
        # clock-anchored base bars (13:00 bar holds 13:30-14:00 data)
        ck = TimeframeSpec("4H", BUCKET, 240, 60, ANCHOR_CLOCK)
        self.assertEqual(bar_timing(CAL, ck, U("2026-10-27T12:00:00+00:00"), EV).close,
                         U("2026-10-27T16:00:00+00:00"))

    def test_4h_unknown_anchor_is_conservative(self):
        unk = TimeframeSpec("4H", BUCKET, 240, 60, None)
        t = bar_timing(CAL, unk, U("2026-10-27T12:00:00+00:00"), EV)
        self.assertFalse(t.exact)
        self.assertEqual(t.close, U("2026-10-27T17:00:00+00:00"))   # >= true 16:30/16:00
        with self.assertRaises(BarOutOfSession):                     # misaligned bucket
            bar_timing(CAL, unk, U("2026-10-27T13:00:00+00:00"), EV)

    def test_early_close_and_holiday(self):
        t = bar_timing(CAL, TimeframeSpec("15m", "intraday", 15),
                       U("2026-11-27T17:45:00+00:00"), U("2026-11-27T19:00:00+00:00"))
        self.assertEqual(t.close, U("2026-11-27T18:00:00+00:00"))   # 13:00 EST
        with self.assertRaises(BarOutOfSession):
            bar_timing(CAL, TimeframeSpec("15m", "intraday", 15),
                       U("2026-11-27T18:00:00+00:00"), U("2026-11-27T19:00:00+00:00"))
        with self.assertRaises(BarOutOfSession):
            bar_timing(CAL, TimeframeSpec("1D", "daily"), U("2026-11-26T05:00:00+00:00"), EV)

    def test_market_now(self):
        self.assertEqual(CAL.market_now(U("2026-10-27T15:00:00+00:00")),
                         U("2026-10-27T15:00:00+00:00"))
        self.assertEqual(CAL.market_now(U("2026-10-31T12:00:00+00:00")),  # Saturday
                         U("2026-10-30T20:00:00+00:00"))
        self.assertEqual(CAL.market_now(U("2026-11-26T18:00:00+00:00")),  # Thanksgiving
                         U("2026-11-25T21:00:00+00:00"))
        self.assertEqual(CAL.market_now(U("2026-10-27T12:00:00+00:00")),  # before open
                         U("2026-10-26T20:00:00+00:00"))

    def test_coverage_and_validation_fail_closed(self):
        with self.assertRaises(CalendarError):
            CAL.market_now(U("2027-01-04T15:00:00+00:00"))
        with self.assertRaises(CalendarError):
            SessionCalendar("x", "Not/AZone", time(9, 30), time(16), date(2026, 1, 1),
                            date(2026, 2, 1))
        with self.assertRaises(ValueError):
            SessionCalendar("x", "America/New_York", time(9, 30), time(16),
                            date(2026, 1, 1), date(2026, 12, 31),
                            holidays=frozenset({date(2026, 11, 26)}),
                            early_closes=((date(2026, 11, 26), time(13)),))
        with self.assertRaises(ValueError):
            CAL.market_now(datetime(2026, 10, 27, 15, 0))                 # naive


# ═══════════════════════════ observation states ═════════════════════════════

class T2_ObservationStates(unittest.TestCase):
    def test_valid_after_close(self):
        for tf, start in ASOF.items():
            o = obs(tf, start, EV)
            self.assertEqual(o.state, ObservationState.VALID, (tf, o))
            self.assertEqual(o.lag_minutes, 0.0)
        o = obs("15m", ASOF["15m"], EV)
        self.assertEqual(o.bar_close_utc, "2026-10-27T20:00:00+00:00")
        self.assertEqual(o.age_minutes, 60.0)

    def test_incomplete_in_session(self):
        ev = "2026-10-27T15:05:00+00:00"
        self.assertEqual(obs("1H", "2026-10-27T14:30:00+00:00", ev).state,
                         ObservationState.INCOMPLETE)
        o = obs("15m", "2026-10-27T14:45:00+00:00", ev)
        self.assertEqual((o.state, o.lag_minutes), (ObservationState.VALID, 5.0))

    def test_stale_when_newer_bars_unobserved(self):
        o = obs("15m", "2026-10-27T14:00:00+00:00", "2026-10-27T15:00:00+00:00")
        self.assertEqual(o.state, ObservationState.STALE)
        self.assertEqual(o.lag_minutes, 45.0)          # newer data since 14:15
        o = obs("1D", "2026-10-26T04:00:00+00:00", "2026-10-27T15:00:00+00:00")
        self.assertEqual((o.state, o.lag_minutes), (ObservationState.STALE, 90.0))
        o = obs("15m", "2026-10-27T14:00:00+00:00", "2026-10-27T14:40:00+00:00")
        self.assertEqual(o.state, ObservationState.VALID)  # 25 min <= 30
        self.assertEqual(obs("15m", "2026-10-27T14:00:00+00:00", "2026-10-27T14:40:00+00:00",
                             max_lag_minutes=20).state, ObservationState.STALE)

    def test_weekend_and_holiday_are_not_stale(self):
        o = obs("15m", "2026-10-30T19:45:00+00:00", "2026-11-01T12:00:00+00:00")
        self.assertEqual(o.state, ObservationState.VALID)
        o = obs("1D", "2026-11-25T05:00:00+00:00", "2026-11-26T18:00:00+00:00")
        self.assertEqual(o.state, ObservationState.VALID)

    def test_calendar_missing_a_holiday_fails_closed(self):
        o = obs("1D", "2026-11-25T05:00:00+00:00", "2026-11-26T18:00:00+00:00",
                cal=CAL_NO_THANKSGIVING)
        self.assertEqual(o.state, ObservationState.STALE)

    def test_dst_week_staleness_uses_local_session(self):
        # Monday after DST ends: session opens 14:30Z, so at 14:00Z Friday's
        # last bar is still the newest data (not stale).
        o = obs("15m", "2026-10-30T19:45:00+00:00", "2026-11-02T14:00:00+00:00")
        self.assertEqual(o.state, ObservationState.VALID)
        o = obs("15m", "2026-10-30T19:45:00+00:00", "2026-11-02T15:30:00+00:00")
        self.assertEqual((o.state, o.lag_minutes), (ObservationState.STALE, 60.0))

    def test_missing_invalid_unverified(self):
        self.assertEqual(obs("1H", None, EV).state, ObservationState.MISSING)
        for bad in ("2026-10-27T19:30:00", "yesterday", 12345, True):
            self.assertEqual(obs("1H", bad, EV).state, ObservationState.INVALID, bad)
        self.assertEqual(obs("1H", "2026-10-27T22:00:00+00:00", EV).state,
                         ObservationState.INVALID)                       # after evaluation
        self.assertEqual(obs("1D", "2026-10-31T04:00:00+00:00", "2026-11-01T00:00:00+00:00").state,
                         ObservationState.INVALID)                       # Saturday bar
        self.assertEqual(obs("1H", "2026-10-27T11:00:00+00:00", EV).state,
                         ObservationState.INVALID)                       # pre-market
        self.assertEqual(obs("1H", ASOF["1H"], EV, cal=None).state,
                         ObservationState.UNVERIFIED)
        self.assertEqual(obs("1H", "2026-12-31T19:30:00+00:00",
                             "2027-01-04T15:00:00+00:00").state, ObservationState.UNVERIFIED)
        self.assertEqual(obs("2H", ASOF["1H"], EV).state, ObservationState.INVALID)

    def test_pandas_and_offset_timestamps(self):
        self.assertEqual(obs("1H", pd.Timestamp("2026-10-27 19:30", tz="UTC"), EV).state,
                         ObservationState.VALID)
        self.assertEqual(obs("1H", "2026-10-27T15:30:00-04:00", EV).bar_start_utc,
                         "2026-10-27T19:30:00+00:00")
        self.assertEqual(obs("1H", pd.Timestamp("2026-10-27 19:30"), EV).state,
                         ObservationState.INVALID)                       # naive pandas


# ═══════════════════════════ corporate actions ══════════════════════════════

class T3_CorporateActions(unittest.TestCase):
    def test_detector(self):
        self.assertIsNone(suspect_split([100.0]))
        self.assertIsNone(suspect_split([100.0, float("nan")]))
        self.assertIsNone(suspect_split([100.0, -1.0]))
        self.assertIs(suspect_split([100.0, 101.0, 99.5]), False)
        self.assertIs(suspect_split([100.0, 50.1]), True)               # 2:1
        self.assertIs(suspect_split([100.0, 80.0]), True)               # 5:4
        self.assertIs(suspect_split([20.0, 200.0]), True)               # 1:10 reverse

    def test_detector_silence_is_not_safety(self):
        for rec in (None, dataclasses.replace(clean_record(), verified=False),
                    clean_record("BBB"),
                    dataclasses.replace(clean_record(), window_start_utc="2026-09-01T00:00:00+00:00")):
            ca = classify_corporate_action("AAA", "raw", detector_suspected=False, record=rec,
                                           lookback_start=LOOKBACK, evaluated_at=EV)
            self.assertEqual(ca.state, CorporateActionState.UNVERIFIED, rec)
        ca = classify_corporate_action("AAA", "raw", detector_suspected=False,
                                       record=clean_record(), lookback_start=LOOKBACK,
                                       evaluated_at=EV)
        self.assertEqual(ca.state, CorporateActionState.VERIFIED_CLEAN)

    def test_suspected_and_adjusted(self):
        ca = classify_corporate_action("AAA", "raw", detector_suspected=True,
                                       record=clean_record(), lookback_start=LOOKBACK,
                                       evaluated_at=EV)
        self.assertEqual(ca.state, CorporateActionState.SUSPECTED)
        ca = classify_corporate_action("AAA", "raw", detector_suspected=False,
                                       record=clean_record(events=("2026-09-15T13:30:00+00:00",)),
                                       lookback_start=LOOKBACK, evaluated_at=EV)
        self.assertEqual(ca.state, CorporateActionState.SUSPECTED)
        ca = classify_corporate_action("AAA", provider_price_basis("yfinance"),
                                       detector_suspected=False, record=None,
                                       lookback_start=LOOKBACK, evaluated_at=EV)
        self.assertEqual(ca.state, CorporateActionState.ADJUSTED)
        self.assertEqual(provider_price_basis("alpaca"), PriceBasis.UNKNOWN)
        self.assertEqual(provider_price_basis("somebody"), PriceBasis.UNKNOWN)

    def test_m19_consequences(self):
        cases = {
            "unverified": dict(corporate_action_record=None),
            "suspected": dict(detector_suspected=True),
            "adjusted": dict(price_basis="total_return_adjusted"),
            "unknown_basis": dict(price_basis=None),
        }
        for name, over in cases.items():
            sc = score_signal(sig(), **full_evidence(**over))
            self.assertEqual(sc.decision_bucket.value, "BLOCKED", name)
        sc = score_signal(sig(), **full_evidence(detector_suspected=True))
        self.assertIn("adjusted_price_pit_risk", sc.hard_gate_failures)
        sc = score_signal(sig(), **full_evidence(corporate_action_record=None))
        self.assertIn("missing_context_key", sc.hard_gate_failures)


# ═══════════════════════════ risk authority ═════════════════════════════════

class T4_RiskAuthority(unittest.TestCase):
    def _state(self, ref, symbol="AAA", side="long"):
        _, tr = enrich_signal_with_truth(sig(symbol, side),
                                         **full_evidence(symbol, side, risk_decision=ref))
        return tr.risk

    def test_no_decision_is_not_ok(self):
        for prof in (ScoringProfile.RESEARCH, ScoringProfile.STRICT):
            ts = score_signal_with_truth(sig(), profile=prof,
                                         **full_evidence(risk_decision=None))
            self.assertEqual(ts.truth.risk.state, RiskAuthorityState.NOT_EVALUATED)
            ci, _ = enrich_signal_with_truth(sig(), **full_evidence(risk_decision=None))
            self.assertNotIn(K.RISK_AUTHORITY_STATUS, ci.risk_preview)
            self.assertEqual(ts.scored.decision_bucket.value, "BLOCKED")
            self.assertIn("missing_context_key", ts.scored.hard_gate_failures)

    def test_forged_references_rejected(self):
        for forged in ("ok", {"outcome": "approved"}, True, ("m14", "1", "approved")):
            self.assertEqual(self._state(forged).state, RiskAuthorityState.UNVERIFIED)

    def test_approval_validation(self):
        self.assertEqual(self._state(approval()).state, RiskAuthorityState.OK)
        bad = {
            "legacy_source": approval(source="legacy_risk_manager"),
            "other_symbol": approval(symbol="BBB"),
            "other_side": approval(side="short"),
            "expired": approval(at="2026-10-27T20:40:00+00:00"),
            "future": approval(at="2026-10-27T21:00:01+00:00"),
            "predates": approval(at="2026-10-27T20:49:59+00:00"),
            "no_id": approval(did=" "),
            "naive_time": approval(at="2026-10-27T20:55:00"),
            "odd_outcome": approval(outcome="ok"),
        }
        for name, ref in bad.items():
            self.assertEqual(self._state(ref).state, RiskAuthorityState.UNVERIFIED, name)

    def test_matching_block_always_blocks(self):
        for ref in (approval(outcome="blocked"),
                    approval(outcome="blocked", source="legacy_risk_manager",
                             at="2026-10-01T00:00:00+00:00")):
            self.assertEqual(self._state(ref).state, RiskAuthorityState.BLOCKED)
            sc = score_signal(sig(), **full_evidence(risk_decision=ref))
            self.assertIn("risk_authority_blocked", sc.hard_gate_failures)
        self.assertEqual(self._state(approval(symbol="ZZZ", outcome="blocked")).state,
                         RiskAuthorityState.UNVERIFIED)


# ═══════════════════════════ schema / missing values ════════════════════════

class T5_SchemaAndMissingValues(unittest.TestCase):
    def test_clean_schema(self):
        c = check_signal_schema(sig())
        self.assertTrue(c.ok, c.errors)
        self.assertEqual(c.missing_features, ())

    def test_bad_values_mark_schema_mismatch(self):
        cases = {
            "bool_vol": dict(vol_ratio=True), "rsi_range": dict(rsi=140.0),
            "atr_zero": dict(atr=0.0), "levels": dict(stop_loss=101.0),
            "valid_count": dict(valid_count=7), "str_price": dict(price="100"),
        }
        for name, kw in cases.items():
            c = check_signal_schema(sig(**kw))
            self.assertFalse(c.ok, name)
            sc = score_signal(sig(**kw), **full_evidence())
            self.assertEqual(sc.decision_bucket.value, "BLOCKED", name)
            ci, tr = enrich_signal_with_truth(sig(**kw), **full_evidence())
            self.assertIs(ci.data_quality_context[K.DQ_SCHEMA_MATCH], False, name)
        # non-finite values: schema mismatch, and M19 refuses to score at all
        # (its provenance digest rejects non-finite floats) — fail closed.
        for kw in (dict(rsi=float("nan")), dict(macd_hist=math.inf)):
            self.assertFalse(check_signal_schema(sig(**kw)).ok, kw)
            ci, _ = enrich_signal_with_truth(sig(**kw), **full_evidence())
            self.assertIs(ci.data_quality_context[K.DQ_SCHEMA_MATCH], False, kw)
            with self.assertRaises(ValueError):
                score_signal(sig(**kw), **full_evidence())

    def test_unparseable_timestamps_fail_closed(self):
        for bad in ("not-a-time", "2026-10-27T20:50:00", "2026-10-27T16:50:00-04:00"):
            self.assertFalse(check_signal_schema(sig(timestamp=bad)).ok, bad)
            with self.assertRaises(ValueError):
                score_signal(sig(timestamp=bad), **full_evidence())

    def test_missing_features_counted_not_invented(self):
        s = sig()
        del s["bb_pos"], s["vwap_dev"]
        ci, tr = enrich_signal_with_truth(s, **full_evidence())
        self.assertEqual(ci.data_quality_context[K.DQ_MISSING_FEATURE_COUNT], 2)
        self.assertNotIn(K.TECH_BB_POS, ci.technical_context)

    def test_missing_levels_do_not_force_risk_preview(self):
        s = sig()
        for k in ("entry_price", "stop_loss", "target_price"):
            del s[k]
        ci, _ = enrich_signal_with_truth(s, **full_evidence())
        self.assertNotIn(K.RISK_PREVIEW_AVAILABLE, ci.risk_preview)
        sc = score_candidate(ci, default_config(profile=ScoringProfile.RESEARCH))
        self.assertIn("missing_context_key", sc.hard_gate_failures)


# ═══════════════════════════ bridge contract ════════════════════════════════

class T6_BridgeContract(unittest.TestCase):
    def test_no_evidence_means_every_truth_key_removed(self):
        ci, tr = enrich_signal_with_truth(sig())
        for block, key in TRUTH_KEYS:
            self.assertNotIn(key, getattr(ci, block), (block, key))
        self.assertEqual({tuple(u.split(".")) for u in tr.unknown_fields}, TRUTH_KEYS)
        for prof in (ScoringProfile.RESEARCH, ScoringProfile.STRICT):
            sc = score_signal(sig(), profile=prof)
            self.assertEqual(sc.decision_bucket.value, "BLOCKED")
            self.assertEqual(sc.final_score_100, 0.0)

    def test_pre_a01_assumption_kwargs_are_rejected(self):
        for kw in (dict(stale=False), dict(schema_match=True),
                   dict(data_freshness_minutes=5.0)):
            with self.assertRaises(TypeError):
                enrich_signal(sig(), **kw)

    def test_full_evidence_measures_everything(self):
        ts = score_signal_with_truth(sig(), **full_evidence())
        self.assertEqual(ts.truth.unknown_fields, ())
        ci, _ = enrich_signal_with_truth(sig(), **full_evidence())
        self.assertEqual(ci.risk_preview[K.RISK_AUTHORITY_STATUS], "ok")
        self.assertEqual(ci.timeframe_context[K.TF_AVAILABLE], ["1D", "4H", "1H", "15m"])
        self.assertEqual(ci.data_quality_context[K.DQ_DATA_FRESHNESS_MINUTES], 0.0)
        self.assertIs(ci.data_quality_context[K.DQ_STALE_DATA_FLAG], False)
        self.assertIs(ci.ml_context[K.ML_ALLOW_ADJUSTED_FOR_ML], False)
        self.assertNotEqual(ts.scored.decision_bucket.value, "BLOCKED")
        self.assertAlmostEqual(ts.scored.final_score_100, ts.research_rank_score, places=5)

    def test_partial_timeframes_measured_per_symbol(self):
        partial = {k: ASOF[k] for k in ("1D", "1H")}
        ci, tr = enrich_signal_with_truth(sig(available_tfs=4),
                                          **full_evidence(bar_asof=partial))
        self.assertEqual(ci.timeframe_context[K.TF_AVAILABLE], ["1D", "1H"])
        self.assertEqual(ci.scanner_context[K.SCAN_AVAILABLE_TIMEFRAMES], 4)  # scanner's own
        self.assertEqual(tr.observation_states()["4H"], "MISSING")
        sc = score_signal(sig(), **full_evidence(bar_asof=partial))
        self.assertIn("insufficient_available_timeframes", sc.hard_gate_failures)

    def test_stale_bars_block_with_measured_lag(self):
        stale = dict(ASOF, **{"15m": "2026-10-27T18:45:00+00:00"})
        ci, tr = enrich_signal_with_truth(sig(), **full_evidence(bar_asof=stale))
        self.assertIs(ci.data_quality_context[K.DQ_STALE_DATA_FLAG], True)
        self.assertEqual(ci.data_quality_context[K.DQ_DATA_FRESHNESS_MINUTES], 60.0)
        sc = score_signal(sig(), **full_evidence(bar_asof=stale))
        self.assertIn("stale_data", sc.hard_gate_failures)

    def test_incomplete_bars_policy(self):
        ev = U("2026-10-27T15:05:00+00:00")
        intraday = {"1D": "2026-10-27T04:00:00+00:00", "4H": "2026-10-27T12:00:00+00:00",
                    "1H": "2026-10-27T14:30:00+00:00", "15m": "2026-10-27T14:45:00+00:00"}
        s = sig(timestamp="2026-10-27T15:00:00+00:00")
        kw = full_evidence(evaluated_at_utc=ev, bar_asof=intraday,
                           risk_decision=approval(at="2026-10-27T15:01:00+00:00"))
        ci, tr = enrich_signal_with_truth(s, **kw)
        self.assertEqual(ci.timeframe_context[K.TF_AVAILABLE], ["15m"])
        ci2, _ = enrich_signal_with_truth(
            s, policy=TruthPolicy(count_incomplete_as_available=True), **kw)
        self.assertEqual(ci2.timeframe_context[K.TF_AVAILABLE], ["1D", "4H", "1H", "15m"])

    def test_unverifiable_calendar_or_time_is_unknown(self):
        for over in (dict(calendar=None), dict(evaluated_at_utc=None),
                     dict(bar_asof=None), dict(evaluated_at_utc="2027-01-04T15:00:00+00:00")):
            ci, tr = enrich_signal_with_truth(sig(), **full_evidence(**over))
            self.assertNotIn(K.DQ_DATA_FRESHNESS_MINUTES, ci.data_quality_context, over)
            self.assertNotIn(K.DQ_STALE_DATA_FLAG, ci.data_quality_context, over)
            self.assertEqual(score_signal(sig(), **full_evidence(**over)).decision_bucket.value,
                             "BLOCKED", over)

    def test_invalid_bar_record_is_schema_mismatch(self):
        bad = dict(ASOF, **{"1H": "2026-10-27T19:30:00"})          # naive
        ci, tr = enrich_signal_with_truth(sig(), **full_evidence(bar_asof=bad))
        self.assertIs(ci.data_quality_context[K.DQ_SCHEMA_MATCH], False)
        self.assertIn("schema_mismatch", score_signal(sig(), **full_evidence(bar_asof=bad))
                      .hard_gate_failures)

    def test_thinness_only_from_registry(self):
        ci, tr = enrich_signal_with_truth(sig(), **full_evidence(production_thinness_status=None))
        self.assertNotIn(K.ML_PRODUCTION_THINNESS_STATUS, ci.ml_context)
        ci, tr = enrich_signal_with_truth(sig(), **full_evidence(production_thinness_status="maybe"))
        self.assertNotIn(K.ML_PRODUCTION_THINNESS_STATUS, ci.ml_context)


# ═══════════════════ direction, components, ranking preserved ═══════════════

def _pre_a01_reference(signal):
    """The pre-A01 bridge's assumed values, reproduced as a historical
    reference (scanner_bridge @ 80b4343) to prove the truth contract changes
    gate inputs only, not scoring direction, components or ordering."""
    adv = float(signal["entry_price"]) * float(signal["avg_volume_20d"])
    ci = adapter_from_scanner_signal(signal, liquidity={
        "price": signal["entry_price"], "avg_dollar_volume_20d": adv},
        data_quality={"schema_match": True, "stale_data_flag": False,
                      "data_freshness_minutes": 5.0})
    ml = dict(ci.ml_context or {})
    ml.update({K.ML_CALIBRATION_APPLIED: False, K.ML_PRED_CALIBRATED: None,
               K.ML_PRICE_ADJUSTMENT_MODE: "raw", "allow_adjusted_prices_for_ml": False,
               K.ML_MODEL_READINESS_PASSED: False, K.ML_PRODUCTION_THINNESS_STATUS: "ok"})
    tf = dict(ci.timeframe_context or {})
    tf["available_timeframes"] = signal.get("available_tfs", 4)
    rp = dict(ci.risk_preview or {})
    rp.update(risk_preview_available=True, risk_authority_status="ok")
    ci = dataclasses.replace(ci, ml_context=ml, timeframe_context=tf, risk_preview=rp,
                             advisory_context={"adjusted_price_pit_risk": False})
    return score_candidate(ci, default_config(profile=ScoringProfile.RESEARCH))


SET = [sig("STRONG", rsi=70, macd_hist=1.2, atr=3.0, vol_ratio=1.5),
       sig("MID", rsi=55, macd_hist=0.3, valid_count=3, atr=1.2, vol_ratio=1.1),
       sig("WEAK", rsi=48, macd_hist=0.0, valid_count=1, atr=0.4, vol_ratio=0.8),
       sig("SHRT", direction="short", rsi=40, macd_hist=-0.8)]

_NON_TRUTH_COMPONENTS = ("ml", "scanner", "technical_confluence", "trend", "momentum",
                         "volume_liquidity", "volatility", "market_regime",
                         "risk_adjusted")


class T7_RankingPreserved(unittest.TestCase):
    def test_components_and_direction_identical_to_pre_a01(self):
        for s in SET:
            old = _pre_a01_reference(s)
            new = score_signal_with_truth(s, **full_evidence(s["symbol"], s["direction"])).scored
            self.assertEqual(new.side, old.side)
            for c in _NON_TRUTH_COMPONENTS:
                self.assertEqual(new.component_scores[c], old.component_scores[c], (s["symbol"], c))
            self.assertEqual(new.component_scores["data_quality"], 100.0)

    def test_order_identical_with_and_without_evidence(self):
        old = [x.symbol for x in sorted((_pre_a01_reference(s) for s in SET),
                                        key=lambda x: (-x.final_score_100, x.symbol))]
        full = [t.scored.symbol for t in sorted(
            (score_signal_with_truth(s, **full_evidence(s["symbol"], s["direction"])) for s in SET),
            key=lambda t: (-t.research_rank_score, t.scored.symbol))]
        none = [t.scored.symbol for t in sorted(
            (score_signal_with_truth(s) for s in SET),
            key=lambda t: (-t.research_rank_score, t.scored.symbol))]
        self.assertEqual(full, old)
        self.assertEqual(none, old)
        self.assertEqual(old[0], "STRONG")

    def test_short_side_cap_still_applies(self):
        ts = score_signal_with_truth(SET[3], **full_evidence("SHRT", "short"))
        self.assertEqual(ts.scored.side.value, "SHORT")
        self.assertNotIn(ts.scored.decision_bucket.value, ("ELIGIBLE", "HIGH_CONVICTION"))

    def test_harness_ranks_by_research_score_and_reports_truth(self):
        from tools.signal_scoring import score_rank_harness as H
        res = H.build_result(H.fixture_signals(), truth_for=H.fixture_truth)
        self.assertEqual([r["symbol"] for r in res["ranked"]], ["CCC", "AAA", "BBB", "DDD"])
        self.assertEqual(res["n_m19_blocked"], 4)
        r0 = res["ranked"][0]
        self.assertEqual(r0["risk_authority"], "NOT_EVALUATED")
        self.assertEqual(r0["corporate_action"], "UNVERIFIED")
        self.assertIn("risk_preview.risk_authority_status", r0["unknown_inputs"])
        self.assertEqual(r0["observations"]["15m"], "VALID")
        self.assertEqual(r0["observations"]["1H"], "INCOMPLETE")
        self.assertIn("A01 truth contract", H.render(res))


# ═══════════════════════════ execution safety ═══════════════════════════════

_SCENARIOS = [dict(), full_evidence(), full_evidence(risk_decision=None),
              full_evidence(model_readiness_passed=True),
              full_evidence(bar_asof={}), full_evidence(detector_suspected=True)]


class T8_NoExecutionAuthority(unittest.TestCase):
    def test_execution_never_eligible(self):
        for kw in _SCENARIOS:
            for prof in (ScoringProfile.RESEARCH, ScoringProfile.STRICT):
                for s in SET:
                    ts = score_signal_with_truth(s, profile=prof, **kw)
                    self.assertIs(ts.scored.execution_eligible, False)
                    self.assertIs(ts.execution_eligible, False)
                    self.assertNotEqual(ts.scored.decision_bucket.value, "EXECUTE")

    def test_strict_with_full_evidence_still_not_gate_passed(self):
        ts = score_signal_with_truth(sig(), profile=ScoringProfile.STRICT, **full_evidence())
        self.assertFalse(ts.scored.hard_gate_passed)
        self.assertIn("model_readiness_failed", ts.scored.hard_gate_failures)

    def test_wrapper_refuses_a_true_execution_flag(self):
        real = score_signal_with_truth(sig(), **full_evidence())
        forged = TruthfulScore(_Forged(real.scored), real.truth)
        with self.assertRaises(AssertionError):
            forged.execution_eligible

    def test_truth_package_imports_only_allowlisted_modules(self):
        allowed = {"__future__", "dataclasses", "datetime", "enum", "math", "typing",
                   "zoneinfo", "bot.signal_scoring", "bot.signal_scoring.keys"}
        for p in sorted((REPO / "bot" / "scoring_truth").glob("*.py")):
            for node in ast.walk(ast.parse(p.read_text())):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    mods = [node.module or ""]
                for m in mods:
                    self.assertTrue(m in allowed or m.startswith("bot.scoring_truth"),
                                    "%s imports %s" % (p.name, m))

    def test_no_runtime_or_submit_module_imports_the_contract(self):
        targets = [REPO / "main.py"] + sorted((REPO / "bot").rglob("*.py"))
        for p in targets:
            if "scoring_truth" in p.parts:
                continue
            src = p.read_text(errors="replace")
            self.assertNotIn("scoring_truth", src, p)
            self.assertNotIn("scanner_bridge", src, p)

    def test_truth_types_carry_no_authorising_field(self):
        from bot.scoring_truth import contract, observations, corporate_actions
        words = ("execut", "approv", "order", "submit", "eligib", "authoris", "authoriz")
        for mod in (contract, observations, corporate_actions):
            for obj in vars(mod).values():
                if dataclasses.is_dataclass(obj) and isinstance(obj, type):
                    for f in dataclasses.fields(obj):
                        self.assertFalse(any(w in f.name.lower() for w in words),
                                         "%s.%s" % (obj.__name__, f.name))

    def test_scoring_opens_no_socket_or_process(self):
        def boom(*a, **k):
            raise AssertionError("I/O attempted during scoring")
        with mock.patch.object(socket, "socket", boom), \
                mock.patch.object(subprocess, "Popen", boom):
            for kw in _SCENARIOS:
                score_signal_with_truth(sig(), **kw)


class _Forged:
    def __init__(self, real):
        self._real = real
        self.execution_eligible = True

    def __getattr__(self, k):
        return getattr(self._real, k)


# ═══════════════════ scanner as-of record (C5b transition) ══════════════════

PRE_A01_SCANNER_BLOB = "4317e57e72f9d8d862607a8e3f155cc53f4ecd3d"
A01_SCANNER_ADDED = (
    "def _bar_start_iso(df):\n"
    "    \"\"\"A01: ISO start time of the last bar (the row compute_features scored).\n"
    "    Metadata only; never raises.\"\"\"\n"
    "    try:\n"
    "        return df.index[-1].isoformat()\n"
    "    except Exception:\n"
    "        return None\n"
    "\n"
    "\n",
    "    bar_asof      = {}   # A01: sym -> {tf_label -> UTC start of the bar used}\n",
    "            bar_asof.setdefault(sym, {})[tf_label] = _bar_start_iso(df)\n",
    "        'bar_asof':       bar_asof,   # A01: read-only as-of metadata\n",
)


def _blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _bars(end, freq, n=120, seed=0):
    idx = pd.date_range(end=end, periods=n, freq=freq, tz="UTC")
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.004, n)))
    return pd.DataFrame({"open": close, "high": close * 1.002, "low": close * 0.998,
                         "close": close, "volume": rng.integers(1e5, 2e5, n).astype(float)},
                        index=idx)


class T9_ScannerAsOf(unittest.TestCase):
    def _scan(self, frames_by_interval):
        import bot.scanner as scanner
        from bot.strategy import DEFAULTS

        class _Sent:
            name = "test"

            def get_sentiment(self, sym):
                from bot.sentiment.base import SentimentResult
                return SentimentResult.unavailable("test", "test")

        def fetch(focus, period, interval):
            return {s: frames_by_interval[interval].copy() for s in focus}

        with mock.patch.object(scanner, "load_strategy", lambda: DEFAULTS), \
                mock.patch.object(scanner, "fetch_bars", fetch), \
                mock.patch.object(scanner, "score_timeframe", lambda i, d, s: 1 if d == "long" else 0), \
                mock.patch.object(scanner, "get_sentiment_mode", lambda: "off"), \
                mock.patch.object(scanner, "get_sentiment_provider", lambda: _Sent()), \
                mock.patch.object(scanner, "apply_sentiment", lambda s, r, m: (s, True)):
            return scanner.scan_cycle(["AAA", "BBB"], {}, conn=None, cycle_id=0)

    def test_meta_records_the_scored_bar_per_symbol_and_timeframe(self):
        from bot.data import resample_to_4h
        frames = {"1d": _bars("2026-10-27", "1D"),
                  "1h": _bars("2026-10-27 19:00", "1h", 200),
                  "15m": _bars("2026-10-27 19:45", "15min", 200)}
        signals, meta = self._scan(frames)
        self.assertTrue(signals)
        exp = {"1D": frames["1d"].index[-1].isoformat(),
               "4H": resample_to_4h(frames["1h"]).index[-1].isoformat(),
               "1H": frames["1h"].index[-1].isoformat(),
               "15m": frames["15m"].index[-1].isoformat()}
        self.assertEqual(meta["bar_asof"], {"AAA": exp, "BBB": exp})
        self.assertEqual(exp["4H"], "2026-10-27T16:00:00+00:00")
        self.assertEqual(set(meta), {"tfs_available", "tfs_list", "symbols_scanned", "bar_asof"})
        for s in signals:   # the signal dict is unchanged: no new keys
            self.assertFalse(any("asof" in k or k.startswith("_bar") for k in s))
        # end-to-end: the scanner's record feeds the contract
        ts = score_signal_with_truth(signals[0], evaluated_at_utc="2026-10-27T21:00:00+00:00",
                                     calendar=CAL, bar_asof=meta["bar_asof"]["AAA"])
        self.assertEqual(ts.truth.observation_states()["1D"], "VALID")

    def test_bar_start_iso_never_raises(self):
        from bot.scanner import _bar_start_iso
        self.assertIsNone(_bar_start_iso(object()))
        self.assertIsNone(_bar_start_iso(pd.DataFrame()))
        naive = pd.DataFrame({"close": [1.0]}, index=[pd.Timestamp("2026-10-27 19:30")])
        self.assertEqual(_bar_start_iso(naive), "2026-10-27T19:30:00")
        self.assertEqual(obs("1H", _bar_start_iso(naive), EV).state, ObservationState.INVALID)

    def test_scanner_change_is_exactly_the_a01_additions(self):
        text = (REPO / "bot" / "scanner.py").read_text()
        for chunk in A01_SCANNER_ADDED:
            self.assertEqual(text.count(chunk), 1, chunk)
            text = text.replace(chunk, "", 1)
        self.assertEqual(_blob(text.encode()), PRE_A01_SCANNER_BLOB)

    def test_c5b_table_excuses_only_the_reviewed_blob(self):
        import _protected_baselines as PB
        e = PB.APPROVED_CHANGES["bot/scanner.py"]
        self.assertEqual(e.from_blob, "63a71b91ac88a34905fe29135b5d857f318ba129")
        self.assertEqual(e.to_blob, PB.worktree_blob("bot/scanner.py"))
        self.assertIn("A01", e.approval)
        data = (REPO / "bot" / "scanner.py").read_bytes()
        self.assertNotEqual(_blob(data + b"\n# drift\n"), e.to_blob)
        self.assertNotEqual(_blob(data.replace(b"bar_asof", b"bar_asoF")), e.to_blob)

    def test_main_reads_only_preexisting_meta_keys(self):
        src = (REPO / "main.py").read_text()
        self.assertNotIn("bar_asof", src)
        keys = set()
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "meta"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                keys.add(node.args[0].value)
        self.assertEqual(keys, {"tfs_available", "tfs_list", "symbols_scanned"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
