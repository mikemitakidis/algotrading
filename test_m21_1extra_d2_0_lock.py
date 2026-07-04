#!/usr/bin/env python3
"""M21.1extra-D2.0 — CycleLock contract tests (temp paths only; no VPS path)."""
from __future__ import annotations

import json
import os
import tempfile
import shutil
import unittest

from bot.paper_loop_d2 import CycleLock, LockHeld


class TestCycleLock(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "d2.lock")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_acquire_writes_pid_and_started(self):
        CycleLock(self.path, pid=1111).acquire()
        data = json.load(open(self.path))
        self.assertEqual(data["pid"], 1111)
        self.assertIn("started_at_utc", data)

    def test_live_lock_refuses(self):
        CycleLock(self.path, pid=1111, pid_alive=lambda p: True).acquire()
        other = CycleLock(self.path, pid=2222, pid_alive=lambda p: True)
        with self.assertRaises(LockHeld):
            other.acquire()

    def test_stale_lock_reclaimed(self):
        CycleLock(self.path, pid=1111, pid_alive=lambda p: False).acquire()
        # 1111 is dead -> 3333 reclaims
        CycleLock(self.path, pid=3333, pid_alive=lambda p: p == 3333).acquire()
        self.assertEqual(json.load(open(self.path))["pid"], 3333)

    def test_released_on_normal_completion(self):
        with CycleLock(self.path, pid=4444):
            self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_released_on_exception(self):
        with self.assertRaises(RuntimeError):
            with CycleLock(self.path, pid=5555):
                raise RuntimeError("boom")
        self.assertFalse(os.path.exists(self.path))

    def test_exception_not_suppressed(self):
        # __exit__ returns False -> the exception propagates.
        class Marker(Exception):
            pass
        with self.assertRaises(Marker):
            with CycleLock(self.path, pid=5556):
                raise Marker()

    def test_corrupt_lock_reclaimed(self):
        with open(self.path, "w") as fh:
            fh.write("not json {{{")
        CycleLock(self.path, pid=6666).acquire()
        self.assertEqual(json.load(open(self.path))["pid"], 6666)

    def test_release_only_removes_own_lock(self):
        # a lock owned by someone else must not be removed by our release().
        CycleLock(self.path, pid=1111, pid_alive=lambda p: True).acquire()
        mine = CycleLock(self.path, pid=2222)
        mine.release()  # we never acquired -> no-op
        self.assertTrue(os.path.exists(self.path))

    def test_reacquire_same_pid_ok(self):
        # the same pid re-acquiring (e.g. re-entrant run) is allowed.
        CycleLock(self.path, pid=1111, pid_alive=lambda p: True).acquire()
        CycleLock(self.path, pid=1111, pid_alive=lambda p: True).acquire()
        self.assertEqual(json.load(open(self.path))["pid"], 1111)

    def test_path_is_injected_not_vps(self):
        lock = CycleLock(self.path, pid=1)
        self.assertNotIn("/opt/algo-trader", lock.path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
