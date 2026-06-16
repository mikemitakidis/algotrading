"""test_group_d_scanner.py — proofs for pre-M19 Group D scanner fixes.

ISSUE-009 — partial-data threshold: no silent auto-relaxation; hard-block by
            default; min_valid always == confluence.min_valid_tfs.
ISSUE-010 — WATCH route is logged-only and never appended to the actionable
            signals list (so it can never reach insert_signal / OrderIntent /
            risk / broker).

All tests drive bot.scanner.scan_cycle with conn=None (no flywheel, no
signals.db) and patch its data/feature/strategy/sentiment seams. No broker,
no live code, no real signals.db, no data/ml.
"""
import ast
import pathlib
import unittest
from unittest import mock

import bot.scanner as scanner
from bot.feature_engine import FeatureSet

_REPO_ROOT = pathlib.Path(__file__).resolve().parent

# All four enabled timeframes, in scanner order.
_ALL_TFS = [
    ("1D",  "3mo", "1d", False),
    ("4H",  "1mo", "1h", True),
    ("1H",  "5d",  "1h", False),
    ("15m", "5d",  "15m", False),
]


def _decision_ind():
    """Minimal indicator dict with the keys scan_cycle reads."""
    return {
        "price": 100.0, "atr": 2.0, "rsi": 55.0, "macd_hist": 0.1,
        "bb_pos": 0.5, "vwap_dev": 0.0, "vol_ratio": 1.2,
    }


def _fake_featureset(*_a, **_k):
    return FeatureSet(decision=_decision_ind(), ml={"ml_dummy": 1.0})


class _NeutralSentiment:
    name = "test"

    def get_sentiment(self, sym):
        from bot.sentiment.base import SentimentResult
        return SentimentResult.unavailable(self.name, "test")


def _passthrough_sentiment(signal, sent_result, mode):
    """apply_sentiment stand-in: never blocks."""
    return signal, True


class _ScanHarness:
    """Context manager that patches scan_cycle's seams.

    available_labels: which TF labels return data (the rest are 'no data').
    long_pass_labels: which TF labels score 1 for the 'long' direction.
    confluence:       dict merged into the strategy 'confluence' block.
    routing:          dict merged into the strategy 'routing' block.
    """

    def __init__(self, available_labels, long_pass_labels,
                 confluence=None, routing=None, enabled_tfs=None):
        self.available = set(available_labels)
        self.long_pass = set(long_pass_labels)
        self.confluence = confluence or {}
        self.routing = routing or {}
        self.enabled = enabled_tfs or _ALL_TFS

    def __enter__(self):
        strategy = {
            "version": 1,
            "confluence": {"min_valid_tfs": 3, **self.confluence},
            "routing": {"etoro_min_tfs": 4, "ibkr_min_tfs": 2, **self.routing},
            "risk": {"atr_stop_mult": 2.0, "atr_target_mult": 3.0},
        }

        def fake_fetch(focus, period, interval):
            # Map (period, interval) back to a label via self.enabled.
            label = None
            for lbl, p, iv, _rs in self.enabled:
                if p == period and iv == interval:
                    label = lbl
                    break
            if label is None or label not in self.available:
                return {}
            # Return one symbol with a dummy frame (compute_features patched).
            return {"AAPL": object()}

        def fake_score(ind, direction, strat):
            # Determine which label we're scoring by a counter trick: we can't
            # see the label here, so we approximate by passing through a
            # module-level current-label set in fake_compute. Simpler: score
            # 'long' as 1 only when the *current* label is in long_pass.
            return 1 if (direction == "long"
                         and scanner._CURRENT_TF_LABEL in self.long_pass) else 0

        # We need the current TF label visible to fake_score. Wrap the
        # per-TF loop by patching compute_features to stamp the label.
        outer = self

        def fake_compute(_df):
            return _fake_featureset()

        # Patch _build_timeframes to expose label so fake_fetch/score align.
        def fake_build(_strategy):
            return list(outer.enabled)

        self._patches = [
            mock.patch.object(scanner, "load_strategy", lambda: strategy),
            mock.patch.object(scanner, "_build_timeframes", fake_build),
            mock.patch.object(scanner, "compute_features", fake_compute),
            mock.patch.object(scanner, "resample_to_4h", lambda df: df),
            mock.patch.object(scanner, "score_timeframe", fake_score),
            mock.patch.object(scanner, "get_sentiment_mode", lambda: "off"),
            mock.patch.object(scanner, "get_sentiment_provider",
                              lambda: _NeutralSentiment()),
            mock.patch.object(scanner, "apply_sentiment", _passthrough_sentiment),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


# scan_cycle iterates timeframes and, per TF, calls score_timeframe. To let
# score depend on the current label, we stamp it on the module inside the loop
# by patching fetch_bars to set _CURRENT_TF_LABEL. Simpler: monkeypatch the
# loop variable indirectly via fetch_bars wrapper below.


def _run(harness_kwargs):
    """Run scan_cycle under the harness, returning (signals, meta).

    We override fetch_bars with a label-stamping version so score_timeframe
    can know which TF it is scoring.
    """
    h = _ScanHarness(**harness_kwargs)
    with h:
        # Replace fetch_bars with a stamping wrapper bound to the enabled set.
        def stamping_fetch(focus, period, interval):
            for lbl, p, iv, _rs in h.enabled:
                if p == period and iv == interval:
                    scanner._CURRENT_TF_LABEL = lbl
                    if lbl in h.available:
                        return {"AAPL": object()}
                    return {}
            return {}
        with mock.patch.object(scanner, "fetch_bars", stamping_fetch):
            return scanner.scan_cycle(["AAPL"], {}, conn=None, cycle_id=0)


class Issue009PartialData(unittest.TestCase):

    def test_all4_available_3of4_fires(self):
        # 3 of 4 TFs agree long, all 4 available, cfg_min=3 -> fires.
        sig, meta = _run(dict(
            available_labels=["1D", "4H", "1H", "15m"],
            long_pass_labels=["1D", "4H", "1H"]))
        longs = [s for s in sig if s["direction"] == "long"]
        self.assertEqual(len(longs), 1)
        self.assertEqual(longs[0]["valid_count"], 3)

    def test_all4_available_only_2_passing_no_fire(self):
        # all 4 available but only 2 agree, cfg_min=3 -> no signal.
        sig, meta = _run(dict(
            available_labels=["1D", "4H", "1H", "15m"],
            long_pass_labels=["1D", "4H"]))
        self.assertEqual(sig, [])

    def test_3_available_block_when_partial_disallowed(self):
        sig, meta = _run(dict(
            available_labels=["1D", "4H", "1H"],     # only 3 returned data
            long_pass_labels=["1D", "4H", "1H"]))
        self.assertEqual(sig, [])
        self.assertTrue(meta.get("blocked_partial_data"))
        self.assertEqual(meta.get("allow_partial_data"), False)

    def test_3_available_partial_allowed_min_valid_stays_cfg_min(self):
        # allow_partial=True: scan runs, but min_valid stays 3, so a 2-of-3
        # agreement must NOT fire (no relaxation).
        sig, meta = _run(dict(
            available_labels=["1D", "4H", "1H"],
            long_pass_labels=["1D", "4H"],            # only 2 agree
            confluence={"allow_partial_data": True}))
        self.assertEqual(sig, [], "2-of-3 must not fire; min_valid stays 3")
        # And a real 3-of-3 DOES fire under partial mode:
        sig2, _ = _run(dict(
            available_labels=["1D", "4H", "1H"],
            long_pass_labels=["1D", "4H", "1H"],
            confluence={"allow_partial_data": True}))
        self.assertEqual(len(sig2), 1)
        self.assertEqual(sig2[0]["valid_count"], 3)

    def test_1_available_block_when_partial_disallowed(self):
        sig, meta = _run(dict(
            available_labels=["1D"],
            long_pass_labels=["1D"]))
        self.assertEqual(sig, [])
        self.assertTrue(meta.get("blocked_partial_data"))

    def test_0_available_no_signals(self):
        sig, meta = _run(dict(
            available_labels=[],
            long_pass_labels=[]))
        self.assertEqual(sig, [])
        self.assertEqual(meta.get("tfs_available"), 0)


class Issue010WatchRoute(unittest.TestCase):

    def test_watch_scenario_returns_no_watch_signal(self):
        # Force WATCH: ibkr_min=4 so a 3-of-4 agreement (count=3 < 4) would be
        # WATCH. allow_partial irrelevant (all 4 available). count=3 >= min_valid
        # (cfg_min=3) so it passes the confluence bar, then routes WATCH.
        sig, meta = _run(dict(
            available_labels=["1D", "4H", "1H", "15m"],
            long_pass_labels=["1D", "4H", "1H"],      # count=3
            routing={"ibkr_min_tfs": 4, "etoro_min_tfs": 4}))
        self.assertEqual(
            [s for s in sig if s.get("route") == "WATCH"], [],
            "WATCH-routed signals must not be returned")

    def test_no_returned_signal_is_watch(self):
        sig, _ = _run(dict(
            available_labels=["1D", "4H", "1H", "15m"],
            long_pass_labels=["1D", "4H", "1H"],
            routing={"ibkr_min_tfs": 4, "etoro_min_tfs": 4}))
        self.assertTrue(all(s.get("route") != "WATCH" for s in sig))
        # In this forced-WATCH config, nothing actionable should be returned.
        self.assertEqual(sig, [])

    def test_scanner_has_explicit_watch_continue_branch(self):
        """Static: scanner must drop WATCH before appending to signals."""
        src = (_REPO_ROOT / "bot" / "scanner.py").read_text()
        tree = ast.parse(src)
        # Find scan_cycle and confirm a `route = 'WATCH'` assignment is
        # followed (within the same else-branch) by a `continue`.
        self.assertIn("route = 'WATCH'", src)
        # crude but effective: the WATCH else-branch must contain a continue
        idx = src.index("route = 'WATCH'")
        after = src[idx:idx + 1200]
        self.assertIn("continue", after,
                      "WATCH branch must `continue` before appending signal")

    def test_main_builds_orderintent_only_from_returned_signals(self):
        """Static: main.py builds OrderIntent inside the loop over scanner's
        returned `signals`, so a signal the scanner never returns (WATCH)
        cannot reach broker execution."""
        src = (_REPO_ROOT / "main.py").read_text()
        self.assertIn("for signal in signals:", src)
        self.assertIn("OrderIntent", src)
        # OrderIntent construction must occur after iterating `signals`.
        self.assertLess(src.index("for signal in signals:"),
                        src.index("OrderIntent("))


if __name__ == "__main__":
    unittest.main()
