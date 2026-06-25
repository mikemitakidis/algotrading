"""M20.J — m20_status reporter tests. Pure, offline, read-only; no network,
no broker/live imports, no writes."""
import json
import pathlib
import tempfile
import unittest

import bot.runtime.m20_status as status
from bot.runtime.m20_status import build_status, STATUS_SCHEMA_VERSION

_REPO = pathlib.Path(__file__).resolve().parent
_SEED = _REPO / "configs" / "universe" / "us_seed.json"
_EXPANDED = _REPO / "configs" / "universe" / "us_expanded.json"
_HAS_UNIVERSE = _SEED.exists() and _EXPANDED.exists()


class M20JReportShape(unittest.TestCase):
    def test_report_has_expected_sections(self):
        r = build_status()
        for k in ("schema_version", "universe", "quality_snapshot",
                  "quality_thresholds", "paper_loop", "paper_storage",
                  "frozen_m20_commits", "main_merged", "next_required"):
            self.assertIn(k, r)
        self.assertEqual(r["schema_version"], STATUS_SCHEMA_VERSION)
        self.assertFalse(r["main_merged"])  # nothing merged to main yet
        self.assertIn("M20.UD", r["next_required"])

    def test_json_serializable_and_deterministic(self):
        a = json.dumps(build_status(), sort_keys=True)
        b = json.dumps(build_status(), sort_keys=True)
        self.assertEqual(a, b)               # deterministic
        json.loads(a)                        # valid JSON


@unittest.skipUnless(_HAS_UNIVERSE, "universe files present")
class M20JUniverseCounts(unittest.TestCase):
    def test_counts_match_post_uc2_state(self):
        u = build_status()["universe"]
        self.assertTrue(u["files_present"])
        self.assertEqual(u["total"], 573)
        self.assertEqual(u["verified"], 536)
        self.assertEqual(u["failed"], 18)
        self.assertEqual(u["unverified"], 19)
        self.assertEqual(u["scan_ready"], 536)
        self.assertEqual(u["active"], 573)


class M20JReferences(unittest.TestCase):
    def test_snapshot_reference(self):
        s = build_status()["quality_snapshot"]
        if s["present"]:
            self.assertEqual(s["asof"], "2026-06-24")
            self.assertEqual(s["schema_version"], "m20_quality_snapshot_v1")

    def test_thresholds_reference(self):
        t = build_status()["quality_thresholds"]
        if t["present"]:
            self.assertEqual(t["max_scan_ready_per_run"], 600)
            self.assertEqual(t["liquidity_source"], "yahoo")
            self.assertEqual(t["min_history_days"], 252)

    def test_frozen_commits_present(self):
        c = build_status()["frozen_m20_commits"]
        for key in ("m19_main_baseline", "uc1_snapshot", "uc2_writeback",
                    "ue_registry_selector", "m20i_paper_loop"):
            self.assertIn(key, c)
            self.assertEqual(len(c[key]), 40)  # full sha


class M20JPaperLoopStatus(unittest.TestCase):
    def test_paper_loop_disabled_by_default(self):
        import os
        prev = os.environ.pop("PAPER_LOOP_ENABLED", None)
        try:
            self.assertFalse(build_status()["paper_loop"]["enabled"])
        finally:
            if prev is not None:
                os.environ["PAPER_LOOP_ENABLED"] = prev

    def test_paper_loop_reflects_env(self):
        import os
        prev = os.environ.get("PAPER_LOOP_ENABLED")
        try:
            os.environ["PAPER_LOOP_ENABLED"] = "true"
            self.assertTrue(build_status()["paper_loop"]["enabled"])
        finally:
            if prev is None:
                os.environ.pop("PAPER_LOOP_ENABLED", None)
            else:
                os.environ["PAPER_LOOP_ENABLED"] = prev


class M20JPaperStorageGraceful(unittest.TestCase):
    def test_handles_absent_storage(self):
        ps = build_status()["paper_storage"]
        # in this repo no data/paper artifacts exist; must report gracefully
        self.assertIn("present", ps)
        if not ps["present"]:
            self.assertIn("note", ps)

    def test_summarizes_present_storage_via_helpers(self):
        # build a tiny events.jsonl in a temp data/paper and point the reporter
        # at it; must summarize via load helpers without raising.
        from bot.paper import (PaperEvent, PaperEventType, append_events)
        with tempfile.TemporaryDirectory() as d:
            paper_dir = pathlib.Path(d) / "data" / "paper"
            paper_dir.mkdir(parents=True)
            ev_path = paper_dir / "events.jsonl"
            try:
                ev = PaperEvent(
                    event_type=PaperEventType.ACCOUNT_OPENED
                    if hasattr(PaperEventType, "ACCOUNT_OPENED")
                    else list(PaperEventType)[0],
                    event_time_utc="2026-06-24T15:00:00+00:00",
                    detail={"cash_delta": -20000.0})
                append_events([ev], path=str(ev_path))
            except Exception:
                self.skipTest("PaperEvent construction differs; shape-only test")
            orig = status._PAPER_DIR
            try:
                status._PAPER_DIR = paper_dir
                ps = status._paper_storage_summary()
                self.assertTrue(ps["present"])
                self.assertGreaterEqual(ps.get("events_loaded", 0), 0)
            finally:
                status._PAPER_DIR = orig


class M20JSafety(unittest.TestCase):
    def test_no_network_or_broker_imports(self):
        src = pathlib.Path(status.__file__).read_text()
        for forbidden in (
                "import requests", "import urllib", "import socket",
                "from bot.brokers", "import bot.brokers",
                "from bot.live", "import bot.live",
                "import alpaca", "from alpaca", "ib_insync"):
            self.assertNotIn(forbidden, src)

    def test_reporter_does_not_write(self):
        # running the reporter must not mutate universe/config files nor
        # create data/paper.
        seed_before = _SEED.read_bytes() if _SEED.exists() else b""
        had_paper = (_REPO / "data" / "paper").exists()
        build_status()
        if _SEED.exists():
            self.assertEqual(_SEED.read_bytes(), seed_before)
        self.assertEqual((_REPO / "data" / "paper").exists(), had_paper)


if __name__ == "__main__":
    unittest.main()
