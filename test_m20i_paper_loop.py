"""M20.I — runtime paper loop tests. Simulation-only; no live/broker imports,
no execution_intents writes, no log_intent."""
import pathlib
import unittest

import bot.runtime.paper_loop as loop
from bot.runtime.paper_loop import run_paper_loop, PaperLoopResult, PaperLoopOutcome
from bot.paper import new_account
from bot.signal_scoring.schema import (
    ScoredSignalCandidate, SignalSide, DecisionBucket)


def _acct(equity=100000.0):
    return new_account(starting_equity=equity,
                       as_of_utc="2026-06-24T00:00:00+00:00").account_state


def _sig(symbol="AAPL", direction="long", entry=200.0, stop=196.0,
         target=208.0):
    return {"timestamp": "2026-06-24T15:00:00+00:00", "symbol": symbol,
            "direction": direction, "entry_price": entry, "stop_loss": stop,
            "target_price": target, "valid_count": 4, "available_tfs": 4,
            "rsi": 55.0, "macd_hist": 0.5, "atr": 3.0, "vol_ratio": 1.2,
            "strategy_version": 1}


def _eligible_candidate(symbol="AAPL"):
    return ScoredSignalCandidate(
        symbol=symbol, side=SignalSide.LONG,
        signal_timestamp_utc="2026-06-24T15:00:00+00:00",
        candidate_id=f"cand-{symbol}", decision_bucket=DecisionBucket.ELIGIBLE,
        execution_eligible=False, hard_gate_passed=True,
        final_score=80.0, final_score_100=80.0)


class M20IRealChainRunsAndRejects(unittest.TestCase):
    """The full real chain (M19 adapter+scoring -> routing) runs without error
    and rejects cleanly when M19 gates/short-side block routing."""

    def test_short_signal_skipped(self):
        res = run_paper_loop([_sig("TSLA", "short")], _acct(),
                             evaluated_at_utc="2026-06-24T15:00:00+00:00")
        self.assertEqual(res.opened_count, 0)
        self.assertEqual(res.skipped_ineligible, 1)
        self.assertEqual(res.outcomes[0].stage_reached, "rejected")
        self.assertFalse(res.outcomes[0].paper_routing_eligible)

    def test_real_chain_runs_no_errors(self):
        res = run_paper_loop([_sig("AAPL", "long")], _acct(),
                             evaluated_at_utc="2026-06-24T15:00:00+00:00")
        self.assertEqual(res.errors, [])         # chain runs end-to-end
        self.assertEqual(res.signals_in, 1)
        # M19 strict gates reject a minimal fixture; that is correct behaviour.
        self.assertEqual(res.opened_count, 0)


class M20IFullPipelineOpens(unittest.TestCase):
    """With an eligible candidate, the loop drives routing->sizing->order->
    fill->position->account-open and advances the account."""

    def setUp(self):
        self._orig = loop.score_candidate
        loop.score_candidate = lambda ci, cfg: _eligible_candidate(
            getattr(ci, "symbol", "AAPL"))

    def tearDown(self):
        loop.score_candidate = self._orig

    def test_eligible_signal_opens_position(self):
        acct = _acct(100000.0)
        res = run_paper_loop([_sig("AAPL", "long")], acct,
                             evaluated_at_utc="2026-06-24T15:00:00+00:00")
        self.assertEqual(res.errors, [])
        self.assertEqual(res.routed_count, 1)
        self.assertEqual(res.opened_count, 1)
        self.assertTrue(res.outcomes[0].opened)
        self.assertEqual(res.outcomes[0].stage_reached, "opened")
        # account advanced: cash reduced, one open position
        self.assertLess(res.account.available_paper_cash, 100000.0)
        self.assertEqual(len(res.account.open_positions), 1)

    def test_multiple_signals_accumulate(self):
        acct = _acct(100000.0)
        res = run_paper_loop([_sig("AAPL"), _sig("MSFT", entry=300.0,
                                                 stop=294.0, target=312.0)],
                             acct, evaluated_at_utc="2026-06-24T15:00:00+00:00")
        self.assertEqual(res.opened_count, 2)
        self.assertEqual(len(res.account.open_positions), 2)


class M20ISafetyGuards(unittest.TestCase):
    """No live execution, no broker calls, no execution_intents, no log_intent."""

    def test_no_live_or_broker_imports(self):
        src = pathlib.Path(loop.__file__).read_text()
        # forbidden import statements (not bare words — the docstring
        # legitimately mentions log_intent / execution_intents when stating
        # what the module must NOT do).
        for forbidden in (
                "import requests", "import urllib", "import socket",
                "from bot.brokers", "import bot.brokers",
                "from bot.live", "import bot.live",
                "from bot.risk", "import bot.risk",
                "ib_insync", "import alpaca", "from alpaca"):
            self.assertNotIn(forbidden, src)
        # forbidden call/usage patterns (parenthesised call or assignment),
        # which would indicate actual live-execution behaviour.
        for forbidden in ("log_intent(", "execution_intents",
                          "execution_eligible=True", "execution_eligible =True",
                          "execution_eligible = True"):
            self.assertNotIn(forbidden, src)

    def test_account_is_not_live(self):
        acct = _acct()
        self.assertFalse(getattr(acct, "IS_LIVE", True))

    def test_empty_signals_no_op(self):
        acct = _acct()
        res = run_paper_loop([], acct,
                             evaluated_at_utc="2026-06-24T15:00:00+00:00")
        self.assertEqual(res.signals_in, 0)
        self.assertEqual(res.opened_count, 0)
        self.assertEqual(res.account.available_paper_cash,
                         acct.available_paper_cash)

    def test_bad_signal_does_not_abort_batch(self):
        # a malformed signal is recorded as error/rejection but the batch
        # continues for the others.
        res = run_paper_loop([{"symbol": None}, _sig("AAPL", "short")],
                             _acct(), evaluated_at_utc="2026-06-24T15:00:00+00:00")
        self.assertEqual(res.signals_in, 2)
        self.assertEqual(len(res.outcomes), 2)


if __name__ == "__main__":
    unittest.main()
