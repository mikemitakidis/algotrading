"""M21 A18 — gateway watchdog event-loop defect (linked to M21 B-6 / OP2).

Defect (VPS bot.log, reproduced offline 5 Nov 2026): the first ``import
ib_insync`` / ``IB()`` happened inside the 'gw-watchdog' thread, which has no
asyncio event loop. eventkit binds a loop at import time, so every probe raised
``RuntimeError: There is no current event loop in thread 'gw-watchdog'`` outside
api_probe's try-block; _tick() aborted before recording the probe.

These tests prove the fix without any real IBKR connection:
  * every network test talks only to 127.0.0.1 on an ephemeral port, to a local
    FAKE gateway (``_FakeGateway``) or to a closed port; IBKR ports are refused;
  * import-order-sensitive scenarios run in a fresh interpreter (subprocess),
    because the defect only appears when ib_insync was NOT imported first by
    the main thread.
"""
from __future__ import annotations

import ast
import asyncio
import os
import socket
import struct
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import bot.gateway_watchdog as gw  # noqa: E402

IBKR_PORTS = {4001, 4002, 7496, 7497}

try:
    import importlib.util as _ilu
    HAVE_IB = _ilu.find_spec("ib_insync") is not None
except Exception:  # pragma: no cover
    HAVE_IB = False


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert port not in IBKR_PORTS
    return port


# A minimal local stand-in for the IB API handshake: enough for ib_insync's
# connect(readonly=True) + reqCurrentTime(). Account name is a placeholder.
_FAKE_SERVER = r'''
import socket, struct, threading, time
def _frame(*fields):
    p = b"".join(str(f).encode() + b"\0" for f in fields)
    return struct.pack(">I", len(p)) + p
def _recv_exact(c, n):
    b = b""
    while len(b) < n:
        chunk = c.recv(n - len(b))
        if not chunk:
            raise EOFError
        b += chunk
    return b
class _FakeGateway:
    """Local fake: answers the handshake and the sync requests; mute=True
    accepts TCP but never answers (port open, API dead)."""
    def __init__(self, mute=False):
        self.mute = mute
        self.srv = socket.socket(); self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0)); self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        assert self.port not in (4001, 4002, 7496, 7497)
        self.connections = 0
        threading.Thread(target=self._serve, daemon=True).start()
    def _serve(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._client, args=(c,), daemon=True).start()
    def _client(self, c):
        try:
            if self.mute:
                time.sleep(30); return
            assert _recv_exact(c, 4) == b"API\0"
            n = struct.unpack(">I", _recv_exact(c, 4))[0]; _recv_exact(c, n)
            c.sendall(_frame(176, "20261105 12:00:00 UTC"))
            while True:
                n = struct.unpack(">I", _recv_exact(c, 4))[0]
                f = _recv_exact(c, n).split(b"\0")
                mid = int(f[0])
                if mid == 71:   c.sendall(_frame(9, 1, 1) + _frame(15, 1, "FAKEACCT"))
                elif mid == 61: c.sendall(_frame(62, 1))
                elif mid == 6:  c.sendall(_frame(54, 1, "FAKEACCT"))
                elif mid == 76: c.sendall(_frame(74, 1, int(f[2])))
                elif mid == 7:  c.sendall(_frame(55, 1, int(f[2])))
                elif mid == 49: c.sendall(_frame(49, 1, int(time.time())))
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass
'''


def _run_fresh(body: str, timeout: int = 90) -> subprocess.CompletedProcess:
    """Run ``body`` in a fresh interpreter with the repo on sys.path."""
    code = "import sys; sys.path.insert(0, %r)\n" % REPO + _FAKE_SERVER + "\n" + textwrap.dedent(body)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("IBKR", "BROKER"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=timeout, cwd=REPO, env=env)


class _FakeFlywheel:
    def __init__(self):
        self.states, self.events = [], []

    def write_gateway_state(self, state):
        self.states.append(dict(state))

    def write_gateway_event(self, **kw):
        self.events.append(kw)


def _cfg(port: int, failures_to_down: int = 2) -> "gw.WatchdogConfig":
    assert port not in IBKR_PORTS
    return gw.WatchdogConfig(enabled=True, mode="alert_only", interval_sec=60, api_timeout_sec=2,
                             failures_to_down=failures_to_down, host="127.0.0.1", port=port,
                             broker_mode="paper", systemd_unit="ibgateway")


# --------------------------------------------------------------------------
class A1_RootCauseAndNonMainThreadProbe(unittest.TestCase):
    """The probe works from a non-main thread (fresh interpreter: ib_insync
    has NOT been imported by the main thread)."""

    @unittest.skipUnless(HAVE_IB, "ib_insync not installed")
    def test_root_cause_is_reproduced_without_the_fix(self):
        # Documents the defect: a bare IB() in a fresh non-main thread raises.
        r = _run_fresh('''
            import threading
            out = {}
            def t():
                try:
                    from ib_insync import IB; IB(); out["r"] = "ok"
                except Exception as e:
                    out["r"] = "%s: %s" % (type(e).__name__, e)
            th = threading.Thread(target=t, name="gw-watchdog"); th.start(); th.join()
            print(out["r"])
        ''')
        self.assertIn("RuntimeError: There is no current event loop in thread 'gw-watchdog'", r.stdout, r.stderr)

    @unittest.skipUnless(HAVE_IB, "ib_insync not installed")
    def test_probe_from_non_main_thread_reaches_the_network(self):
        # Closed local port: the probe now fails for the TRUE reason (refused),
        # not with the event-loop RuntimeError, and never raises.
        port = _closed_port()
        r = _run_fresh('''
            import threading, bot.gateway_watchdog as gw
            out = {}
            def t():
                try:
                    out["r"] = gw.api_probe("127.0.0.1", %d, 99, 2)
                except BaseException as e:
                    out["r"] = ("RAISED", repr(e))
            th = threading.Thread(target=t, name="gw-watchdog"); th.start(); th.join()
            print(repr(out["r"]))
        ''' % port)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("RAISED", r.stdout)
        self.assertNotIn("no current event loop", r.stdout)
        self.assertIn("(False, None,", r.stdout)
        self.assertRegex(r.stdout, r"Connect|Refused|refused|OSError|Errno")

    @unittest.skipUnless(HAVE_IB, "ib_insync not installed")
    def test_probe_succeeds_from_non_main_thread_against_local_fake_gateway(self):
        r = _run_fresh('''
            import threading, bot.gateway_watchdog as gw
            fake = _FakeGateway()
            out = {}
            def t():
                out["r"] = gw.api_probe("127.0.0.1", fake.port, 99, 5)
            th = threading.Thread(target=t, name="gw-watchdog"); th.start(); th.join(60)
            ok, lat, err = out["r"]
            print("ok=%s err=%s conns=%d" % (ok, err, fake.connections))
        ''')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ok=True err=None conns=1", r.stdout, r.stdout + r.stderr)


# --------------------------------------------------------------------------
class A2_WatchdogRecordsTruthfulState(unittest.TestCase):
    """End to end through GatewayWatchdog.start() (fresh interpreter)."""

    _BODY = '''
        import time, bot.gateway_watchdog as gw
        gw.systemd_probe = lambda unit: True        # no systemctl in tests
        class FW:
            def __init__(self): self.states = []; self.events = []
            def write_gateway_state(self, s): self.states.append(dict(s))
            def write_gateway_event(self, **kw): self.events.append(kw)
        fake = _FakeGateway(mute=%(mute)s)
        cfg = gw.WatchdogConfig.from_env(broker_mode="paper", host="127.0.0.1", port=fake.port,
                                         systemd_unit="ibgateway")
        cfg.interval_sec = 1; cfg.api_timeout_sec = 2; cfg.failures_to_down = 2
        fw = FW()
        wd = gw.GatewayWatchdog(cfg, fw)
        wd.start()
        import sys
        print("preimported_in_main=%%s" %% ("ib_insync" in sys.modules))
        deadline = time.time() + 45
        while time.time() < deadline and len(fw.states) < %(ticks)d:
            time.sleep(0.2)
        wd.stop(timeout=10)
        s = wd.current_state()
        print("state=%%s healthy=%%s ticks=%%d err=%%s" %% (s["state"], wd.is_healthy_for_submission(),
              len(fw.states), s.get("last_probe_error")))
        print("persisted=%%s" %% [x["state"] for x in fw.states])
        print("thread_alive=%%s" %% wd._thread.is_alive())
    '''

    @unittest.skipUnless(HAVE_IB, "ib_insync not installed")
    def test_reaches_api_up_healthy_against_local_fake_gateway(self):
        r = _run_fresh(self._BODY % {"mute": False, "ticks": 2})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("preimported_in_main=True", r.stdout)
        self.assertIn("state=api_up_healthy healthy=True", r.stdout, r.stdout + r.stderr)
        self.assertIn("err=None", r.stdout)
        self.assertIn("'api_up_healthy'", r.stdout)          # persisted every tick
        self.assertIn("thread_alive=False", r.stdout)
        self.assertNotIn("tick error", r.stderr)
        self.assertNotIn("no current event loop", r.stdout + r.stderr)

    @unittest.skipUnless(HAVE_IB, "ib_insync not installed")
    def test_port_open_api_dead_is_recorded_as_down_not_frozen(self):
        # The defect's exact situation: TCP open, API unusable. Before A18 the
        # tick aborted and nothing was recorded; now each tick is persisted and
        # the declared state is truthful and fail-closed.
        r = _run_fresh(self._BODY % {"mute": True, "ticks": 2}, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("state=tcp_up_api_down healthy=False ticks=2", r.stdout, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"err=(TimeoutError|ConnectionError|.*[Tt]imeout)")
        self.assertNotIn("tick error", r.stderr)
        self.assertNotIn("no current event loop", r.stdout + r.stderr)


# --------------------------------------------------------------------------
class A3_FailedImportsAndProbesAreSafe(unittest.TestCase):
    """api_probe never raises; _tick records a FAILED probe if it does."""

    def test_import_error_is_a_failed_probe(self):
        with mock.patch.dict(sys.modules, {"ib_insync": None}):
            self.assertEqual(gw.api_probe("127.0.0.1", _closed_port(), 99, 1),
                             (False, None, "ib_insync_not_installed"))

    def test_import_time_runtime_error_is_a_failed_probe(self):
        real_import = __import__

        def boom(name, *a, **kw):
            if name == "ib_insync":
                raise RuntimeError("There is no current event loop in thread 'gw-watchdog'.")
            return real_import(name, *a, **kw)
        with mock.patch.dict(sys.modules), mock.patch("builtins.__import__", side_effect=boom):
            sys.modules.pop("ib_insync", None)
            ok, lat, err = gw.api_probe("127.0.0.1", _closed_port(), 99, 1)
        self.assertEqual((ok, lat), (False, None))
        self.assertTrue(err.startswith("ib_insync_import_failed: RuntimeError"), err)

    def _fake_module(self, ib_cls):
        m = type(sys)("ib_insync"); m.IB = ib_cls
        return mock.patch.dict(sys.modules, {"ib_insync": m})

    def test_constructor_error_is_a_failed_probe(self):
        class IB:
            def __init__(self):
                raise RuntimeError("There is no current event loop in thread 'gw-watchdog'.")
        with self._fake_module(IB):
            ok, lat, err = gw.api_probe("127.0.0.1", _closed_port(), 99, 1)
        self.assertEqual((ok, lat), (False, None))
        self.assertTrue(err.startswith("ib_construct_failed: RuntimeError"), err)

    def test_connect_error_is_a_failed_probe_and_never_disconnects_a_dead_client(self):
        calls = []

        class IB:
            def connect(self, *a, **kw): raise ConnectionRefusedError("refused")
            def isConnected(self): return False
            def disconnect(self): calls.append("disconnect")
        with self._fake_module(IB):
            ok, lat, err = gw.api_probe("127.0.0.1", _closed_port(), 99, 1)
        self.assertEqual((ok, lat), (False, None))
        self.assertTrue(err.startswith("ConnectionRefusedError"), err)
        self.assertEqual(calls, [])

    def test_success_with_fake_client_disconnects(self):
        calls = []

        class IB:
            def connect(self, *a, **kw): calls.append(("connect", kw.get("readonly")))
            def reqCurrentTime(self): calls.append("time")
            def isConnected(self): return True
            def disconnect(self): calls.append("disconnect")
        with self._fake_module(IB):
            ok, lat, err = gw.api_probe("127.0.0.1", _closed_port(), 99, 1)
        self.assertTrue(ok); self.assertIsNone(err); self.assertIsInstance(lat, int)
        self.assertEqual(calls, [("connect", True), "time", "disconnect"])

    def test_probe_from_a_thread_creates_and_closes_its_own_loop(self):
        seen = {}

        class IB:
            def connect(self, *a, **kw):
                seen["loop"] = asyncio.get_event_loop_policy().get_event_loop()
            def reqCurrentTime(self): pass
            def isConnected(self): return False
            def disconnect(self): pass

        def t():
            seen["r"] = gw.api_probe("127.0.0.1", _closed_port(), 99, 1)
        with self._fake_module(IB):
            th = threading.Thread(target=t, name="probe-test"); th.start(); th.join(10)
        self.assertTrue(seen["r"][0])
        self.assertTrue(seen["loop"].is_closed())

    def _wd(self, failures_to_down=2):
        fw = _FakeFlywheel()
        return gw.GatewayWatchdog(_cfg(_closed_port(), failures_to_down), fw), fw

    def test_tick_records_a_failed_probe_when_api_probe_raises(self):
        wd, fw = self._wd(failures_to_down=1)
        with mock.patch.object(gw, "systemd_probe", return_value=True), \
             mock.patch.object(gw, "tcp_probe", return_value=True), \
             mock.patch.object(gw, "api_probe", side_effect=RuntimeError("boom")):
            wd._tick()
        s = wd.current_state()
        self.assertEqual(s["state"], gw.STATE_TCP_UP_API_DOWN)
        self.assertTrue(s["last_probe_error"].startswith("api_probe_raised: RuntimeError"))
        self.assertFalse(wd.is_healthy_for_submission())
        self.assertEqual(len(fw.states), 1)           # persisted, not skipped

    def test_a_raising_probe_never_counts_as_success(self):
        wd, fw = self._wd(failures_to_down=2)
        with mock.patch.object(gw, "systemd_probe", return_value=True), \
             mock.patch.object(gw, "tcp_probe", return_value=True):
            with mock.patch.object(gw, "api_probe", return_value=(True, 5, None)):
                wd._tick()
            self.assertTrue(wd.is_healthy_for_submission())
            with mock.patch.object(gw, "api_probe", side_effect=RuntimeError("boom")):
                wd._tick(); wd._tick()
        self.assertEqual(wd.current_state()["state"], gw.STATE_TCP_UP_API_DOWN)
        self.assertEqual(wd.current_state()["failure_count"], 2)
        self.assertFalse(wd.is_healthy_for_submission())
        self.assertEqual(len(fw.states), 3)


# --------------------------------------------------------------------------
class A4_GatewayDownRemainsFailClosed(unittest.TestCase):
    def _run(self, service, tcp, api, ticks=2):
        fw = _FakeFlywheel()
        wd = gw.GatewayWatchdog(_cfg(_closed_port(), 2), fw)
        with mock.patch.object(gw, "systemd_probe", return_value=service), \
             mock.patch.object(gw, "tcp_probe", return_value=tcp), \
             mock.patch.object(gw, "api_probe", return_value=api):
            for _ in range(ticks):
                wd._tick()
        return wd, fw

    def test_initial_state_is_not_healthy(self):
        wd = gw.GatewayWatchdog(_cfg(_closed_port()), _FakeFlywheel())
        self.assertEqual(wd.current_state()["state"], gw.STATE_UNKNOWN)
        self.assertFalse(wd.is_healthy_for_submission())

    def test_service_down(self):
        wd, fw = self._run(False, True, (True, 1, None))
        self.assertEqual(wd.current_state()["state"], gw.STATE_SERVICE_DOWN)
        self.assertFalse(wd.is_healthy_for_submission())

    def test_tcp_down(self):
        wd, _ = self._run(True, False, (True, 1, None))
        self.assertEqual(wd.current_state()["state"], gw.STATE_SERVICE_RUNNING_TCP_DOWN)
        self.assertFalse(wd.is_healthy_for_submission())

    def test_unreadable_systemd_and_tcp_down(self):
        wd, _ = self._run(None, False, (True, 1, None))
        self.assertFalse(wd.is_healthy_for_submission())

    def test_api_down(self):
        wd, fw = self._run(True, True, (False, None, "TimeoutError: x"))
        s = wd.current_state()
        self.assertEqual(s["state"], gw.STATE_TCP_UP_API_DOWN)
        self.assertEqual(s["last_probe_error"], "TimeoutError: x")
        self.assertFalse(wd.is_healthy_for_submission())
        self.assertEqual(len(fw.states), 2)


# --------------------------------------------------------------------------
class A5_ThreadLoopAndStaticGuards(unittest.TestCase):
    def test_ensure_thread_event_loop_is_a_noop_in_the_main_thread(self):
        self.assertIs(threading.current_thread(), threading.main_thread())
        self.assertIsNone(gw.ensure_thread_event_loop())

    def test_thread_gets_one_loop_and_the_second_call_reuses_it(self):
        out = {}

        def t():
            first = gw.ensure_thread_event_loop()
            out["first"] = first
            out["second"] = gw.ensure_thread_event_loop()
            out["current"] = asyncio.get_event_loop_policy().get_event_loop()
            first.close()
        th = threading.Thread(target=t); th.start(); th.join(10)
        self.assertIsNotNone(out["first"])
        self.assertIsNone(out["second"])
        self.assertIs(out["current"], out["first"])

    def test_watchdog_thread_loop_is_closed_when_the_watchdog_stops(self):
        made = []
        real = gw.ensure_thread_event_loop

        def spy():
            loop = real()
            if loop is not None:
                made.append(loop)
            return loop
        fw = _FakeFlywheel()
        wd = gw.GatewayWatchdog(_cfg(_closed_port()), fw)
        with mock.patch.object(gw, "ensure_thread_event_loop", side_effect=spy), \
             mock.patch.object(gw, "preimport_ib_insync", return_value=True), \
             mock.patch.object(gw, "systemd_probe", return_value=False):
            wd.start()
            deadline = time.time() + 10
            while time.time() < deadline and not fw.states:
                time.sleep(0.05)
            wd.stop(timeout=10)
        self.assertEqual(len(made), 1)
        self.assertTrue(made[0].is_closed())
        self.assertFalse(wd._thread.is_alive())
        self.assertEqual(fw.states[0]["state"], gw.STATE_UNKNOWN)   # 1 failure < failures_to_down

    def _tree(self):
        with open(os.path.join(REPO, "bot", "gateway_watchdog.py")) as f:
            return ast.parse(f.read())

    def test_ib_construction_and_connect_are_inside_try_blocks(self):
        fn = next(n for n in ast.walk(self._tree())
                  if isinstance(n, ast.FunctionDef) and n.name == "_api_probe_once")
        tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
        guarded = set()
        for t in tries:
            for n in ast.walk(ast.Module(body=t.body, type_ignores=[])):
                if isinstance(n, ast.Call):
                    f = n.func
                    guarded.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
        self.assertTrue({"IB", "connect", "reqCurrentTime"} <= guarded, guarded)

    def test_tick_guards_the_api_probe_call(self):
        fn = next(n for n in ast.walk(self._tree())
                  if isinstance(n, ast.FunctionDef) and n.name == "_tick")
        ok = any(isinstance(n, ast.Try) and any(
            isinstance(c, ast.Call) and getattr(c.func, "id", "") == "api_probe"
            for c in ast.walk(ast.Module(body=n.body, type_ignores=[]))) for n in ast.walk(fn))
        self.assertTrue(ok)

    def test_no_test_in_this_module_targets_an_ibkr_port(self):
        with open(os.path.abspath(__file__)) as f:
            src = f.read()
        for p in IBKR_PORTS:
            # the only occurrences are the refusal lists
            self.assertLessEqual(src.count(str(p)), 2, p)

    def test_state_machine_rules_unchanged(self):
        # derive_state truth table (M15.1) is untouched by A18.
        P = gw.ProbeResult
        now = gw._utc_now()
        self.assertEqual(P(ts=now, service_running=False, tcp_ok=True, api_ok=True).derive_state(),
                         gw.STATE_SERVICE_DOWN)
        self.assertEqual(P(ts=now, service_running=True, tcp_ok=False, api_ok=True).derive_state(),
                         gw.STATE_SERVICE_RUNNING_TCP_DOWN)
        self.assertEqual(P(ts=now, service_running=True, tcp_ok=True, api_ok=False).derive_state(),
                         gw.STATE_TCP_UP_API_DOWN)
        self.assertEqual(P(ts=now, service_running=True, tcp_ok=True, api_ok=True).derive_state(),
                         gw.STATE_API_UP_HEALTHY)


if __name__ == "__main__":
    unittest.main()
