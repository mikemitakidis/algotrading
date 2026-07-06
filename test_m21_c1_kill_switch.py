#!/usr/bin/env python3
"""M21 safety-test coverage for bot/kill_switch.py (C1 + D1).

History:
  - C1 originally added kill-switch CI coverage, closing audit R1-4
    (kill_switch fail-safe had zero CI coverage).
  - D1 closes audit R2-1 by changing the missing-file read path to fail
    CLOSED. A missing kill_switch.json and a corrupt/unreadable one now BOTH
    read ACTIVE — the earlier fail-open asymmetry is removed. This file's
    expectations were updated accordingly (see TestMissingFileFailsClosed).

ensure_default_state() remains the deliberate initializer for the
inactive/default state: it explicitly writes {'active': False} at startup, and
is the ONLY intended way to obtain an inactive state (see TestEnsureDefaultState).

Isolation: every test monkeypatches bot.kill_switch.KILL_SWITCH_PATH to a temp
file, so the PRODUCTION data/kill_switch.json is never read or written. A guard
test asserts the production path is untouched.

These tests are red/green guardrails: if the read-path fail-closed behaviour
(missing OR unreadable -> ACTIVE) is removed, the corresponding tests must fail.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import bot.kill_switch as ks


class _IsolatedKillSwitch(unittest.TestCase):
    """Redirect KILL_SWITCH_PATH to a temp file for the duration of each test.
    The production kill_switch.json is never touched."""

    def setUp(self):
        self._saved_path = ks.KILL_SWITCH_PATH
        self._tmpdir = tempfile.mkdtemp()
        ks.KILL_SWITCH_PATH = Path(self._tmpdir) / "kill_switch.json"

    def tearDown(self):
        ks.KILL_SWITCH_PATH = self._saved_path
        # best-effort temp cleanup
        try:
            for p in Path(self._tmpdir).glob("*"):
                p.unlink()
            os.rmdir(self._tmpdir)
        except OSError:
            pass

    def _write_raw(self, text: str):
        ks.KILL_SWITCH_PATH.parent.mkdir(parents=True, exist_ok=True)
        ks.KILL_SWITCH_PATH.write_text(text)


class TestActivateDeactivate(_IsolatedKillSwitch):
    def test_activate_makes_active(self):
        state = ks.activate_kill_switch("test activate")
        self.assertTrue(state["active"])
        self.assertTrue(ks.is_kill_switch_active())

    def test_deactivate_makes_inactive(self):
        ks.activate_kill_switch("on")
        state = ks.deactivate_kill_switch("off")
        self.assertFalse(state["active"])
        self.assertFalse(ks.is_kill_switch_active())

    def test_state_persists_across_reads(self):
        ks.activate_kill_switch("persist")
        # a fresh read (new _read_state call) still sees active.
        self.assertTrue(ks.is_kill_switch_active())
        full = ks.get_kill_switch_state()
        self.assertTrue(full["active"])
        self.assertEqual(full["reason"], "persist")


class TestFailSafe(_IsolatedKillSwitch):
    def test_unreadable_state_fails_safe_active(self):
        # corrupt JSON -> json.loads raises -> _read_state returns active=True.
        # THIS IS THE CRITICAL FAIL-SAFE. If runtime changed to fail-open on a
        # corrupt file, this test must fail.
        self._write_raw("{ this is not valid json ")
        self.assertTrue(ks.is_kill_switch_active(),
                        "corrupt/unreadable state MUST fail safe to ACTIVE")

    def test_empty_file_fails_safe_active(self):
        # empty file -> json.loads("") raises -> active.
        self._write_raw("")
        self.assertTrue(ks.is_kill_switch_active())

    def test_missing_active_key_treated_active(self):
        # a JSON object lacking 'active' -> .get('active', True) -> active.
        self._write_raw(json.dumps({"reason": "no active key"}))
        self.assertTrue(ks.is_kill_switch_active())

    def test_explicit_true_is_active(self):
        self._write_raw(json.dumps({"active": True}))
        self.assertTrue(ks.is_kill_switch_active())

    def test_explicit_false_is_inactive(self):
        self._write_raw(json.dumps({"active": False}))
        self.assertFalse(ks.is_kill_switch_active())


class TestMissingFileFailsClosed(_IsolatedKillSwitch):
    """R2-1 FIXED (D1): a missing state file on the read path now fails CLOSED
    (ACTIVE), consistent with the corrupt-file case. The only way to get an
    inactive/default state is ensure_default_state(), tested separately."""

    def test_cleanly_missing_file_is_active_failclosed(self):
        # no file at all -> read path now returns ACTIVE (was inactive pre-D1).
        if ks.KILL_SWITCH_PATH.exists():
            ks.KILL_SWITCH_PATH.unlink()
        self.assertTrue(
            ks.is_kill_switch_active(),
            "D1: a missing state file must fail CLOSED (ACTIVE)")

    def test_missing_and_corrupt_both_fail_closed(self):
        # both the missing and corrupt read paths now agree: ACTIVE.
        if ks.KILL_SWITCH_PATH.exists():
            ks.KILL_SWITCH_PATH.unlink()
        missing_active = ks.is_kill_switch_active()
        self._write_raw("{corrupt")
        corrupt_active = ks.is_kill_switch_active()
        self.assertTrue(missing_active)   # fail-closed (changed by D1)
        self.assertTrue(corrupt_active)   # fail-safe (unchanged)
        self.assertEqual(
            missing_active, corrupt_active,
            "D1: missing and corrupt now both fail closed (asymmetry removed)")


class TestEnsureDefaultState(_IsolatedKillSwitch):
    """ensure_default_state() remains the ONLY intended initializer of an
    inactive/default state, and it does so explicitly."""

    def test_ensure_default_creates_inactive_when_missing(self):
        if ks.KILL_SWITCH_PATH.exists():
            ks.KILL_SWITCH_PATH.unlink()
        # before init: read path fails closed (active).
        self.assertTrue(ks.is_kill_switch_active())
        # deliberate initializer writes an inactive default.
        ks.ensure_default_state()
        self.assertTrue(ks.KILL_SWITCH_PATH.exists())
        self.assertFalse(ks.is_kill_switch_active(),
                         "ensure_default_state must create an inactive default")

    def test_ensure_default_preserves_existing_active_state(self):
        # if the file already exists (active), ensure_default_state must NOT
        # overwrite it back to inactive.
        ks.activate_kill_switch("pre-existing active")
        ks.ensure_default_state()
        self.assertTrue(ks.is_kill_switch_active(),
                        "ensure_default_state must not clobber an active state")


class TestProductionPathUntouched(_IsolatedKillSwitch):
    def test_isolation_uses_tempdir_not_production(self):
        # our redirected path must be under the temp dir, not data/.
        self.assertIn(self._tmpdir, str(ks.KILL_SWITCH_PATH))
        self.assertNotIn("data/kill_switch.json", str(ks.KILL_SWITCH_PATH))

    def test_writes_land_in_tempdir_only(self):
        ks.activate_kill_switch("temp only")
        # the temp file exists; the saved production path was NOT written by us.
        self.assertTrue(ks.KILL_SWITCH_PATH.exists())
        self.assertIn(self._tmpdir, str(ks.KILL_SWITCH_PATH))


if __name__ == "__main__":
    unittest.main(verbosity=2)
