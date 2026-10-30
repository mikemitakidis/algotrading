"""M21 E5 — IBKR paper statement reconciliation and fee calibration (offline).

Statements are synthetic CSV fixtures written in the IBKR Flex (Trades) and
Activity Statement layouts; journals are built with the production D2 schema
(``LoopJournal``). Every account id is a made-up placeholder. These tests prove
the parsing, matching and labelling rules; they say nothing about real fills.
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.paper_loop_d2.journal import LoopJournal
from research.economics import statement as ST
from research.economics.fees import IBKR_PRO_FIXED, order_fees

PAPER, LIVE = "DUX0000001", "UX9999999"
FLEX_HEAD = ["ClientAccountID", "AssetClass", "CurrencyPrimary", "Symbol", "Buy/Sell", "Quantity", "TradePrice",
             "IBCommission", "IBCommissionCurrency", "DateTime", "OrderReference", "IBOrderID", "IBExecID",
             "LevelOfDetail"]
T0 = datetime(2026, 10, 15, 13, 35, 0, tzinfo=timezone.utc)        # 09:35 New York (EDT)


def ny(t):
    """A UTC time as a New York wall-clock Flex timestamp (DST-aware)."""
    return t.astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d;%H%M%S")


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def quiet(fn, *a):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*a)


class Env:
    def __init__(self, tc):
        self.dir = tempfile.mkdtemp(prefix="e5_")
        tc.addCleanup(shutil.rmtree, self.dir, True)
        self.journal = os.path.join(self.dir, "journal.db")
        LoopJournal(self.journal)
        self.n = 0

    def order(self, *, symbol="AAA", direction="long", qty=100, entry=(10.0, T0, 1.0), exit=(11.0, T0 + timedelta(
            hours=2), 1.0), source="ibkr_paper", state="CLOSED", ref_price=9.99, perm="9001", order_ref=True,
              exec_claims=None, account=None):
        self.n += 1
        ref = "R%03d" % self.n
        oref = "M21L-%03d" % self.n if order_ref else None
        e_px, e_t, e_f = entry if entry else (None, None, None)
        x_px, x_t, x_f = exit if exit else (None, None, None)
        c = sqlite3.connect(self.journal)
        with c:
            c.execute(
                "INSERT INTO loop_orders (client_ref, cycle_key, symbol, direction, qty, entry_ref_price, state, "
                "data_source, broker_perm_id, planned_at_utc, updated_at_utc, entry_fill_price, entry_filled_at_utc,"
                " entry_fees, exit_fill_price, exit_filled_at_utc, exit_fees, order_ref, sl_perm_id, account) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ref, "scan:x", symbol, direction, qty, ref_price, state, source, perm, T0.isoformat(),
                 T0.isoformat(), e_px, e_t.isoformat() if e_t else None, e_f, x_px,
                 x_t.isoformat() if x_t else None, x_f, oref, "9002", account))
            for exec_id, leg in (exec_claims or ()):
                c.execute("INSERT INTO loop_exec_claims (exec_id, client_ref, leg, claimed_at_utc) VALUES (?,?,?,?)",
                          (exec_id, ref, leg, T0.isoformat()))
        c.close()
        return ref, oref

    def flex(self, rows, *, name="flex.csv", head=FLEX_HEAD):
        p = os.path.join(self.dir, name)
        with open(p, "w", newline="") as fh:
            fh.write(",".join('"%s"' % h for h in head) + "\n")
            for r in rows:
                fh.write(",".join('"%s"' % r.get(h, "") for h in head) + "\n")
        return p

    def legacy(self, rows):
        p = os.path.join(self.dir, "signals.db")
        c = sqlite3.connect(p)
        with c:
            c.execute("CREATE TABLE execution_intents (id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, "
                      "broker TEXT, status TEXT, fill_price REAL, fill_qty REAL, filled_at TEXT)")
            for r in rows:
                c.execute("INSERT INTO execution_intents (symbol, direction, broker, status, fill_price, fill_qty, "
                          "filled_at) VALUES (?,?,?,?,?,?,?)", r)
        c.close()
        return p


def ex(sym="AAA", side="BUY", qty=100, px=10.0, t=T0, comm="-1.0", oref="", oid="", eid="", acct=PAPER,
       level="EXECUTION", ccy="USD"):
    q = qty if side == "BUY" else -qty
    return {"ClientAccountID": acct, "AssetClass": "STK", "CurrencyPrimary": "USD", "Symbol": sym, "Buy/Sell": side,
            "Quantity": "%g" % q, "TradePrice": "%g" % px, "IBCommission": comm, "IBCommissionCurrency": ccy,
            "DateTime": ny(t), "OrderReference": oref, "IBOrderID": oid, "IBExecID": eid, "LevelOfDetail": level}


def run(env, statements, *, legacy=None, tz="America/New_York", **kw):
    files = [ST.parse_statement(p, tz=tz) for p in statements]
    claims, stats = ST.ws2_claims(env.journal)
    stats = {"ws2_journal": stats}
    if legacy:
        cl, stats["legacy_intents"] = ST.legacy_claims(legacy)
        claims = claims + cl
    return ST.build_report(files, claims, stats, tz=tz, **kw)


def leg(rep, claim_id):
    return next(l for l in rep["legs"] if l["claim_id"] == claim_id)


class Matching(unittest.TestCase):
    def test_exec_id_and_order_ref_matches_make_a_statement_matched_trade(self):
        E = Env(self)
        r1, o1 = E.order(exec_claims=[("E1", "entry"), ("E2", "take_profit")])
        r2, o2 = E.order(symbol="BBB", entry=(20.0, T0, 1.0), exit=(19.0, T0 + timedelta(hours=3), 1.0))
        rows = [ex(eid="E1", oref=o1), ex(side="SELL", px=11.0, t=T0 + timedelta(hours=2), eid="E2", oref=o1),
                # BBB entry in two partial fills (VWAP 20.0), exit by the stop
                ex(sym="BBB", qty=40, px=19.95, comm="-0.4", oref=o2, eid="E3"),
                ex(sym="BBB", qty=60, px=20.0333333333, t=T0 + timedelta(seconds=5), comm="-0.6", oref=o2, eid="E4"),
                ex(sym="BBB", side="SELL", px=19.0, t=T0 + timedelta(hours=3), oref=o2, eid="E5")]
        rep = run(E, [E.flex(rows)])
        self.assertEqual(leg(rep, r1 + ":entry")["key"], "exec_id")
        self.assertEqual(leg(rep, r2 + ":entry")["key"], "order_ref")
        self.assertEqual(leg(rep, r2 + ":entry")["statement"]["executions"], 2)
        self.assertEqual(rep["counts"][ST.MATCHED], 4)
        self.assertEqual(rep["matched_trade_ids"], [r1, r2])
        self.assertEqual(rep["statement_only"], [])
        self.assertEqual(rep["fees"]["counts"]["with_statement_commission"], 4)

    def test_mismatches_are_classified_with_reasons(self):
        E = Env(self)
        ra, oa = E.order(qty=100)
        rb, ob = E.order(symbol="BBB")
        rc, oc = E.order(symbol="CCC", entry=(10.0, T0, 2.5))
        rows = [ex(qty=90, oref=oa, eid="A1"), ex(side="SELL", px=11.0, t=T0 + timedelta(hours=2), oref=oa, eid="A2"),
                ex(sym="BBB", px=10.05, oref=ob, eid="B1"),
                ex(sym="BBB", side="SELL", px=11.0, t=T0 + timedelta(hours=2, minutes=5), oref=ob, eid="B2"),
                ex(sym="CCC", oref=oc, eid="C1"),
                ex(sym="CCC", side="SELL", px=11.0, t=T0 + timedelta(hours=2), oref=oc, eid="C2")]
        rep = run(E, [E.flex(rows)])
        self.assertEqual(leg(rep, ra + ":entry")["class"], ST.MISMATCH)
        self.assertIn("quantity_differs:claimed=100,statement=90", leg(rep, ra + ":entry")["reasons"])
        self.assertTrue(any(x.startswith("price_differs") for x in leg(rep, rb + ":entry")["reasons"]))
        self.assertTrue(any(x.startswith("time_differs_by_300s") for x in leg(rep, rb + ":exit")["reasons"]))
        self.assertIn("fees_differ:claimed=2.5000,statement=1.0000", leg(rep, rc + ":entry")["reasons"])
        self.assertEqual(rep["matched_trade_ids"], [])
        self.assertEqual(rep["fees"]["counts"]["matched_legs"], 2)            # exits of A and C (B's is late)

    def test_unmatched_claim_unrecorded_fill_and_unattributed_execution(self):
        E = Env(self)
        r1, o1 = E.order()
        r2, o2 = E.order(symbol="BBB", entry=(10.0, T0, None), exit=None, state="SUBMITTED")
        rows = [ex(oref=o1, eid="X1"),                                          # exit never on the statement
                ex(sym="BBB", oref=o2, eid="X2"),
                ex(sym="BBB", side="SELL", oref=o2, px=9.0, t=T0 + timedelta(hours=1), eid="X3"),  # unrecorded
                ex(sym="ZZZ", eid="X4"),                                        # not ours
                ex(sym="QQQ", oref="M21L-999", eid="X5")]                       # bot ref, unknown order
        rep = run(E, [E.flex(rows)])
        self.assertEqual(leg(rep, r1 + ":exit")["class"], ST.UNMATCHED_CLAIM)
        self.assertEqual(leg(rep, r2 + ":entry")["class"], ST.MATCHED)
        classes = {(e["symbol"], e["class"]) for e in rep["statement_only"]}
        self.assertEqual(classes, {("BBB", ST.UNRECORDED), ("ZZZ", ST.UNATTRIBUTED), ("QQQ", ST.UNRECORDED)})
        t = {x["trade_id"]: x for x in rep["trades"]}
        self.assertFalse(t[r1]["statement_matched"])
        self.assertIn("exit:UNMATCHED_CLAIM", t[r1]["reasons"])
        self.assertIn("trade_not_closed", t[r2]["reasons"])

    def test_claimed_exec_id_missing_from_statement_is_a_mismatch(self):
        E = Env(self)
        r1, o1 = E.order(exit=None, state="OPEN", exec_claims=[("E1", "entry"), ("E9", "entry")])
        rep = run(E, [E.flex([ex(eid="E1", oref=o1)])])
        self.assertEqual(leg(rep, r1 + ":entry")["class"], ST.MISMATCH)
        self.assertIn("claimed_exec_ids_absent_from_statement:1", leg(rep, r1 + ":entry")["reasons"])

    def test_perm_id_fallback_when_no_order_ref(self):
        E = Env(self)
        r1, _ = E.order(order_ref=False, perm="5551", exit=None, state="OPEN")
        rep = run(E, [E.flex([ex(oid="5551", eid="P1")])])
        self.assertEqual((leg(rep, r1 + ":entry")["class"], leg(rep, r1 + ":entry")["key"]),
                         (ST.MATCHED, "perm_id_as_IBOrderID"))

    def test_time_window_alone_is_only_probable(self):
        E = Env(self)
        r1, _ = E.order(order_ref=False, perm=None)
        rows = [ex(t=T0 + timedelta(seconds=30)), ex(side="SELL", px=11.0, t=T0 + timedelta(hours=2, seconds=10))]
        rep = run(E, [E.flex(rows)])
        self.assertEqual(leg(rep, r1 + ":entry")["class"], ST.PROBABLE)
        self.assertEqual(rep["matched_trade_ids"], [])
        self.assertEqual(rep["fees"]["counts"]["matched_legs"], 0)              # probable legs never calibrate

    def test_simulated_orders_are_excluded(self):
        E = Env(self)
        E.order(source="simulated")
        rep = run(E, [E.flex([ex(sym="ZZZ", eid="Z")])])
        self.assertEqual(rep["claims"]["ws2_journal"]["orders_simulated_excluded"], 1)
        self.assertEqual(rep["claims"]["ws2_journal"]["orders_other_source_excluded"], 0)
        self.assertEqual(rep["legs"], [])

    def test_legacy_claims_reach_probable_at_most(self):
        E = Env(self)
        lg = E.legacy([("AAA", "long", "ibkr_paper", "filled", 10.0, 100, (T0 + timedelta(seconds=20)).isoformat()),
                       ("BBB", "long", "paper", "paper_logged", 10.0, 100, T0.isoformat()),
                       ("CCC", "long", "ibkr_paper", "accepted", None, None, None),
                       ("EEE", "long", "ibkr_paper", "cancelled", 10.0, 100, T0.isoformat()),
                       ("DDD", "short", "ibkr_paper", "filled", 10.0, 50, "2026-10-15T13:35:00")])   # naive
        rep = run(E, [E.flex([ex(eid="L1"), ex(sym="DDD", side="SELL", qty=50, eid="L2")])], legacy=lg)
        self.assertEqual(rep["claims"]["legacy_intents"], {"intents_ibkr": 4, "filled_claims": 2, "not_filled": 2,
                                                           "unusable": 0})
        self.assertEqual(leg(rep, "intent:1")["class"], ST.PROBABLE)
        self.assertEqual(leg(rep, "intent:5")["class"], ST.UNMATCHED_CLAIM)
        self.assertIn("claim_time_naive", leg(rep, "intent:5")["reasons"])


class AccountsAndFees(unittest.TestCase):
    def test_live_account_execution_raises_an_alert_and_never_matches(self):
        E = Env(self)
        r1, o1 = E.order(exit=None, state="OPEN")
        rep = run(E, [E.flex([ex(oref=o1, eid="E1", acct=LIVE), ex(sym="ZZZ", eid="E2", acct=LIVE)])])
        self.assertEqual(rep["alerts"][0]["alert"], ST.LIVE_ALERT)
        self.assertEqual(rep["alerts"][0]["executions"], 2)
        self.assertIn("non_paper_account", leg(rep, r1 + ":entry")["reasons"])
        text = json.dumps(rep)
        self.assertNotIn(LIVE, text)
        self.assertNotIn(PAPER, text)
        self.assertIn("UX***99", text)

    def test_missing_commission_is_never_zero(self):
        E = Env(self)
        r1, o1 = E.order(entry=(10.0, T0, None), exit=(11.0, T0 + timedelta(hours=2), None))
        rows = [ex(oref=o1, eid="E1", comm=""), ex(side="SELL", px=11.0, t=T0 + timedelta(hours=2), oref=o1,
                                                   eid="E2", comm="0")]
        rep = run(E, [E.flex(rows)])
        f = rep["fees"]["counts"]
        self.assertEqual((f["matched_legs"], f["commission_missing"], f["with_statement_commission"],
                          f["zero_commission_reported"]), (2, 1, 1, 1))
        self.assertIsNone(leg(rep, r1 + ":entry")["statement"]["commission"])

    def test_non_usd_commission_is_excluded(self):
        E = Env(self)
        _, o1 = E.order(exit=None, state="OPEN")
        rep = run(E, [E.flex([ex(oref=o1, eid="E1", ccy="EUR")])])
        self.assertEqual(rep["fees"]["counts"]["commission_not_usd"], 1)

    def test_fee_calibration_from_statement_commissions(self):
        E = Env(self)
        rows = []
        for i in range(30):
            t = T0 + timedelta(days=i)
            _, o = E.order(symbol="S%02d" % i, entry=(10.0, t, None), exit=None, state="OPEN")
            fee = order_fees(100, 10.0, "buy", t.date(), plan=IBKR_PRO_FIXED).total * 1.5
            rows.append(ex(sym="S%02d" % i, t=t, comm="-%.6f" % fee, oref=o, eid="F%02d" % i))
        cal = run(E, [E.flex(rows)])["fees"]["calibration"]
        self.assertEqual(cal["fees"]["verdict"], "MODEL_UNDERESTIMATES")
        self.assertAlmostEqual(cal["fees"]["median"], 1.5, places=6)
        self.assertAlmostEqual(cal["suggested_fee_multiplier"], 1.5, places=6)
        self.assertFalse(cal["applied"])
        E2 = Env(self)
        _, o = E2.order(entry=(10.0, T0, None), exit=None, state="OPEN")
        cal2 = run(E2, [E2.flex([ex(oref=o, eid="F")])])["fees"]["calibration"]
        self.assertEqual(cal2["fees"]["verdict"], "INCONCLUSIVE")

    def test_entry_slippage_against_the_reference_price(self):
        E = Env(self)
        _, o = E.order(entry=(10.02, T0, None), exit=None, state="OPEN", ref_price=10.0)
        rep = run(E, [E.flex([ex(px=10.02, oref=o, eid="S1")])])
        self.assertAlmostEqual(rep["entry_slippage_vs_reference_bps"]["median"], 20.0, places=6)
        E2 = Env(self)                                    # a short entry filled below the reference is adverse
        _, o2 = E2.order(direction="short", entry=(9.98, T0, None), exit=None, state="OPEN", ref_price=10.0)
        rep2 = run(E2, [E2.flex([ex(side="SELL", px=9.98, oref=o2, eid="S2")])])
        self.assertAlmostEqual(rep2["entry_slippage_vs_reference_bps"]["median"], 20.0, places=6)


class AccountIdentityAndEdgeCases(unittest.TestCase):
    """Release-safety review (30 Oct): only positively verified PAPER executions can support paper evidence."""
    EXIT_T = T0 + timedelta(hours=2)

    def closed(self, E, **kw):
        return E.order(**kw)

    def pair(self, oref, *, acct=PAPER, sym="AAA", eid="E", asset="STK", ccy="USD"):
        a = ex(sym=sym, oref=oref, eid=eid + "1", acct=acct, ccy=ccy)
        b = ex(sym=sym, side="SELL", px=11.0, t=self.EXIT_T, oref=oref, eid=eid + "2", acct=acct, ccy=ccy)
        a["AssetClass"] = b["AssetClass"] = asset
        return [a, b]

    def test_blank_account_is_never_paper_evidence(self):
        E = Env(self)
        r1, o1 = self.closed(E)
        rep = run(E, [E.flex(self.pair(o1, acct=""))])
        self.assertEqual(leg(rep, r1 + ":entry")["class"], ST.MISMATCH)
        self.assertIn("account_unverified", leg(rep, r1 + ":entry")["reasons"])
        self.assertEqual(rep["matched_trade_ids"], [])
        self.assertEqual(rep["fees"]["counts"]["matched_legs"], 0)
        self.assertIn("ACCOUNT_UNVERIFIED_EXECUTIONS", [a["alert"] for a in rep["alerts"]])

    def test_statement_without_an_account_column_never_matches(self):
        E = Env(self)
        r1, o1 = self.closed(E)
        head = [h for h in FLEX_HEAD if h != "ClientAccountID"]
        rep = run(E, [E.flex(self.pair(o1), head=head)])
        self.assertEqual(rep["matched_trade_ids"], [])
        self.assertIn("account_unverified", leg(rep, r1 + ":exit")["reasons"])

    def test_execution_on_another_paper_account_does_not_match_the_journal_account(self):
        E = Env(self)
        r1, o1 = self.closed(E, account="DUX0000001")
        rep = run(E, [E.flex(self.pair(o1, acct="DUX0000002"))])
        self.assertIn("account_differs_from_journal", leg(rep, r1 + ":entry")["reasons"])
        self.assertEqual(rep["matched_trade_ids"], [])
        E2 = Env(self)
        r2, o2 = self.closed(E2, account="DUX0000001")
        rep2 = run(E2, [E2.flex(self.pair(o2, acct="DUX0000001"))])
        self.assertEqual(rep2["matched_trade_ids"], [r2])

    def test_live_and_paper_in_one_export_with_identical_fills(self):
        E = Env(self)
        r1, _ = self.closed(E, order_ref=False, perm=None)
        rows = self.pair("", acct=PAPER, eid="P") + self.pair("", acct=LIVE, eid="L")
        rep = run(E, [E.flex(rows)])
        self.assertNotEqual(leg(rep, r1 + ":entry")["class"], ST.MATCHED)
        self.assertEqual(rep["matched_trade_ids"], [])
        self.assertIn(ST.LIVE_ALERT, [a["alert"] for a in rep["alerts"]])
        live = [e for e in rep["statement_only"] if e["account"] == "UX***99"]
        self.assertTrue(live and all(e["class"] != ST.MATCHED for e in live))
        r2, o2 = self.closed(E, symbol="BBB")
        rep2 = run(E, [E.flex(self.pair(o2, acct=LIVE, sym="BBB", eid="Q"), name="l2.csv")])
        self.assertEqual(leg(rep2, r2 + ":entry")["class"], ST.MISMATCH)
        self.assertIn("non_paper_account", leg(rep2, r2 + ":entry")["reasons"])

    def test_unknown_asset_class_is_not_evidence(self):
        E = Env(self)
        r1, o1 = self.closed(E)
        rep = run(E, [E.flex(self.pair(o1, asset="OPT"))])
        self.assertIn("asset_class_unverified", leg(rep, r1 + ":entry")["reasons"])
        E2 = Env(self)
        r2, o2 = self.closed(E2)
        rep2 = run(E2, [E2.flex(self.pair(o2, asset=""))])
        self.assertEqual(rep2["matched_trade_ids"], [])

    def test_missing_commission_currency_is_unknown_not_usd(self):
        E = Env(self)
        _, o1 = self.closed(E)
        rep = run(E, [E.flex(self.pair(o1, ccy=""))])
        f = rep["fees"]["counts"]
        self.assertEqual((f["matched_legs"], f["with_statement_commission"], f["commission_currency_unknown"]),
                         (2, 0, 2))

    def test_a_cancelled_execution_cannot_support_a_match(self):
        E = Env(self)
        r1, o1 = self.closed(E)
        rows = self.pair(o1)
        corr = ex(oref=o1, eid="C1", side="SELL")             # IBKR reverses the BUY with a cancellation row
        corr["Buy/Sell"] = "SELL (Ca.)"
        rep = run(E, [E.flex(rows + [corr])])
        self.assertEqual(leg(rep, r1 + ":entry")["class"], ST.MISMATCH)
        self.assertIn("correction_present_requires_review", leg(rep, r1 + ":entry")["reasons"])
        self.assertEqual(rep["matched_trade_ids"], [])

    def test_ambiguous_order_ref_and_perm_id(self):
        E = Env(self)
        _, o1 = self.closed(E)
        c = sqlite3.connect(E.journal)
        with self.assertRaises(sqlite3.IntegrityError):           # the journal itself refuses a reused orderRef
            with c:
                c.execute("INSERT INTO loop_orders (client_ref, cycle_key, symbol, direction, qty, state, "
                          "data_source, planned_at_utc, updated_at_utc, order_ref) "
                          "VALUES ('R900','scan:y','AAA','long',100,'OPEN','ibkr_paper',?,?,?)",
                          (T0.isoformat(), T0.isoformat(), o1))
        c.close()
        E2 = Env(self)
        r2, _ = self.closed(E2, order_ref=False, perm="777", exit=None, state="OPEN")
        r3, _ = self.closed(E2, order_ref=False, perm="777", symbol="BBB", exit=None, state="OPEN")
        rep2 = run(E2, [E2.flex([ex(oid="777", eid="X1"), ex(oid="777", eid="X2", t=T0 + timedelta(seconds=1))])])
        for r in (r2, r3):
            self.assertNotEqual(leg(rep2, r + ":entry")["class"], ST.MATCHED)
        self.assertIn("ambiguous_order_key", leg(rep2, r2 + ":entry")["reasons"])

    def test_entry_split_across_two_broker_orders_is_ambiguous(self):
        E = Env(self)
        r1, o1 = self.closed(E)
        a = ex(qty=40, oref=o1, oid="501", eid="A1")
        b = ex(qty=60, oref=o1, oid="502", eid="A2", t=T0 + timedelta(seconds=2))
        x = ex(side="SELL", px=11.0, t=self.EXIT_T, oref=o1, oid="503", eid="A3")
        rep = run(E, [E.flex([a, b, x])])
        self.assertIn("entry_spans_multiple_broker_orders", leg(rep, r1 + ":entry")["reasons"])

    def test_partial_fills_on_both_legs_still_match(self):
        E = Env(self)
        r1, o1 = self.closed(E)
        rows = [ex(qty=30, px=9.9, oref=o1, oid="601", eid="B1", comm="-0.3"),
                ex(qty=70, px=10.0428571428, oref=o1, oid="601", eid="B2", t=T0 + timedelta(seconds=3), comm="-0.7"),
                ex(side="SELL", qty=50, px=11.0, t=self.EXIT_T, oref=o1, oid="602", eid="B3", comm="-0.5"),  # TP part
                ex(side="SELL", qty=50, px=11.0, t=self.EXIT_T + timedelta(seconds=30), oref=o1, oid="603",
                   eid="B4", comm="-0.5")]                                                      # stop for the rest
        rep = run(E, [E.flex(rows)])
        self.assertEqual(rep["matched_trade_ids"], [r1])
        self.assertEqual(leg(rep, r1 + ":entry")["statement"]["executions"], 2)
        self.assertEqual(leg(rep, r1 + ":exit")["statement"]["executions"], 2)

    def test_duplicate_rows_across_files_are_refused(self):
        E = Env(self)
        _, o1 = self.closed(E)
        head = [h for h in FLEX_HEAD if h != "IBExecID"]
        rows = [dict(r) for r in self.pair(o1)]
        a, b = E.flex(rows, name="a.csv", head=head), E.flex(rows, name="b.csv", head=head)
        with self.assertRaisesRegex(ST.StatementRefused, "more than one statement file"):
            run(E, [a, b])
        with self.assertRaisesRegex(ST.StatementRefused, "repeated"):
            ST.parse_statement(E.flex([dict(ex(oid="9", oref=o1)), dict(ex(oid="9", oref=o1))], name="d.csv",
                                      head=head + ["IBOrderID"] if "IBOrderID" not in head else head),
                               tz="America/New_York")


class Parsing(unittest.TestCase):
    def _write(self, E, text, name="s.csv"):
        p = os.path.join(E.dir, name)
        with open(p, "w") as fh:
            fh.write(text)
        return p

    def test_refusals(self):
        E = Env(self)
        ok = E.flex([ex(eid="E1")])
        with self.assertRaises(ST.StatementRefused):
            ST.parse_statement(ok, tz="")
        with self.assertRaises(ST.StatementRefused):
            ST.parse_statement(ok, tz="Mars/Olympus")
        for bad, msg in (({"DateTime": "10/15/2026 09:35:00"}, "unambiguous"),
                         ({"TradePrice": "abc"}, "not a number"),
                         ({"Quantity": "0"}, "zero quantity"),
                         ({"Buy/Sell": "SELL"}, "contradicts"),
                         ({"TradePrice": "-1"}, "positive")):
            row = ex(eid="E1")
            row.update(bad)
            with self.assertRaisesRegex(ST.StatementRefused, msg):
                ST.parse_statement(E.flex([row], name="bad.csv"), tz="America/New_York")
        with self.assertRaisesRegex(ST.StatementRefused, "repeated"):
            ST.parse_statement(E.flex([ex(eid="D"), ex(eid="D")], name="dup.csv"), tz="America/New_York")
        with self.assertRaisesRegex(ST.StatementRefused, "required column"):
            ST.parse_statement(E.flex([ex()], name="nop.csv", head=[h for h in FLEX_HEAD if h != "TradePrice"]),
                               tz="America/New_York")

    def test_overlapping_files_refused(self):
        E = Env(self)
        a, b = E.flex([ex(eid="E1")], name="a.csv"), E.flex([ex(eid="E1")], name="b.csv")
        with self.assertRaisesRegex(ST.StatementRefused, "more than one statement file"):
            run(E, [a, b])

    def test_statement_time_zone_is_applied(self):
        E = Env(self)
        e = ST.parse_statement(E.flex([ex(eid="E1")]), tz="America/New_York").executions[0]
        self.assertEqual(e.time_utc, T0)
        e2 = ST.parse_statement(E.flex([ex(eid="E1")], name="u.csv"), tz="UTC").executions[0]
        self.assertEqual(e2.time_utc, T0 - timedelta(hours=4))

    def test_execution_level_preferred_and_corrections_excluded(self):
        E = Env(self)
        rows = [ex(eid="E1"), ex(eid="E2", t=T0 + timedelta(seconds=1)), ex(qty=200, level="ORDER"),
                ex(sym="BBB", eid="E3")]
        rows[3]["Buy/Sell"] = "BUY (Ca.)"
        sf = ST.parse_statement(E.flex(rows), tz="America/New_York")
        self.assertEqual((sf.level_used, sf.rows_used, sf.rows_ignored_by_level, sf.correction_rows),
                         ("EXECUTION", 2, 1, 1))

    def test_flex_record_type_layout(self):
        E = Env(self)
        head = ["Symbol", "Buy/Sell", "Quantity", "TradePrice", "IBCommission", "DateTime", "IBExecID"]
        text = ('"BOF","X","Flex"\n"BOS","TRNT","Trades"\n"HEADER","TRNT",' + ",".join('"%s"' % h for h in head) +
                '\n"DATA","TRNT","AAA","BUY","100","10","-1","20261015;093500","E1"\n"EOS","TRNT"\n"EOF","X"\n')
        sf = ST.parse_statement(self._write(E, text), tz="America/New_York")
        self.assertEqual((sf.format, sf.rows_used, sf.executions[0].exec_id, sf.executions[0].commission),
                         ("flex", 1, "E1", 1.0))
        self.assertEqual(sf.executions[0].time_utc, T0)

    def test_activity_statement_layout(self):
        E = Env(self)
        text = ('Statement,Header,Field Name,Field Value\nStatement,Data,Period,"October 15, 2026"\n'
                'Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,'
                'C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code\n'
                'Trades,Data,Order,Stocks,USD,AAA,"2026-10-15, 09:35:00",100,10,10.1,-1000,-1,1001,0,10,O\n'
                'Trades,Data,Order,Stocks,USD,AAA,"2026-10-15, 11:35:00",-100,11,11,1100,-1,-1001,98,0,C\n'
                'Trades,SubTotal,,Stocks,USD,AAA,,0,,,100,-2,0,98,10,\n')
        sf = ST.parse_statement(self._write(E, text), tz="America/New_York")
        self.assertEqual((sf.format, sf.level_used, sf.rows_used), ("activity", "ORDER", 2))
        self.assertEqual([x.side for x in sf.executions], ["BUY", "SELL"])
        self.assertIsNone(sf.executions[0].exec_id)
        r1, _ = E.order(order_ref=False, perm=None)
        rep = ST.build_report([sf], *[(c, {"ws2_journal": s}) for c, s in [ST.ws2_claims(E.journal)]][0],
                              tz="America/New_York")
        self.assertEqual(leg(rep, r1 + ":entry")["class"], ST.PROBABLE)
        self.assertIn("account_unverified", leg(rep, r1 + ":entry")["reasons"])     # no account column
        self.assertIn("NO_EXECUTION_IDS_IN_SOME_FILES", [a["alert"] for a in rep["alerts"]])
        self.assertIn("ACCOUNT_UNVERIFIED_EXECUTIONS", [a["alert"] for a in rep["alerts"]])


class Cli(unittest.TestCase):
    def test_cli_is_read_only_and_writes_matched_ids(self):
        E = Env(self)
        r1, o1 = E.order()
        p = E.flex([ex(oref=o1, eid="E1"), ex(side="SELL", px=11.0, t=T0 + timedelta(hours=2), oref=o1, eid="E2")])
        before = sha(E.journal)
        oj, om = os.path.join(E.dir, "r.json"), os.path.join(E.dir, "r.md")
        rc = quiet(ST.main, ["--statement", p, "--statement-tz", "America/New_York", "--journal-db", E.journal,
                             "--out-json", oj, "--out-md", om])
        self.assertEqual(rc, 0)
        self.assertEqual(sha(E.journal), before)
        with open(oj) as fh:
            rep = json.load(fh)
        self.assertEqual((rep["schema"], rep["matched_trade_ids"]), (ST.SCHEMA, [r1]))
        with open(om) as fh:
            self.assertIn("Statement-matched closed trades: 1 of 1", fh.read())

    def test_cli_refusals(self):
        E = Env(self)
        p = E.flex([ex(eid="E1")])
        self.assertEqual(quiet(ST.main, ["--statement", p, "--statement-tz", "America/New_York"]), 2)
        missing = os.path.join(E.dir, "absent.db")
        self.assertEqual(quiet(ST.main, ["--statement", p, "--statement-tz", "America/New_York",
                                         "--journal-db", missing]), 2)
        self.assertFalse(os.path.exists(missing))
        with self.assertRaises(SystemExit):
            quiet(ST.main, ["--statement", p, "--journal-db", E.journal])          # no time zone


if __name__ == "__main__":
    unittest.main()
