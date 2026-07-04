#!/usr/bin/env python3
"""M21.1extra-D2.0 — CycleLock contract tests (atomic; temp paths only)."""
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

    def _write_lock(self, pid):
        with open(self.path, "w") as fh:
            json.dump({"pid": pid, "started_at_utc": "2026-07-06T00:00:00+00:00"},
                      fh)

    def test_atomic_acquire_writes_pid_and_started(self):
        CycleLock(self.path, pid=1111, pid_alive=lambda p: True).acquire()
        data = json.load(open(self.path))
        self.assertEqual(data["pid"], 1111)
        self.assertIn("started_at_utc", data)

    def test_live_lock_same_pid_refuses(self):
        # Blocker 1: a live lock held by the SAME pid must refuse.
        self._write_lock(1111)
        with self.assertRaises(LockHeld):
            CycleLock(self.path, pid=1111, pid_alive=lambda p: True).acquire()

    def test_live_lock_different_pid_refuses(self):
        self._write_lock(2222)
        with self.assertRaises(LockHeld):
            CycleLock(self.path, pid=3333, pid_alive=lambda p: True).acquire()

    def test_stale_dead_pid_reclaimed(self):
        self._write_lock(1111)  # 1111 dead
        CycleLock(self.path, pid=4444,
                  pid_alive=lambda p: p == 4444).acquire()
        self.assertEqual(json.load(open(self.path))["pid"], 4444)

    def test_stale_same_pid_reclaimed_when_dead(self):
        # a lock file naming our pid but our pid reported dead -> reclaim.
        self._write_lock(5555)
        CycleLock(self.path, pid=5555,
                  pid_alive=lambda p: False).acquire()
        self.assertEqual(json.load(open(self.path))["pid"], 5555)

    def test_corrupt_lock_reclaimed(self):
        with open(self.path, "w") as fh:
            fh.write("not json {{{")
        # corrupt -> owner pid -1 -> realistic pid_alive(-1)=False -> reclaim
        CycleLock(self.path, pid=6666).acquire()
        self.assertEqual(json.load(open(self.path))["pid"], 6666)

    def test_file_exists_collision_then_reclaim_retries(self):
        # simulate an atomic-create collision on a STALE file: O_EXCL fails,
        # the code reads it, sees a dead pid, removes it, and retries create.
        self._write_lock(9999)  # dead
        lock = CycleLock(self.path, pid=1234, pid_alive=lambda p: p == 1234)
        lock.acquire()  # must succeed via reclaim-then-retry
        self.assertEqual(json.load(open(self.path))["pid"], 1234)

    def test_released_on_normal_completion(self):
        with CycleLock(self.path, pid=4444, pid_alive=lambda p: True):
            self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_released_on_exception(self):
        with self.assertRaises(RuntimeError):
            with CycleLock(self.path, pid=5555, pid_alive=lambda p: True):
                raise RuntimeError("boom")
        self.assertFalse(os.path.exists(self.path))

    def test_exception_not_suppressed(self):
        class Marker(Exception):
            pass
        with self.assertRaises(Marker):
            with CycleLock(self.path, pid=5556, pid_alive=lambda p: True):
                raise Marker()

    def test_release_only_removes_own_acquired_lock(self):
        # a foreign live lock must not be removed by our release().
        self._write_lock(1111)
        mine = CycleLock(self.path, pid=2222)  # never acquired
        mine.release()
        self.assertTrue(os.path.exists(self.path))

    def test_release_noop_when_not_acquired(self):
        lock = CycleLock(self.path, pid=1)
        lock.release()  # never acquired -> no error, nothing removed
        self.assertFalse(os.path.exists(self.path))

    def test_path_is_injected_not_vps(self):
        lock = CycleLock(self.path, pid=1)
        self.assertNotIn("/opt/algo-trader", lock.path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
