"""
M15.2 — Health endpoint + heartbeat tests.

NEVER touches the real data/signals.db.
- All DB tests use tempfile fixtures.
- All heartbeat tests use temp directories.
- Endpoint tests use Flask test_client + monkeypatched paths.

Run: python3 test_m15_2_health.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ============================================================
# Heartbeat module
# ============================================================
class TestHeartbeatModule(unittest.TestCase):
    """Test bot/heartbeat.py in isolation, never touching real paths."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix='m15_2_hb_'))
        self.hb_path = self.tmpdir / 'heartbeat.json'
        # Empty DB file so the readable-DB check passes
        self.db_path = self.tmpdir / 'signals.db'
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("CREATE TABLE x (a INTEGER)")
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make(self, interval_sec=1):
        from bot.heartbeat import Heartbeat
        return Heartbeat(
            scan_interval_sec=900,
            heartbeat_path=self.hb_path,
            signals_db_path=self.db_path,
            interval_sec=interval_sec,
        )

    def test_start_writes_immediate_first_heartbeat(self):
        hb = self._make(interval_sec=60)
        hb.start()
        try:
            self.assertTrue(self.hb_path.exists(),
                            'first heartbeat must be written at start()')
            data = json.loads(self.hb_path.read_text())
            self.assertIn('last_heartbeat_ts', data)
            self.assertEqual(data['scan_interval_sec'], 900)
            self.assertTrue(data['db_writable'])
        finally:
            hb.stop(timeout=2)

    def test_heartbeat_updates_independently_of_scan(self):
        hb = self._make(interval_sec=1)  # fast tick
        hb.start()
        try:
            time.sleep(0.2)
            first = json.loads(self.hb_path.read_text())['last_heartbeat_ts']
            time.sleep(1.5)
            second = json.loads(self.hb_path.read_text())['last_heartbeat_ts']
            self.assertNotEqual(first, second,
                                'heartbeat must update independently of scan')
        finally:
            hb.stop(timeout=2)

    def test_record_scan_started_completed_distinct(self):
        hb = self._make(interval_sec=60)
        hb.start()
        try:
            hb.record_scan_started()
            s1 = json.loads(self.hb_path.read_text())
            self.assertIsNotNone(s1['last_scan_started_ts'])
            self.assertIsNone(s1['last_scan_completed_ts'])
            time.sleep(0.05)
            hb.record_scan_completed()
            s2 = json.loads(self.hb_path.read_text())
            self.assertIsNotNone(s2['last_scan_completed_ts'])
            self.assertNotEqual(s2['last_scan_started_ts'],
                                s2['last_scan_completed_ts'])
        finally:
            hb.stop(timeout=2)

    def test_atomic_write_no_partial_reads(self):
        """100 concurrent reads while heartbeat writes — never see partial JSON."""
        hb = self._make(interval_sec=1)
        hb.start()
        try:
            time.sleep(0.1)  # ensure first heartbeat written
            errors = []
            stop = threading.Event()

            def reader():
                for _ in range(200):
                    if stop.is_set():
                        return
                    try:
                        with open(self.hb_path, 'r') as f:
                            d = json.load(f)
                        if 'last_heartbeat_ts' not in d:
                            errors.append('missing key')
                    except json.JSONDecodeError as e:
                        errors.append(f'partial JSON: {e}')
                    except OSError:
                        pass  # tempfile rename race; benign
                    time.sleep(0.005)

            threads = [threading.Thread(target=reader) for _ in range(4)]
            for t in threads:
                t.start()
            # Force more writes during reads
            for _ in range(5):
                hb.record_scan_started()
                time.sleep(0.05)
                hb.record_scan_completed()
                time.sleep(0.05)
            stop.set()
            for t in threads:
                t.join(timeout=2)
            self.assertEqual(errors, [], f'atomic write violated: {errors[:5]}')
        finally:
            hb.stop(timeout=2)

    def test_heartbeat_thread_crash_resilient(self):
        """An exception in one tick must not kill the loop."""
        hb = self._make(interval_sec=1)
        hb.start()
        try:
            time.sleep(0.1)
            self.assertTrue(self.hb_path.exists())
            first_mtime = self.hb_path.stat().st_mtime

            # Inject a tick failure
            with patch('bot.heartbeat._atomic_write_json',
                       side_effect=OSError('synthetic')) as broken:
                time.sleep(1.2)
            # Wrapper released — next tick should succeed
            time.sleep(1.2)
            self.assertTrue(self.hb_path.exists())
            self.assertGreater(self.hb_path.stat().st_mtime, first_mtime,
                               'heartbeat thread must continue after exception')
            self.assertTrue(broken.called)
        finally:
            hb.stop(timeout=2)

    def test_db_writable_false_when_data_dir_not_writable(self):
        from bot.heartbeat import _probe_db_writable
        # Real data dir + DB → writable
        self.assertTrue(_probe_db_writable(self.db_path, self.tmpdir))
        # Nonexistent dir → writable=False
        nonexistent = self.tmpdir / 'subdir-that-mkdir-cant-make'
        # mkdir(parents=True, exist_ok=True) succeeds, so use a true read-only
        # parent: try a path under /proc which is sysfs-only
        ro_path = Path('/proc/1/fakedir/heartbeat.json')
        self.assertFalse(_probe_db_writable(self.db_path, ro_path.parent))

    def test_db_readable_check_uses_readonly_uri(self):
        """Confirm DB check opens read-only — no write lock acquired."""
        from bot.heartbeat import _probe_db_writable
        # Start a writer transaction on the DB
        writer = sqlite3.connect(str(self.db_path))
        writer.execute('BEGIN IMMEDIATE')
        try:
            # A naive BEGIN IMMEDIATE check from heartbeat would deadlock.
            # Our read-only probe should succeed despite the writer lock.
            start = time.monotonic()
            ok = _probe_db_writable(self.db_path, self.tmpdir)
            elapsed = time.monotonic() - start
            self.assertTrue(ok, 'read-only DB check must succeed under writer lock')
            self.assertLess(elapsed, 1.0,
                            f'read-only check must not block on writer lock '
                            f'(took {elapsed:.2f}s)')
        finally:
            writer.rollback()
            writer.close()

    def test_heartbeat_includes_gateway_summary(self):
        """Heartbeat payload must include a gateway summary read from
        signals.db read-only. This is what /api/health consumes."""
        # Build a full flywheel DB and write a gateway_state row
        import bot.flywheel as fw
        conn = sqlite3.connect(str(self.db_path))
        fw.init_flywheel_tables(conn)
        conn.close()
        fw.write_gateway_state(
            {'state': 'api_up_healthy', 'tcp_ok': True, 'api_ok': True,
             'probe_age_seconds': 12, 'broker_mode': 'paper'},
            db_path=str(self.db_path),
        )
        hb = self._make(interval_sec=60)
        hb.start()
        try:
            data = json.loads(self.hb_path.read_text())
            self.assertIn('gateway', data)
            gw = data['gateway']
            self.assertEqual(gw.get('state'), 'api_up_healthy')
            self.assertEqual(gw.get('tcp_ok'), True)
            self.assertEqual(gw.get('probe_age_seconds'), 12)
        finally:
            hb.stop(timeout=2)

    def test_gateway_summary_read_uses_readonly(self):
        """The heartbeat thread must read gateway_state read-only —
        no write lock contention with the trading scan loop."""
        from bot.heartbeat import _read_gateway_summary
        import bot.flywheel as fw
        conn = sqlite3.connect(str(self.db_path))
        fw.init_flywheel_tables(conn)
        conn.close()
        fw.write_gateway_state({'state': 'api_up_healthy'}, db_path=str(self.db_path))
        # Hold a writer transaction
        writer = sqlite3.connect(str(self.db_path))
        writer.execute('BEGIN IMMEDIATE')
        try:
            start = time.monotonic()
            summary = _read_gateway_summary(self.db_path)
            elapsed = time.monotonic() - start
            self.assertEqual(summary.get('state'), 'api_up_healthy')
            self.assertLess(elapsed, 1.0,
                            'gateway summary read must not block on writer lock')
        finally:
            writer.rollback()
            writer.close()

    def test_gateway_summary_returns_empty_when_db_missing(self):
        """Missing or unreadable signals.db must NOT raise — heartbeat
        keeps ticking with an empty gateway summary."""
        from bot.heartbeat import _read_gateway_summary
        missing = self.tmpdir / 'nonexistent.db'
        self.assertEqual(_read_gateway_summary(missing), {})


# ============================================================
# /api/health endpoint
# ============================================================
class TestHealthEndpoint(unittest.TestCase):
    """Use Flask test_client + monkeypatched heartbeat path + temp DB."""

    @classmethod
    def setUpClass(cls):
        # Set env before importing dashboard.app
        cls.tmpdir = Path(tempfile.mkdtemp(prefix='m15_2_dash_'))
        cls.hb_path = cls.tmpdir / 'heartbeat.json'
        cls.db_path = cls.tmpdir / 'signals.db'
        # Empty signals.db (no execution_intents needed for health endpoint)
        sqlite3.connect(str(cls.db_path)).close()
        os.environ['HEARTBEAT_FILE_PATH'] = str(cls.hb_path)
        os.environ['SIGNALS_DB_PATH']     = str(cls.db_path)
        os.environ['HEARTBEAT_STALE_SEC'] = '90'
        os.environ['SCAN_STALE_MULTIPLIER'] = '3'
        # Lazy-import dashboard so it picks up env
        import importlib
        if 'dashboard.app' in sys.modules:
            importlib.reload(sys.modules['dashboard.app'])
        from dashboard import app as dash_app_mod
        cls.dash_mod = dash_app_mod
        cls.app = dash_app_mod.app
        cls.app.config['TESTING'] = True
        # Force DB_PATH to our temp file so /api/health reads gateway_state
        # from the right DB (it uses module-level DB_PATH var)
        cls.dash_mod.DB_PATH = cls.db_path

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)
        for k in ('HEARTBEAT_FILE_PATH', 'SIGNALS_DB_PATH',
                  'HEARTBEAT_STALE_SEC', 'SCAN_STALE_MULTIPLIER',
                  'HEALTH_ENDPOINT_AUTH_TOKEN'):
            os.environ.pop(k, None)

    def setUp(self):
        # Clear any auth token env between tests
        os.environ.pop('HEALTH_ENDPOINT_AUTH_TOKEN', None)
        # Remove heartbeat file before each test
        if self.hb_path.exists():
            self.hb_path.unlink()
        # Clear gateway_state table if it exists (test isolation)
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("DELETE FROM gateway_state")
            conn.commit()
            conn.close()
        except sqlite3.OperationalError:
            pass  # table doesn't exist yet

    def _write_hb(self, **overrides):
        """Write a default-healthy heartbeat with optional overrides."""
        now = datetime.now(timezone.utc)
        payload = {
            'last_heartbeat_ts':       now.isoformat(timespec='seconds'),
            'last_scan_started_ts':    (now - timedelta(seconds=200)).isoformat(timespec='seconds'),
            'last_scan_completed_ts':  (now - timedelta(seconds=120)).isoformat(timespec='seconds'),
            'scan_interval_sec':       900,
            'db_writable':             True,
            'db_writable_checked_at':  now.isoformat(timespec='seconds'),
            'gateway':                 {'state': 'api_up_healthy'},
            'pid':                     12345,
            'process_started_at':      (now - timedelta(hours=1)).isoformat(timespec='seconds'),
            'heartbeat_interval_sec':  45,
        }
        payload.update(overrides)
        self.hb_path.write_text(json.dumps(payload))
        return payload

    def _get(self, headers=None):
        client = self.app.test_client()
        return client.get('/api/health', headers=headers or {})

    # ---- Status synthesis ----

    def test_heartbeat_missing_returns_critical_503(self):
        r = self._get()
        self.assertEqual(r.status_code, 503)
        body = r.get_json()
        self.assertEqual(body['status'], 'critical')
        self.assertEqual(body['reason_code'], 'heartbeat_missing')

    def test_heartbeat_stale_returns_critical_503(self):
        # Write a heartbeat with old mtime
        self._write_hb()
        old = time.time() - 300  # 5 minutes ago, > stale_sec=90
        os.utime(str(self.hb_path), (old, old))
        r = self._get()
        self.assertEqual(r.status_code, 503)
        body = r.get_json()
        self.assertEqual(body['status'], 'critical')
        self.assertEqual(body['reason_code'], 'heartbeat_stale')

    def test_db_unwritable_returns_critical_503(self):
        self._write_hb(db_writable=False)
        r = self._get()
        self.assertEqual(r.status_code, 503)
        body = r.get_json()
        self.assertEqual(body['status'], 'critical')
        self.assertEqual(body['reason_code'], 'db_unwritable')

    def test_scan_wedged_returns_critical_503(self):
        now = datetime.now(timezone.utc)
        # scan_started fresher than scan_completed AND started > 2*interval ago
        self._write_hb(
            last_scan_started_ts=(now - timedelta(seconds=2200)).isoformat(timespec='seconds'),
            last_scan_completed_ts=(now - timedelta(hours=2)).isoformat(timespec='seconds'),
            scan_interval_sec=900,
        )
        r = self._get()
        self.assertEqual(r.status_code, 503)
        body = r.get_json()
        self.assertEqual(body['reason_code'], 'scan_wedged')

    def test_scan_stale_returns_critical_503(self):
        now = datetime.now(timezone.utc)
        # scan_completed older than scan_interval * 3 (900 * 3 = 2700s)
        self._write_hb(
            last_scan_started_ts=(now - timedelta(seconds=3000)).isoformat(timespec='seconds'),
            last_scan_completed_ts=(now - timedelta(seconds=3000)).isoformat(timespec='seconds'),
            scan_interval_sec=900,
        )
        r = self._get()
        self.assertEqual(r.status_code, 503)
        body = r.get_json()
        self.assertEqual(body['reason_code'], 'scan_stale')

    def test_gateway_degraded_returns_degraded_200(self):
        # Gateway summary now lives in the heartbeat file, not signals.db.
        # /api/health reads gateway state from heartbeat ONLY.
        self._write_hb(gateway={'state': 'service_running_tcp_down',
                                'tcp_ok': False, 'api_ok': False})
        r = self._get()
        self.assertEqual(r.status_code, 200,
                         'degraded must NOT page external monitors')
        body = r.get_json()
        self.assertEqual(body['status'], 'degraded')
        self.assertEqual(body['reason_code'], 'gateway_degraded')
        self.assertEqual(body['gateway_state'], 'service_running_tcp_down')

    def test_healthy_returns_ok_200(self):
        self._write_hb(gateway={'state': 'api_up_healthy'})
        r = self._get()
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['status'], 'ok')
        self.assertIsNone(body['reason_code'])

    def test_gateway_unknown_does_not_flag_degraded(self):
        """First few seconds after boot the watchdog has state='unknown'.
        That must NOT be treated as degraded."""
        # gateway summary absent or state='unknown' → ok
        self._write_hb(gateway={})
        r = self._get()
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['status'], 'ok')

    # ---- Auth matrix ----

    def test_no_token_serves_minimal_payload(self):
        self._write_hb()
        r = self._get()
        body = r.get_json()
        # Minimal keys present
        for k in ('status', 'http_code', 'checked_at',
                  'heartbeat_age_sec', 'scan_age_sec',
                  'gateway_state', 'reason_code'):
            self.assertIn(k, body)
        # Full-payload keys absent
        for k in ('heartbeat', 'scan', 'db_writable', 'pid',
                  'process_started_at', 'warnings'):
            self.assertNotIn(k, body,
                             f'{k} must not appear in minimal payload')

    def test_token_set_no_header_returns_minimal(self):
        os.environ['HEALTH_ENDPOINT_AUTH_TOKEN'] = 'super-secret-token'
        self._write_hb()
        r = self._get()
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertNotIn('heartbeat', body)  # minimal payload

    def test_token_set_wrong_header_returns_401(self):
        os.environ['HEALTH_ENDPOINT_AUTH_TOKEN'] = 'super-secret-token'
        self._write_hb()
        r = self._get(headers={'Authorization': 'Bearer wrong-token'})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()['error'], 'unauthorized')

    def test_token_set_correct_header_returns_full_payload(self):
        os.environ['HEALTH_ENDPOINT_AUTH_TOKEN'] = 'super-secret-token'
        self._write_hb()
        r = self._get(headers={'Authorization': 'Bearer super-secret-token'})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        # Full-payload keys present
        for k in ('heartbeat', 'scan', 'db_writable', 'pid',
                  'process_started_at', 'warnings'):
            self.assertIn(k, body, f'full payload missing {k}')

    def test_constant_time_compare_used(self):
        """Source proof: dashboard/app.py uses hmac.compare_digest."""
        repo = Path(__file__).resolve().parent
        with open(repo / 'dashboard' / 'app.py') as f:
            src = f.read()
        self.assertIn('compare_digest', src,
                      'health endpoint must use hmac.compare_digest')

    def test_malformed_authorization_header_returns_401(self):
        os.environ['HEALTH_ENDPOINT_AUTH_TOKEN'] = 'super-secret-token'
        self._write_hb()
        # No "Bearer " prefix
        r = self._get(headers={'Authorization': 'super-secret-token'})
        self.assertEqual(r.status_code, 401)

    # ---- Security / hygiene ----

    def test_minimal_payload_has_no_secrets(self):
        self._write_hb()
        r = self._get()
        body_str = json.dumps(r.get_json())
        # PIDs are operational not secret, but minimal payload still
        # omits them by design.
        self.assertNotIn('pid', json.loads(body_str))
        # No raw account IDs (test fixture doesn't have one, but the
        # contract is: never include account IDs at all)
        self.assertNotIn('account', body_str.lower())

    def test_mtime_preferred_when_json_ts_tampered(self):
        """Tamper test: write a heartbeat with a fake-fresh ts inside JSON
        but old mtime. Endpoint must use mtime (trustworthy) and report stale."""
        now = datetime.now(timezone.utc)
        self._write_hb(last_heartbeat_ts=now.isoformat(timespec='seconds'))
        # Force mtime to 5 minutes ago (greater than 90s stale threshold)
        old = time.time() - 300
        os.utime(str(self.hb_path), (old, old))
        r = self._get()
        self.assertEqual(r.status_code, 503)
        body = r.get_json()
        self.assertEqual(body['reason_code'], 'heartbeat_stale')

    # ---- M15.2 review fix: endpoint must NOT touch signals.db ----

    def test_endpoint_does_not_open_signals_db(self):
        """Hard proof: /api/health must NEVER open signals.db, in any mode.

        Runs the endpoint with sqlite3.connect monkeypatched to record every
        call. Asserts no call ever names signals.db (whether via str path or
        file:...?mode=ro URI). This guards the dashboard process from any
        future lock contention with the trading scan loop.
        """
        import sqlite3 as _sqlite3
        opened = []
        real_connect = _sqlite3.connect

        def tracking_connect(target, *args, **kwargs):
            opened.append(str(target))
            return real_connect(target, *args, **kwargs)

        self._write_hb(gateway={'state': 'api_up_healthy'})
        with patch.object(_sqlite3, 'connect', side_effect=tracking_connect):
            # Hit every code path in the endpoint
            r1 = self._get()  # ok branch
            r2 = self._get(headers={'Authorization': 'Bearer x'})  # unauth path (no token configured)
            os.environ['HEALTH_ENDPOINT_AUTH_TOKEN'] = 'secret'
            r3 = self._get(headers={'Authorization': 'Bearer secret'})  # full payload
            r4 = self._get(headers={'Authorization': 'Bearer wrong'})  # 401
            os.environ.pop('HEALTH_ENDPOINT_AUTH_TOKEN', None)

        # None of those /api/health requests must have opened signals.db
        offenders = [t for t in opened if 'signals.db' in t]
        self.assertEqual(
            offenders, [],
            f'/api/health opened signals.db: {offenders}'
        )

    def test_endpoint_does_not_import_read_gateway_state(self):
        """Source proof: api_health() function body does not reference
        read_gateway_state. Belt-and-braces against future regressions."""
        import ast
        repo = Path(__file__).resolve().parent
        with open(repo / 'dashboard' / 'app.py') as f:
            tree = ast.parse(f.read())
        api_health_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == 'api_health':
                api_health_fn = node
                break
        self.assertIsNotNone(api_health_fn, 'api_health function not found')
        # Collect every Name/Attribute referenced inside api_health
        names = set()
        for child in ast.walk(api_health_fn):
            if isinstance(child, ast.Name):
                names.add(child.id)
            elif isinstance(child, ast.Attribute):
                names.add(child.attr)
            elif isinstance(child, ast.ImportFrom) and child.module:
                for alias in child.names:
                    names.add(alias.name)
        self.assertNotIn(
            'read_gateway_state', names,
            'api_health() must not reference read_gateway_state — gateway '
            'summary comes from heartbeat.json'
        )


if __name__ == '__main__':
    unittest.main(verbosity=2)
