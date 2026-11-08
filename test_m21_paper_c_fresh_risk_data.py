"""test_m21_paper_c_fresh_risk_data.py: M21 PR-c (BLK-3), fresh risk data or no entry.

1. M14 enforces reading ages (pnl_max_age_sec / exposure_max_age_sec) against each
   reading's fetch time, over the entry's aggregation domain. Missing, unparseable,
   naive, future or too-old readings block. Thresholds are unchanged.
2. The snapshot carries the fetch times the real ingesters record.
3. The paper loop's risk gate refreshes the paper account's exposure and P&L before
   every M14 call, fail-closed, serialised by a file lock, ibkr_paper only.
4. Races: a refresh that wrote nothing, data that ages during the decision, and
   concurrent refreshes all end in a block (or a serialised refresh), never in an
   entry on stale data.

No socket, no gateway, no broker: refresh child processes are replaced by fakes.
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import test_m14_e_engine as E  # noqa: E402
from bot.risk_authority import engine as ENG  # noqa: E402
from bot.risk_authority.engine import RiskPolicyView, decide, load_policy_view_from_env  # noqa: E402
from bot.risk_authority.snapshot import ALL_BROKER_SCOPES, _load_scope_row  # noqa: E402
from bot.paper_loop_d2.risk_refresh import PaperRiskRefresher  # noqa: E402


def iso(delta_sec=0.0, *, tz=timezone.utc):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_sec)).astimezone(tz).isoformat()


def snap(**per_scope):
    """per_scope: scope -> dict of _scope() overrides."""
    scopes = {s: E._scope(scope=s, **per_scope.get(s, {})) for s in ALL_BROKER_SCOPES}
    return E._snapshot(scopes_override=scopes)


def run(scope="ibkr_paper", s=None, **pol):
    return decide(E._ctx(scope=scope), s or snap(), E._req(), policy=E._baseline_policy(**pol))


# ─── 1. M14 reading-age enforcement ─────────────────────────────────────────
class EngineAges(unittest.TestCase):
    def assertBlocked(self, d, reason):
        self.assertEqual(d.result, "block")
        self.assertIn(reason, d.reason_codes)

    def test_fresh_readings_allow(self):
        self.assertEqual(run().result, "allow")

    def test_thresholds_unchanged(self):
        self.assertEqual((RiskPolicyView().pnl_max_age_sec, RiskPolicyView().exposure_max_age_sec),
                         (300, 120))
        with mock.patch.dict(os.environ, {}, clear=True):
            v = load_policy_view_from_env()
        self.assertEqual((v.pnl_max_age_sec, v.exposure_max_age_sec), (300, 120))

    def test_same_day_status_from_hours_ago_no_longer_passes(self):
        # BLK-3: before PR-c a 'fresh' status passed all day.
        s = snap(ibkr_paper=dict(pnl_fetched_at=iso(-7200), exposure_fetched_at=iso(-7200)))
        self.assertBlocked(run(s=s), "exposure_stale")

    def test_exposure_age_boundary(self):
        self.assertEqual(run(s=snap(ibkr_paper=dict(exposure_fetched_at=iso(-110)))).result, "allow")
        self.assertBlocked(run(s=snap(ibkr_paper=dict(exposure_fetched_at=iso(-125)))),
                           "exposure_stale")

    def test_pnl_age_boundary(self):
        self.assertEqual(run(s=snap(ibkr_paper=dict(pnl_fetched_at=iso(-290)))).result, "allow")
        self.assertBlocked(run(s=snap(ibkr_paper=dict(pnl_fetched_at=iso(-305)))),
                           "daily_pnl_unknown")

    def test_missing_or_bad_timestamps_block(self):
        naive_now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        for bad in (None, "", "garbage", naive_now, 1731060000, iso(+60)):
            self.assertBlocked(run(s=snap(ibkr_paper=dict(exposure_fetched_at=bad))),
                               "exposure_stale")
            self.assertBlocked(run(s=snap(ibkr_paper=dict(pnl_fetched_at=bad))),
                               "daily_pnl_unknown")

    def test_small_skew_z_suffix_and_other_offsets_accepted(self):
        z = (datetime.now(timezone.utc) - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        athens = iso(-10, tz=timezone(timedelta(hours=2)))
        for ok in (iso(+3), z, athens):
            self.assertEqual(run(s=snap(ibkr_paper=dict(exposure_fetched_at=ok,
                                                        pnl_fetched_at=ok))).result, "allow", ok)

    def test_bad_limits_block(self):
        for bad in (0, -1, "300", True, None, float("nan")):
            self.assertBlocked(run(pnl_max_age_sec=bad), "daily_pnl_unknown")
            self.assertBlocked(run(exposure_max_age_sec=bad), "exposure_stale")

    def test_tighter_operator_limit_is_respected(self):
        s = snap(ibkr_paper=dict(exposure_fetched_at=iso(-40)))
        self.assertBlocked(run(s=s, exposure_max_age_sec=30), "exposure_stale")

    def test_paper_entry_ignores_real_accounts_ages(self):
        stale = dict(pnl_fetched_at=None, exposure_fetched_at=iso(-9999))
        s = snap(ibkr_live=stale, etoro_real=stale, etoro_paper=stale)
        self.assertEqual(run("ibkr_paper", s=s).result, "allow")

    def test_real_entry_needs_every_domain_reading_fresh(self):
        s = snap(ibkr_paper=dict(exposure_fetched_at=None))
        self.assertBlocked(run("ibkr_live", s=s), "combined_exposure_unknown")
        s = snap(etoro_paper=dict(pnl_fetched_at=iso(-400)))
        self.assertBlocked(run("ibkr_live", s=s), "global_daily_loss_unknown")
        self.assertEqual(run("ibkr_live").result, "allow")

    def test_query_authority_not_affected(self):
        s = snap(ibkr_paper=dict(exposure_fetched_at=None, pnl_fetched_at=None))
        d = decide(E._ctx(action="query_authority"), s, None, policy=E._baseline_policy())
        self.assertNotIn("exposure_stale", d.reason_codes)
        self.assertNotIn("daily_pnl_unknown", d.reason_codes)

    def test_decision_time_is_the_engine_clock(self):
        s = snap()
        later = datetime.now(timezone.utc) + timedelta(seconds=301)

        class _Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return later
        with mock.patch.object(ENG, "datetime", _Later):
            self.assertBlocked(run(s=s), "exposure_stale")


# ─── 2. snapshot carries the real ingesters' fetch times ────────────────────
class SnapshotTimes(unittest.TestCase):
    def setUp(self):
        import test_m14_f_preflight as F
        self.F = F
        self.fx = F._DB()
        self.addCleanup(self.fx.cleanup)

    def _row(self, lifecycle):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self.fx.conn() as c:
            c.execute("INSERT INTO daily_state_per_broker (date, broker_scope, lifecycle_json, "
                      "updated_at) VALUES (?,?,?,?)",
                      (today, "ibkr_paper", json.dumps(lifecycle), iso()))
            return _load_scope_row(c, today, "ibkr_paper")

    def test_fetch_times_read_from_lifecycle(self):
        sv = self._row({"latest_reading": {"fetched_at_utc": "A"},
                        "exposure_latest_reading": {"fetched_at_utc": "B"}})
        self.assertEqual((sv.pnl_fetched_at_utc, sv.exposure_fetched_at_utc), ("A", "B"))

    def test_missing_or_malformed_summaries_are_none(self):
        for lc in ({}, {"latest_reading": "x", "exposure_latest_reading": [1]},
                   {"latest_reading": {"fetched_at_utc": 5}}):
            with self.subTest(lc=lc):
                self.fx.cleanup()
                self.fx = self.F._DB()
                sv = self._row(lc)
                self.assertIsNone(sv.pnl_fetched_at_utc)
                self.assertIsNone(sv.exposure_fetched_at_utc)

    def test_absent_row_is_none(self):
        with self.fx.conn() as c:
            sv = _load_scope_row(c, "2000-01-01", "ibkr_paper")
        self.assertIsNone(sv.pnl_fetched_at_utc)


def write_fresh_paper_state(db_path):
    """What a successful refresh does: the REAL M14.C / M14.D writers, fed by fake
    adapters for a flat paper account with no trades today."""
    from bot.risk_authority.ingest import ingest_once
    from bot.risk_authority.ingest_exposure import ingest_exposure_once
    from bot.risk_authority.ingest_ibkr import IBKRPnLAdapter
    from bot.risk_authority.ingest_ibkr_exposure import IBKRExposureAdapter
    conn = sqlite3.connect(db_path)
    try:
        ingest_exposure_once(conn, scope="ibkr_paper",
                             adapter=IBKRExposureAdapter("ibkr_paper", positions_reader=lambda: []))
        ingest_once(conn, scope="ibkr_paper",
                    adapter=IBKRPnLAdapter(broker_scope="ibkr_paper",
                                           executions_reader=lambda d: []))
    finally:
        conn.close()


# ─── 3/4. the loop's risk gate with the real M14 and a refresher ────────────
class GateWithRefresh(unittest.TestCase):
    def setUp(self):
        import test_m14_f_preflight as F
        from bot.broker_allocation import save_policy
        p = mock.patch.dict(os.environ, {"ALPACA_KEY": "test-key", "ALPACA_SECRET": "test-secret"})
        p.start()
        self.addCleanup(p.stop)
        self.fx = F._DB()
        self.addCleanup(self.fx.cleanup)
        lane = {"ibkr_paper.auto_trading_enabled": True,
                "ibkr_paper.auto_trading_enabled_until_utc": F._future_utc_iso()}
        with self.fx.conn() as c:
            save_policy(c, F._valid_policy(**lane))
        self.db = self.fx.path

    def gate(self, refresher, **kw):
        from bot.paper_loop_d2.risk_gate import make_risk_authority_gate
        from test_m21_1extra_d2_3_learning_loop import MS_NOW, market_state
        return make_risk_authority_gate(conn_factory=lambda: sqlite3.connect(self.db),
                                        market_state=market_state(**kw), clock=lambda: MS_NOW,
                                        refresher=refresher)

    def call(self, g):
        return g(symbol="AAA", side="long", notional_usd=100.0, stop_loss=95.0, take_profit=110.0)

    def decisions(self):
        with sqlite3.connect(self.db) as c:
            return c.execute("SELECT COUNT(*) FROM risk_decisions").fetchone()[0]

    def test_refresh_writing_fresh_state_allows(self):
        ok, why, did = self.call(self.gate(lambda: (write_fresh_paper_state(self.db), (True, "refreshed"))[1]))
        self.assertEqual((ok, why), (True, "risk_authority_allow"))
        self.assertTrue(did)

    def test_refresh_refusal_blocks_before_m14(self):
        for result, frag in (((False, "pnl_refresh_rc_1"), "risk_refresh_block:pnl_refresh_rc_1"),
                             ((True,), "risk_refresh_block:invalid_result"),
                             (("True", "x"), "risk_refresh_block:x"),
                             ((1, "y"), "risk_refresh_block:y"),
                             (None, "risk_refresh_block:invalid_result")):
            with mock.patch("bot.risk_authority.run_risk_preflight",
                            side_effect=AssertionError("M14 must not be called")):
                ok, why, did = self.call(self.gate(lambda r=result: r))
            self.assertEqual((ok, why, did), (False, frag, None))
        self.assertEqual(self.decisions(), 0)

    def test_refresher_exception_blocks(self):
        def boom():
            raise RuntimeError("x")
        self.assertEqual(self.call(self.gate(boom)), (False, "risk_refresh_error:RuntimeError", None))

    def test_market_closed_means_no_refresh(self):
        calls = []
        g = self.gate(lambda: (calls.append(1), (True, "refreshed"))[1], is_open=False)
        ok, why, _ = self.call(g)
        self.assertFalse(ok)
        self.assertTrue(why.startswith("market_state_block"))
        self.assertEqual(calls, [])

    def test_race_refresh_reported_ok_but_wrote_nothing(self):
        # Data from 10 minutes ago and a refresher that claims success: M14 blocks.
        import test_m14_f_preflight as F
        with self.fx.conn() as c:
            F._seed_fresh_state(c, scope="ibkr_paper", fetched_at=iso(-600))
        ok, why, did = self.call(self.gate(lambda: (True, "refreshed")))
        self.assertFalse(ok)
        self.assertEqual(why, "risk_authority_block:exposure_stale")
        self.assertTrue(did)                                    # M14 recorded the block

    def test_race_pnl_refresh_stale_while_exposure_fresh(self):
        def refresher():
            write_fresh_paper_state(self.db)
            with sqlite3.connect(self.db) as c:                 # P&L reading from 6 minutes ago
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                lc = json.loads(c.execute("SELECT lifecycle_json FROM daily_state_per_broker "
                                          "WHERE date=? AND broker_scope='ibkr_paper'",
                                          (today,)).fetchone()[0])
                lc["latest_reading"]["fetched_at_utc"] = iso(-360)
                c.execute("UPDATE daily_state_per_broker SET lifecycle_json=? WHERE date=? "
                          "AND broker_scope='ibkr_paper'", (json.dumps(lc), today))
            return True, "refreshed"
        ok, why, _ = self.call(self.gate(refresher))
        self.assertEqual((ok, why), (False, "risk_authority_block:daily_pnl_unknown"))

    def test_race_data_ages_during_the_decision(self):
        later = datetime.now(timezone.utc) + timedelta(seconds=180)

        class _Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return later
        g = self.gate(lambda: (write_fresh_paper_state(self.db), (True, "refreshed"))[1])
        with mock.patch.object(ENG, "datetime", _Later):
            ok, why, _ = self.call(g)
        self.assertEqual((ok, why), (False, "risk_authority_block:exposure_stale"))

    def test_unknown_pnl_from_refresh_blocks(self):
        def refresher():
            from bot.risk_authority.ingest import ingest_once
            from bot.risk_authority.ingest_exposure import ingest_exposure_once
            from bot.risk_authority.ingest_ibkr import IBKRPnLAdapter
            from bot.risk_authority.ingest_ibkr_exposure import IBKRExposureAdapter

            def bad(day):
                raise RuntimeError("gateway")
            conn = sqlite3.connect(self.db)
            try:
                ingest_exposure_once(conn, scope="ibkr_paper", adapter=IBKRExposureAdapter(
                    "ibkr_paper", positions_reader=lambda: []))
                ingest_once(conn, scope="ibkr_paper", adapter=IBKRPnLAdapter(
                    broker_scope="ibkr_paper", executions_reader=bad))
            finally:
                conn.close()
            return True, "refreshed"                            # even if it lied
        ok, why, _ = self.call(self.gate(refresher))
        self.assertEqual((ok, why), (False, "risk_authority_block:daily_pnl_unknown"))

    def test_non_callable_refresher_rejected(self):
        with self.assertRaises(ValueError):
            self.gate("not callable")


# ─── 3. the refresher itself ───────────────────────────────────────────────
class _Proc:
    def __init__(self, rc):
        self.returncode, self.stdout, self.stderr = rc, "", ""


class Refresher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = os.path.join(self.tmp.name, "r.lock")
        self.calls = []

    def make(self, rcs=(0, 0), **kw):
        rcs = list(rcs)

        def runner(argv, **k):
            self.calls.append((argv, k))
            r = rcs.pop(0)
            if isinstance(r, BaseException):
                raise r
            return _Proc(r)
        kw.setdefault("lock_wait_sec", 0.3)
        return PaperRiskRefresher(repo=REPO, db_path="/x/signals.db", lock_path=self.lock,
                                  python="/py", runner=runner, **kw)

    def test_commands_are_paper_only_read_only_clis(self):
        cmds = self.make().commands()
        self.assertEqual([n for n, _ in cmds], ["exposure", "pnl"])
        for _, argv in cmds:
            self.assertEqual(argv[:3], ["/py", "-I", "-B"])
            self.assertEqual(argv[-4:], ["--scope", "ibkr_paper", "--db", "/x/signals.db"])
            self.assertNotIn("ibkr_live", " ".join(argv))
            self.assertNotIn("--all", argv)
        self.assertTrue(cmds[0][1][3].endswith("tools/ingest_exposure_state.py"))
        self.assertTrue(cmds[1][1][3].endswith("tools/ingest_risk_state.py"))

    def test_both_ok(self):
        r = self.make()
        self.assertEqual(r(), (True, "refreshed"))
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(k["timeout"] == 90.0 for _, k in self.calls))

    def test_first_failure_stops(self):
        self.assertEqual(self.make((1, 0))(), (False, "exposure_refresh_rc_1"))
        self.assertEqual(len(self.calls), 1)

    def test_pnl_failure_codes(self):
        self.assertEqual(self.make((0, 1))(), (False, "pnl_refresh_rc_1"))
        self.assertEqual(self.make((0, 2))(), (False, "pnl_refresh_rc_2"))

    def test_timeout_and_spawn_errors(self):
        import subprocess
        self.assertEqual(self.make((0, subprocess.TimeoutExpired("x", 1)))(),
                         (False, "pnl_refresh_timeout"))
        self.assertEqual(self.make((OSError("no python"),))(),
                         (False, "exposure_refresh_error:OSError"))

    def test_lock_busy_blocks_without_running(self):
        import fcntl
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            t0 = time.monotonic()
            self.assertEqual(self.make()(), (False, "refresh_lock_busy"))
            self.assertGreaterEqual(time.monotonic() - t0, 0.25)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertEqual(self.calls, [])

    def test_lock_released_after_failure(self):
        self.assertFalse(self.make((1,))()[0])
        self.assertEqual(self.make()(), (True, "refreshed"))

    def test_concurrent_refreshes_are_serialised(self):
        spans, guard = [], threading.Lock()

        def runner(argv, **k):
            t0 = time.monotonic()
            time.sleep(0.05)
            with guard:
                spans.append((t0, time.monotonic()))
            return _Proc(0)
        refs = [PaperRiskRefresher(repo=REPO, db_path="/x", lock_path=self.lock, runner=runner,
                                   lock_wait_sec=5) for _ in range(3)]
        out = []
        ts = [threading.Thread(target=lambda r=r: out.append(r())) for r in refs]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(out, [(True, "refreshed")] * 3)
        # each refresh = two consecutive spans; refreshes never interleave
        spans.sort()
        pairs = [spans[i:i + 2] for i in range(0, 6, 2)]
        for a, b in zip(pairs, pairs[1:]):
            self.assertLessEqual(a[1][1], b[0][0] + 1e-6)

    def test_constructor_validation(self):
        with self.assertRaises(ValueError):
            PaperRiskRefresher(repo=REPO, db_path="", lock_path=self.lock)
        with self.assertRaises(ValueError):
            PaperRiskRefresher(repo=REPO, db_path="/x", lock_path=self.lock, timeout_sec=0)


# ─── wiring and source checks ──────────────────────────────────────────────
class Wiring(unittest.TestCase):
    def test_scheduler_wires_the_refresher_into_the_real_gate(self):
        src = (REPO / "tools/paper_loop/m21_1extra_d2_3_scheduler.py").read_text()
        calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", "") == "make_risk_authority_gate"]
        self.assertEqual(len(calls), 1)
        kws = {k.arg: k.value for k in calls[0].keywords}
        self.assertIn("refresher", kws)
        self.assertEqual(getattr(kws["refresher"].func, "id", ""), "make_paper_risk_refresher")

    def test_refresher_module_has_no_broker_or_order_surface(self):
        tree = ast.parse((REPO / "bot/paper_loop_d2/risk_refresh.py").read_text())
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertFalse({m for m in mods if m.startswith(("ib_insync", "bot.brokers", "bot.etoro",
                                                           "requests", "socket"))})
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertFalse({"placeOrder", "cancelOrder", "connect"} & names)
        consts = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)}
        self.assertNotIn("ibkr_live", consts)
        self.assertNotIn("--all", consts)


if __name__ == "__main__":
    unittest.main()
