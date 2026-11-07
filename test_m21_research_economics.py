"""test_m21_research_economics.py — M21 WP12 realistic execution economics (B10).

Pins the dated fee schedules, point-in-time liquidity, spread / impact /
slippage curves, fail-closed fill rules (thin, halted, zero volume, missing
liquidity, borrow), split- and dividend-correct P&L, and the analyses that
say when costs erase an edge. Offline only; every input is hand-built.
Run via ``python _test_isolation.py test_m21_research_economics``.
"""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from research import boundary
from research.economics import analysis as A
from research.economics import fees as F
from research.economics.model import (CAPPED, FILLED, INCONCLUSIVE, NO_FILL, BorrowInfo, CashDividend,
                                      CorporateActions, EconomicsModel, FillBar, LiquiditySnapshot, SplitEvent,
                                      TradeSpec, liquidity_asof, missing_liquidity, price_trade)
from research.evidence.candidates import Bracket, CandidateEvent, make_candidate_id
from research.evidence.costs import ZERO
from research.evidence.labeler import label_candidates

REPO = Path(__file__).resolve().parent
M = EconomicsModel()
VERIFIED = CorporateActions(complete=True)
D0, D1 = date(2026, 5, 4), date(2026, 5, 6)


def daily(n=30, price=100.0, shares=2e6, start="2026-03-02", swing=0.01):
    """Business days stamped 21:00 UTC (bar close); closes alternate +/- swing."""
    ts = pd.bdate_range(start, periods=n, tz="UTC") + pd.Timedelta(21, unit="h")
    c = price * (1 + swing * np.where(np.arange(n) % 2, 1.0, -1.0))
    return pd.DataFrame({"ts_utc": ts, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
                         "volume": np.full(n, float(shares)), "quality_flags": np.zeros(n, dtype=np.int64)})


def snap(price=100.0, shares=2e6, swing=0.01, **kw):
    d = daily(price=price, shares=shares, swing=swing)
    return liquidity_asof(d, d["ts_utc"].iloc[-1], symbol="X", **kw)


LIQ = snap()


def spec(**kw):
    base = dict(symbol="X", direction="long", shares=100, entry_ref_price=100.0, exit_ref_price=102.0,
                entry_date=D0, exit_date=D1, risk_per_share=2.0)
    base.update(kw)
    return TradeSpec(**base)


def priced(s=None, **kw):
    kw.setdefault("model", M)
    kw.setdefault("entry_liq", LIQ)
    kw.setdefault("actions", VERIFIED)
    return price_trade(s or spec(), **kw)


class E1_Fees(unittest.TestCase):
    def test_fixed_plan_minimum_and_one_percent_cap(self):
        self.assertAlmostEqual(F.commission(F.IBKR_PRO_FIXED, 100, 50.0), 1.00)        # 0.50 -> min 1.00
        self.assertAlmostEqual(F.commission(F.IBKR_PRO_FIXED, 1000, 50.0), 5.00)
        self.assertAlmostEqual(F.commission(F.IBKR_PRO_FIXED, 10, 1.0), 0.10)          # 1 % cap beats the min

    def test_tiered_plan_rates_minimum_and_clearing_cap(self):
        T = F.IBKR_PRO_TIERED
        self.assertAlmostEqual(F.commission(T, 1000, 50.0), 3.50)
        self.assertAlmostEqual(F.commission(T, 1000, 50.0, monthly_shares=500_000), 2.00)
        self.assertAlmostEqual(F.commission(T, 1000, 50.0, monthly_shares=2e8), 0.50)
        self.assertAlmostEqual(F.commission(T, 50, 50.0), 0.35)
        fb = F.order_fees(1000, 0.02, F.BUY, D0, plan=T)
        self.assertAlmostEqual(fb.clearing, 0.10)                     # 0.0002 x 1000 = 0.20 -> 0.5 % of $20
        self.assertAlmostEqual(fb.pass_through, fb.commission * (0.000175 + 0.000565))

    def test_sec_and_taf_on_sales_only_and_dated(self):
        sell = F.order_fees(1000, 100.0, F.SELL, date(2026, 5, 1))
        self.assertAlmostEqual(sell.sec, 20.60e-6 * 100_000)
        self.assertAlmostEqual(sell.taf, 0.166)
        self.assertEqual(sell.flags, ())
        buy = F.order_fees(1000, 100.0, F.BUY, date(2026, 5, 1))
        self.assertEqual((buy.sec, buy.taf), (0.0, 0.0))
        self.assertAlmostEqual(buy.cat, 0.003)
        holiday = F.order_fees(1000, 100.0, F.SELL, date(2026, 11, 2))
        self.assertEqual(holiday.taf, 0.0)
        big = F.order_fees(100_000, 10.0, F.SELL, date(2026, 9, 1))
        self.assertAlmostEqual(big.taf, 8.30)

    def test_date_outside_verified_windows_uses_worst_rate_and_flags(self):
        fb = F.order_fees(1000, 100.0, F.SELL, date(2026, 2, 2))
        self.assertAlmostEqual(fb.sec, 20.60e-6 * 100_000)
        self.assertIn("fee_rate_unverified_for_date:sec", fb.flags)
        fb27 = F.order_fees(1000, 100.0, F.SELL, date(2027, 1, 4))
        self.assertIn("fee_rate_unverified_for_date:taf", fb27.flags)
        self.assertAlmostEqual(fb27.taf, 0.166)

    def test_bad_side_rejected(self):
        with self.assertRaises(ValueError):
            F.order_fees(1, 1.0, "short", D0)


class E2_Liquidity(unittest.TestCase):
    def test_point_in_time_ignores_later_bars(self):
        d = daily(40)
        asof = d["ts_utc"].iloc[25]
        a = liquidity_asof(d, asof)
        d2 = d.copy()
        d2.loc[26:, "volume"] = 1.0
        d2.loc[26:, "close"] = 1.0
        self.assertEqual(a, liquidity_asof(d2, asof))
        self.assertEqual(a.last_close, d["close"].iloc[25])           # the bar closing at asof counts
        self.assertEqual(liquidity_asof(d, asof - pd.Timedelta(1, "s")).last_close, d["close"].iloc[24])

    def test_short_history_and_missing_data(self):
        self.assertEqual(liquidity_asof(daily(10), "2026-12-31").status, "insufficient_history")
        self.assertEqual(liquidity_asof(None, "2026-12-31").status, "missing")
        self.assertEqual(liquidity_asof(daily(30), "2020-01-01").status, "missing")

    def test_bad_rows_excluded_and_zero_volume_counted(self):
        d = daily(30)
        d.loc[25, "close"] = np.nan                                    # inside the last 20 rows
        d.loc[26, "volume"] = -5
        d.loc[27, "volume"] = 0
        s = liquidity_asof(d, d["ts_utc"].iloc[-1])
        self.assertEqual((s.sessions, s.zero_volume_sessions), (18, 1))

    def test_dollar_adv_is_split_basis_invariant(self):
        raw = daily(30)
        adj = raw.copy()
        adj.loc[:14, "close"] /= 2.0
        adj.loc[:14, "volume"] *= 2.0
        raw2 = raw.copy()
        raw2["close"] = adj["close"] * np.where(np.arange(30) <= 14, 2.0, 1.0)
        a = liquidity_asof(adj, adj["ts_utc"].iloc[-1], window=10)
        b = liquidity_asof(raw2, raw2["ts_utc"].iloc[-1], window=10)
        self.assertAlmostEqual(a.adv_dollars, b.adv_dollars)


class E3_CostCurves(unittest.TestCase):
    def test_spread_widens_as_liquidity_falls_and_has_a_tick_floor(self):
        bps = [M.half_spread_bps(snap(shares=s), 100.0) for s in (1e7, 1e6, 1e5, 2e4, 5e3, 1e3)]
        self.assertEqual(bps, sorted(bps))
        self.assertLess(bps[0], bps[-1])
        self.assertAlmostEqual(M.half_spread_bps(snap(price=2.0, shares=1e9), 2.0), 25.0)   # half a cent on $2

    def test_quoted_spread_overrides_bucket(self):
        q = replace(LIQ, quoted_half_spread_bps=7.5)
        self.assertEqual(M.half_spread_bps(q, 100.0), 7.5)

    def test_impact_follows_square_root_of_participation(self):
        a, b = M.impact_bps(LIQ, 1e5), M.impact_bps(LIQ, 4e5)
        self.assertAlmostEqual(b / a, 2.0)
        self.assertEqual(M.impact_bps(LIQ, 0), 0.0)

    def test_extreme_volatility_costs_more_and_is_flagged(self):
        calm = priced(entry_liq=snap(swing=0.005))
        wild = priced(entry_liq=snap(swing=0.08))
        self.assertIn("extreme_volatility", wild.flags)
        self.assertNotIn("extreme_volatility", calm.flags)
        self.assertGreater(wild.cost_bps, calm.cost_bps)
        gap = priced(spec(stop_gap=True, exit_ref_price=98.0), entry_liq=snap(swing=0.08))
        self.assertGreater(gap.stop_gap_cost, 0)
        self.assertEqual(priced(spec(exit_ref_price=98.0), entry_liq=snap(swing=0.08)).stop_gap_cost, 0.0)

    def test_stress_scales_every_component(self):
        a, b = priced(spec(shares=1000)), priced(spec(shares=1000), model=M.scale(2.0))
        self.assertAlmostEqual(b.spread_cost + b.impact_cost + b.slippage_cost,
                               2 * (a.spread_cost + a.impact_cost + a.slippage_cost), places=6)
        self.assertAlmostEqual(b.fees / a.fees, 2.0, places=2)        # fees also move with the worse fill prices
        self.assertAlmostEqual(b.total_cost / a.total_cost, 2.0, places=2)
        self.assertEqual(b.gross_pnl, a.gross_pnl)
        self.assertEqual(b.model, "wp12_base_x2")

    def test_net_equals_fill_prices_minus_fees(self):
        t = priced(spec(shares=1000))
        q = t.real_entry_shares
        self.assertAlmostEqual(t.net_pnl, q * (t.exit_fill - t.entry_fill) - t.fees, places=6)
        self.assertGreater(t.entry_fill, t.real_entry_price)
        self.assertLess(t.exit_fill, t.real_exit_price)
        self.assertAlmostEqual(t.gross_r, 1.0)
        self.assertLess(t.net_r, t.gross_r)


class E4_FillRules(unittest.TestCase):
    def test_thin_instrument_capped_at_adv_participation(self):
        thin = snap(price=10.0, shares=20_000)                        # ~$200k ADV
        t = priced(spec(shares=5000, entry_ref_price=10.0, exit_ref_price=10.5, risk_per_share=0.5),
                   entry_liq=thin)
        self.assertEqual(t.status, CAPPED)
        self.assertIn("entry_participation_capped", t.flags)
        self.assertLessEqual(t.notional, 0.01 * thin.adv_dollars + 1e-6)
        self.assertEqual(t.real_entry_shares, int(0.01 * thin.adv_dollars // 10.0))

    def test_cap_below_one_share_is_no_fill(self):
        t = priced(spec(shares=10, entry_ref_price=500.0, exit_ref_price=510.0), entry_liq=snap(price=500, shares=50))
        self.assertEqual((t.status, t.reasons), (NO_FILL, ("below_one_share_after_caps",)))
        self.assertFalse(t.priced)

    def test_zero_volume_and_halted_entry_bars_do_not_fill(self):
        z = priced(spec(entry_bar=FillBar(100, 100, 100, 100, 0)))
        h = priced(spec(entry_bar=FillBar(100, 101, 99, 100, 5e5, halted=True)))
        self.assertEqual((z.status, z.reasons), (NO_FILL, ("entry_bar_zero_volume",)))
        self.assertEqual((h.status, h.reasons), (NO_FILL, ("entry_bar_halted",)))
        self.assertEqual((z.total_cost, z.net_pnl), (0.0, 0.0))

    def test_missing_liquidity_is_never_priced_as_free(self):
        for liq, why in ((missing_liquidity("X"), "liquidity_missing"),
                         (liquidity_asof(daily(5), "2026-12-31"), "liquidity_insufficient_history")):
            t = priced(entry_liq=liq)
            self.assertEqual((t.status, t.reasons), (NO_FILL, (why,)))
            self.assertFalse(t.priced)

    def test_fill_bar_participation_cap(self):
        t = priced(spec(shares=2000, entry_bar=FillBar(100, 101, 99, 100, 5000)))   # bar $500k -> cap $50k
        self.assertEqual(t.status, CAPPED)
        self.assertEqual(t.real_entry_shares, 500)

    def test_untradable_exit_bar_is_inconclusive(self):
        t = priced(spec(exit_bar=FillBar(102, 102, 102, 102, 0)))
        self.assertEqual(t.status, INCONCLUSIVE)
        self.assertIn("exit_bar_untradable", t.reasons)
        self.assertFalse(t.priced)

    def test_exit_above_capacity_flagged_and_priced_in_full(self):
        thin_exit = snap(shares=2_000)                                # $200k ADV at exit
        t = priced(spec(shares=1000), exit_liq=thin_exit)
        self.assertEqual(t.status, FILLED)
        self.assertIn("exit_capacity_breach", t.flags)
        self.assertEqual(t.real_exit_shares, 1000)
        self.assertGreater(t.impact_cost, priced(spec(shares=1000)).impact_cost)

    def test_unknown_exit_liquidity_falls_back_to_entry_with_flag(self):
        t = priced(exit_liq=missing_liquidity("X"))
        self.assertIn("exit_liquidity_unknown_used_entry", t.flags)
        self.assertEqual(t.status, FILLED)


class E5_Borrow(unittest.TestCase):
    S = dict(direction="short", entry_ref_price=100.0, exit_ref_price=98.0)

    def test_short_without_known_borrow_is_refused(self):
        for b, why in ((None, "borrow_unknown"), (BorrowInfo("unknown"), "borrow_unknown"),
                       (BorrowInfo("unavailable"), "borrow_unavailable"),
                       (BorrowInfo("hard"), "borrow_hard_rate_unknown")):
            t = priced(spec(**self.S), borrow=b)
            self.assertEqual((t.status, t.reasons), (NO_FILL, (why,)))
        with self.assertRaises(ValueError):
            BorrowInfo("maybe")

    def test_hard_to_borrow_fee_accrues_per_night(self):
        t = priced(spec(shares=1000, exit_date=date(2026, 5, 14), **self.S), borrow=BorrowInfo("hard", 0.20))
        self.assertAlmostEqual(t.borrow_cost, t.notional * 0.20 * 10 / 360)
        self.assertEqual(t.status, FILLED)

    def test_easy_borrow_default_rate_and_same_day(self):
        t = priced(spec(shares=1000, **self.S), borrow=BorrowInfo("easy"))
        self.assertIn("borrow_rate_assumed", t.flags)
        self.assertAlmostEqual(t.borrow_cost, t.notional * 0.0025 * 2 / 360)
        same = priced(spec(shares=1000, exit_date=D0, **self.S), borrow=BorrowInfo("easy", 0.01))
        self.assertEqual(same.borrow_cost, 0.0)

    def test_short_pays_regulatory_fees_on_the_sale_leg(self):
        t = priced(spec(shares=1000, **self.S), borrow=BorrowInfo("easy", 0.01))
        self.assertGreater(t.fees_entry.sec, 0)
        self.assertEqual(t.fees_exit.sec, 0.0)
        self.assertLess(t.entry_fill, t.real_entry_price)
        self.assertGreater(t.exit_fill, t.real_exit_price)
        self.assertGreater(t.gross_pnl, 0)
        self.assertEqual(priced(spec(shares=1000)).borrow_cost, 0.0)


class E6_CorporateActions(unittest.TestCase):
    SPLIT = CorporateActions(True, splits=(SplitEvent(date(2026, 5, 5), 2.0),))

    def test_split_inside_holding_window_same_pnl_on_both_bases(self):
        raw = priced(spec(shares=100, entry_ref_price=100.0, exit_ref_price=55.0, risk_per_share=5.0),
                     actions=self.SPLIT, basis="raw")
        adj = priced(spec(shares=200, entry_ref_price=50.0, exit_ref_price=55.0, risk_per_share=2.5),
                     actions=self.SPLIT, basis="split_adjusted")
        self.assertIn("split_in_window_corrected", raw.flags)
        self.assertAlmostEqual(raw.gross_pnl, 200 * 55 - 100 * 100)
        for k in ("gross_pnl", "net_pnl", "total_cost", "fees", "gross_r", "net_r"):
            self.assertAlmostEqual(getattr(raw, k), getattr(adj, k), places=6, msg=k)
        self.assertEqual((adj.real_entry_shares, adj.real_exit_shares), (100, 200))

    def test_fees_use_real_share_counts(self):
        later = CorporateActions(True, splits=(SplitEvent(date(2026, 6, 1), 2.0),))
        adj = priced(spec(shares=2000, entry_ref_price=50.0, exit_ref_price=51.0), actions=later)
        self.assertEqual(adj.real_entry_shares, 1000)
        self.assertAlmostEqual(adj.fees_entry.commission, 5.00)       # 1000 real shares, not 2000
        self.assertAlmostEqual(adj.real_entry_price, 100.0)

    def test_raw_basis_without_verified_actions_is_inconclusive(self):
        t = priced(actions=CorporateActions(False), basis="raw")
        self.assertEqual(t.status, INCONCLUSIVE)
        self.assertIn("raw_basis_without_verified_corporate_actions", t.reasons)
        a = priced(actions=None, basis="split_adjusted")
        self.assertEqual(a.status, FILLED)
        self.assertIn("corporate_actions_unverified", a.flags)

    def test_reverse_split_fractional_shares_flagged(self):
        rev = CorporateActions(True, splits=(SplitEvent(date(2026, 5, 5), 1 / 3),))
        t = priced(spec(shares=100, exit_ref_price=300.0), actions=rev, basis="raw")
        self.assertEqual(t.real_exit_shares, 33)
        self.assertIn("fractional_shares_after_split", t.flags)
        with self.assertRaises(ValueError):
            SplitEvent(D0, 0.0)

    def test_dividends_received_long_paid_short_ignored_all_adjusted(self):
        div = CorporateActions(True, dividends=(CashDividend(date(2026, 5, 5), 0.50),))
        lg = priced(spec(shares=1000), actions=div, basis="split_adjusted")
        sh = priced(spec(shares=1000, direction="short"), actions=div, basis="split_adjusted",
                    borrow=BorrowInfo("easy", 0.01))
        ta = priced(spec(shares=1000), actions=div, basis="all_adjusted")
        self.assertAlmostEqual(lg.dividend_cash, 500.0)
        self.assertAlmostEqual(sh.dividend_cash, -500.0)
        self.assertEqual(ta.dividend_cash, 0.0)
        self.assertIn("real_price_approximate_all_adjusted", ta.flags)
        outside = CorporateActions(True, dividends=(CashDividend(D0, 0.50),))   # ex-date = entry day
        self.assertEqual(priced(spec(shares=1000), actions=outside).dividend_cash, 0.0)

    def test_invalid_inputs_rejected(self):
        with self.assertRaises(ValueError):
            spec(direction="sideways")
        with self.assertRaises(ValueError):
            spec(exit_date=date(2026, 5, 1))
        with self.assertRaises(ValueError):
            priced(basis="dividend_only")
        with self.assertRaises(ValueError):
            EconomicsModel(max_adv_participation=0)


class E7_Analysis(unittest.TestCase):
    def test_minimum_commission_erases_a_small_trade_edge(self):
        small = [priced(spec(shares=5, exit_ref_price=100.5, risk_per_share=1.0)) for _ in range(40)]
        big = [priced(spec(shares=200, exit_ref_price=100.5, risk_per_share=1.0)) for _ in range(40)]
        e_small, e_big = A.edge_after_costs(small, unit="usd"), A.edge_after_costs(big, unit="usd")
        self.assertEqual(e_small.verdict, A.EDGE_ERASED)
        self.assertGreater(e_small.mean_gross, 0)
        self.assertEqual(e_big.verdict, A.EDGE_SURVIVES)
        self.assertEqual(e_small.share_cost_exceeds_gross, 1.0)

    def test_thin_evidence_and_no_gross_edge(self):
        few = [priced(spec(shares=200)) for _ in range(5)]
        self.assertEqual(A.edge_after_costs(few).verdict, A.INCONCLUSIVE_VERDICT)
        losers = [priced(spec(shares=200, exit_ref_price=99.0)) for _ in range(40)]
        self.assertEqual(A.edge_after_costs(losers).verdict, A.NO_GROSS_EDGE)
        none = [priced(entry_liq=missing_liquidity("X")) for _ in range(40)]
        r = A.edge_after_costs(none)
        self.assertEqual((r.n_priced, r.n_unpriced, r.verdict), (0, 40, A.INCONCLUSIVE_VERDICT))
        with self.assertRaises(ValueError):
            A.edge_after_costs(few, unit="eur")

    def test_size_sensitivity_finds_the_viable_band_and_capacity(self):
        mid = snap(price=50.0, shares=200_000)                         # $10M ADV -> $100k cap at 1 %
        s = A.size_sensitivity(spec(entry_ref_price=50.0, exit_ref_price=50.5, risk_per_share=1.0),
                               [100, 1_000, 5_000, 50_000, 200_000, 500_000], model=M, entry_liq=mid,
                               actions=VERIFIED)
        st = [r.status for r in s.rows]
        self.assertEqual(st, [FILLED] * 4 + [CAPPED] * 2)
        self.assertEqual(s.verdict, A.CAPACITY_LIMITED)
        self.assertEqual((s.min_viable_notional, s.max_viable_notional), (1_000, 50_000))
        self.assertEqual(s.rows[-1].notional_filled, 100_000)
        self.assertGreater(s.rows[0].cost_bps, s.rows[2].cost_bps)    # minimum commission dominates small sizes
        self.assertGreater(s.rows[3].cost_bps, s.rows[2].cost_bps)    # impact dominates large sizes
        self.assertLess(s.rows[0].net_pnl, 0)
        erased = A.size_sensitivity(spec(exit_ref_price=100.01), [1e3, 1e4], model=M, entry_liq=LIQ,
                                    actions=VERIFIED)
        self.assertEqual((erased.verdict, erased.min_viable_notional), (A.EDGE_ERASED, None))

    def test_fee_calibration_verdicts_and_never_auto_applied(self):
        rec = lambda f, s: {"modelled_fees": 1.0, "realised_fees": f, "modelled_slippage_bps": 2.0,
                            "realised_slippage_bps": s}
        thin = A.fee_calibration([rec(1.5, 6.0)] * 10)
        self.assertEqual((thin.fees.verdict, thin.slippage.verdict), (A.INCONCLUSIVE_VERDICT,) * 2)
        self.assertIsNone(thin.suggested_fee_multiplier)
        under = A.fee_calibration([rec(1.5, 5.0)] * 40)
        self.assertEqual(under.fees.verdict, A.UNDERESTIMATES)
        self.assertEqual(under.slippage.verdict, A.UNDERESTIMATES)
        self.assertAlmostEqual(under.suggested_fee_multiplier, 1.5)
        self.assertAlmostEqual(under.suggested_slippage_add_bps, 3.0)
        self.assertFalse(under.applied)
        ok = A.fee_calibration([rec(1.05, 2.5)] * 20 + [rec(0.95, 1.5)] * 20)
        self.assertEqual((ok.fees.verdict, ok.slippage.verdict), (A.CALIBRATED, A.CALIBRATED))
        over = A.fee_calibration([rec(0.5, -3.0)] * 40)
        self.assertEqual((over.fees.verdict, over.slippage.verdict), (A.OVERESTIMATES, A.OVERESTIMATES))
        self.assertIsNone(over.suggested_fee_multiplier)
        zero_model = A.fee_calibration([{"modelled_fees": 0.0, "realised_fees": 1.0}] * 40)
        self.assertEqual(zero_model.fees.n, 0)


def _bars(n=60, start="2026-03-02"):
    ts = pd.bdate_range(start, periods=n, tz="UTC") + pd.Timedelta(21, unit="h")
    c = 100 + np.arange(n) * 0.0
    o = c.copy()
    h, l = c + 0.5, c - 0.5
    h[45:50] = [100.5, 101, 102, 103.5, 104]                          # a long that reaches target
    l[52] = 97.0                                                      # a long that is stopped
    return pd.DataFrame({"ts_utc": ts, "open": o, "high": h, "low": l, "close": c,
                         "volume": np.full(n, 1e6), "quality_flags": np.zeros(n, dtype=np.int64)})


def _cand(bars, i, direction="long", sym="X"):
    t = pd.Timestamp(bars["ts_utc"].iloc[i])
    return CandidateEvent(make_candidate_id(sym, t, direction, "t"), sym, t, direction, "eligible", "synthetic",
                          "1D", "t", 1.0, Bracket(2.0, 3.0, 5))


class E8_RepriceLabels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = _bars()
        cls.lab = label_candidates([_cand(cls.b, 44), _cand(cls.b, 50), _cand(cls.b, 58)], {"X": cls.b},
                                   timeframe="1D", cost=ZERO, stress_cost=ZERO)

    def _run(self, **kw):
        kw.setdefault("model", M)
        kw.setdefault("notional", 10_000.0)
        kw.setdefault("actions_by_symbol", {"X": VERIFIED})
        return A.reprice_labels(self.lab, {"X": self.b}, **kw)

    def test_definitive_labels_priced_below_gross_and_stress_below_base(self):
        out = self._run(bars_by_symbol={"X": self.b})
        d = out[out["status"] == "definitive"]
        self.assertGreaterEqual(len(d), 2)
        self.assertTrue((d["econ_status"] == FILLED).all())
        self.assertTrue((d["r_net_econ"] < d["r_gross"]).all())
        self.assertTrue((d["r_net_econ_stress"] < d["r_net_econ"]).all())
        self.assertTrue((out["econ_model"] == "wp12_base").all())

    def test_non_definitive_labels_are_not_priced(self):
        out = self._run()
        nd = out[out["status"] != "definitive"]
        self.assertGreaterEqual(len(nd), 1)
        self.assertTrue((nd["econ_status"] == "not_priced").all())
        self.assertTrue(nd["r_net_econ"].isna().all())

    def test_missing_daily_history_means_no_fill(self):
        out = A.reprice_labels(self.lab, {}, model=M, notional=10_000.0)
        d = out[out["status"] == "definitive"]
        self.assertTrue((d["econ_status"] == NO_FILL).all())
        self.assertTrue(d["r_net_econ"].isna().all())

    def test_risk_sizing_and_determinism(self):
        a, b = self._run(notional=None, risk_dollars=200.0), self._run(notional=None, risk_dollars=200.0)
        pd.testing.assert_frame_equal(a, b)
        d = a[a["status"] == "definitive"]
        self.assertTrue((d["econ_shares"] == 100).all())                # $200 / $2 stop distance
        with self.assertRaises(ValueError):
            self._run(notional=1.0, risk_dollars=1.0)
        with self.assertRaises(ValueError):
            self._run(notional=None)


class E9_Boundary(unittest.TestCase):
    def test_economics_package_respects_research_boundary(self):
        self.assertEqual([str(v) for v in boundary.scan_tree(REPO / "research" / "economics")], [])

    def test_no_production_module_imports_economics(self):
        for top in ("bot", "tools", "dashboard"):
            for p in (REPO / top).rglob("*.py"):
                self.assertNotIn("research.economics", p.read_text(encoding="utf-8", errors="ignore"), str(p))


if __name__ == "__main__":
    unittest.main()
