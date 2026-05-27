"""
M13.2 — eToro read adapter test suite.

ALL HTTP IS MOCKED. Tests never hit the live eToro API.
Deterministic: token-bucket uses injectable clock + sleeper.

Run: python3 test_m13_2_etoro_read.py
"""
from __future__ import annotations

import ast
import glob
import io
import json
import logging
import os
import sys
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.etoro.client import EtoroClient, TokenBucket
from bot.etoro.errors import (
    EtoroAuthError,
    EtoroError,
    EtoroRateLimitError,
    EtoroRouteError,
    EtoroTransientError,
    EtoroValidationError,
)
from bot.etoro.read_adapter import (
    EtoroReadAdapter,
    HistoryItem,
    IdentityResult,
    InstrumentMatch,
    PortfolioSnapshot,
    Rate,
)


# ---------------------------------------------------------------------------
# Test harness: virtual clock + recording sleeper + scriptable transport
# ---------------------------------------------------------------------------
class VirtualClock:
    """Deterministic clock for tests. Time advances ONLY when sleep() is
    called or when tick() is explicitly invoked."""
    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps: List[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        # Record the sleep and advance virtual time without blocking.
        self.sleeps.append(float(seconds))
        self.now += float(seconds)


class ScriptedTransport:
    """Returns canned responses in order. Each entry:
        (status, headers, body_bytes_or_str_or_dict)
    or a callable that raises an exception when invoked.
    Also records every call so tests can assert on URL/headers.
    """
    def __init__(self, responses: List[Any]):
        self.responses = list(responses)
        self.calls: List[Tuple[str, Dict[str, str], float]] = []

    def __call__(self, url: str, headers: Dict[str, str],
                 timeout: float) -> Tuple[int, Dict[str, str], bytes]:
        self.calls.append((url, dict(headers), timeout))
        if not self.responses:
            raise AssertionError(
                f'ScriptedTransport ran out of responses at call '
                f'{len(self.calls)} -> {url}'
            )
        item = self.responses.pop(0)
        if callable(item):
            return item(url, headers, timeout)
        status, hdrs, body = item
        if isinstance(body, dict) or isinstance(body, list):
            body = json.dumps(body).encode('utf-8')
        elif isinstance(body, str):
            body = body.encode('utf-8')
        return status, hdrs or {}, body


def make_client(
    responses: List[Any],
    rate_limit_per_min: int = 60,
    max_retries: int = 3,
) -> Tuple[EtoroClient, ScriptedTransport, VirtualClock]:
    """Build a fully-mocked client. No network, no env."""
    clock = VirtualClock()
    transport = ScriptedTransport(responses)
    # Counter-based request_id factory: predictable, but still unique per call
    counter = {'n': 0}
    def rid_factory() -> str:
        counter['n'] += 1
        return f'00000000-0000-0000-0000-{counter["n"]:012d}'
    client = EtoroClient(
        api_key='TEST-API-KEY-PUBLIC-HALF',
        user_key='TEST-USER-KEY-USER-HALF',
        timeout_sec=5.0,
        max_retries=max_retries,
        rate_limit_per_min=rate_limit_per_min,
        transport=transport,
        clock=clock,
        sleeper=clock.sleep,
        request_id_factory=rid_factory,
    )
    return client, transport, clock


# ===========================================================================
# Section 1: ENDPOINT SHAPE PARSING (5 verified + 1 docs-corrected)
# ===========================================================================
class TestEndpointShapes(unittest.TestCase):

    def test_get_me(self):
        c, t, _ = make_client([(200, {}, {
            'gcid': 100, 'realCid': 200, 'demoCid': 300,
        })])
        a = EtoroReadAdapter(c)
        r = a.get_identity()
        self.assertIsInstance(r, IdentityResult)
        self.assertEqual(r.gcid, 100)
        self.assertEqual(r.realCid, 200)
        self.assertEqual(r.demoCid, 300)
        self.assertTrue(t.calls[0][0].endswith('/api/v1/me'))

    def test_get_portfolio(self):
        c, t, _ = make_client([(200, {}, {'clientPortfolio': {
            'credit': 0, 'bonusCredit': 0,
            'positions': [{'instrumentId': 1, 'units': 1.0}],
            'orders': [], 'entryOrders': [], 'exitOrders': [],
            'mirrors': [], 'stockOrders': [],
            'ordersForOpen': [], 'ordersForClose': [],
        }})])
        a = EtoroReadAdapter(c)
        snap = a.get_portfolio()
        self.assertIsInstance(snap, PortfolioSnapshot)
        self.assertEqual(snap.credit, 0)
        self.assertEqual(len(snap.positions), 1)
        self.assertTrue(t.calls[0][0].endswith('/trading/info/portfolio'))

    def test_get_real_pnl(self):
        c, t, _ = make_client([(200, {}, {'clientPortfolio': {
            'credit': 0, 'bonusCredit': 0, 'unrealizedPnL': 0,
            'accountCurrencyId': 1,
            'positions': [], 'orders': [], 'entryOrders': [],
            'exitOrders': [], 'mirrors': [], 'stockOrders': [],
            'ordersForOpen': [], 'ordersForClose': [],
        }})])
        a = EtoroReadAdapter(c)
        snap = a.get_real_pnl()
        self.assertEqual(snap.unrealized_pnl, 0)
        self.assertEqual(snap.account_currency_id, 1)
        self.assertTrue(t.calls[0][0].endswith('/trading/info/real/pnl'))

    def test_search_instrument(self):
        c, t, _ = make_client([(200, {}, {
            'items': [
                {'instrumentId': 1234567, 'symbolFull': 'AAPL'},
                {'instrumentId': 2222222, 'symbolFull': 'AAPL2'},
            ],
            'page': 1, 'pageSize': 20, 'totalItems': 2,
        })])
        a = EtoroReadAdapter(c)
        matches = a.search_instrument('AAPL')
        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0].instrument_id, 1234567)
        self.assertIn('search=AAPL', t.calls[0][0])

    def test_get_rates_corrected_route(self):
        """R4 was a 404 because we used /market-data/rates.
        Verified path per OpenAPI: /market-data/instruments/rates"""
        c, t, _ = make_client([(200, {}, {'rates': [
            {'instrumentID': 1, 'bid': 100.0, 'ask': 100.5,
             'lastExecution': 100.25,
             'conversionRateBid': 1.0, 'conversionRateAsk': 1.0,
             'date': '2026-05-25T00:00:00Z'},
        ]})])
        a = EtoroReadAdapter(c)
        rates = a.get_rates([1])
        self.assertEqual(len(rates), 1)
        self.assertEqual(rates[0].instrument_id, 1)
        self.assertEqual(rates[0].bid, 100.0)
        # CRITICAL: path includes /instruments/ segment (R4 correction)
        self.assertIn('/market-data/instruments/rates', t.calls[0][0])
        self.assertIn('instrumentIds=1', t.calls[0][0])

    def test_get_trade_history(self):
        c, t, _ = make_client([(200, {}, [
            {'positionId': 1, 'instrumentId': 100, 'isBuy': True,
             'units': 1.0, 'openRate': 50.0, 'closeRate': 55.0,
             'netProfit': 5.0, 'fees': 0.0},
        ])])
        a = EtoroReadAdapter(c)
        hist = a.get_trade_history('2025-01-01')
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0].position_id, 1)
        self.assertEqual(hist[0].net_profit, 5.0)
        self.assertIn('minDate=2025-01-01', t.calls[0][0])


# ===========================================================================
# Section 2: AUTH HEADER CONSTRUCTION
# ===========================================================================
class TestAuthHeaders(unittest.TestCase):

    def test_required_headers_present(self):
        c, t, _ = make_client([(200, {}, {'gcid': 0})])
        EtoroReadAdapter(c).get_identity()
        hdrs = t.calls[0][1]
        self.assertEqual(hdrs.get('x-api-key'), 'TEST-API-KEY-PUBLIC-HALF')
        self.assertEqual(hdrs.get('x-user-key'), 'TEST-USER-KEY-USER-HALF')
        self.assertEqual(hdrs.get('Accept'), 'application/json')
        self.assertIn('x-request-id', hdrs)

    def test_request_id_unique_per_call(self):
        c, t, _ = make_client([
            (200, {}, {'gcid': 0}),
            (200, {}, {'gcid': 0}),
        ])
        a = EtoroReadAdapter(c)
        a.get_identity()
        a.get_identity()
        rid1 = t.calls[0][1]['x-request-id']
        rid2 = t.calls[1][1]['x-request-id']
        self.assertNotEqual(rid1, rid2,
                            'x-request-id must be fresh per call')

    def test_default_request_id_is_uuid4(self):
        """Default factory (no override) produces a valid UUIDv4."""
        clock = VirtualClock()
        transport = ScriptedTransport([(200, {}, {'gcid': 0})])
        c = EtoroClient(
            api_key='k', user_key='u',
            transport=transport, clock=clock, sleeper=clock.sleep,
            # NO request_id_factory override -> default uuid4
        )
        c.get('/me')
        rid = transport.calls[0][1]['x-request-id']
        parsed = uuid.UUID(rid)  # raises if not a valid UUID
        self.assertEqual(parsed.version, 4)


# ===========================================================================
# Section 3: NO WRITE METHODS — STRUCTURAL AST PROOF
# ===========================================================================
class TestNoWriteCapability(unittest.TestCase):
    """The structural enforcement of the M13.2 contract:
    bot/etoro/ contains zero code paths capable of issuing a non-GET
    HTTP request.

    AST-based — does NOT match comments or docstrings (those are not
    children of executable nodes we inspect)."""

    FORBIDDEN_METHOD_LITERALS = {'POST', 'DELETE', 'PUT', 'PATCH'}
    FORBIDDEN_FUNCTION_NAMES = {
        'post', 'delete', 'put', 'patch',
        '_post', '_delete', '_put', '_patch',
    }

    def _collect_offenders(self):
        offenders = []
        repo = Path(__file__).resolve().parent
        for fname in sorted(glob.glob(str(repo / 'bot' / 'etoro' / '*.py'))):
            with open(fname) as f:
                tree = ast.parse(f.read(), filename=fname)
            rel = os.path.relpath(fname, str(repo))
            # 1. Function/method definitions named like writes
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name.lower() in self.FORBIDDEN_FUNCTION_NAMES:
                        offenders.append(
                            f'{rel}:{node.lineno} forbidden function name: {node.name}'
                        )
            # 2. Call expressions: any keyword arg method='POST'|...
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for kw in (node.keywords or []):
                        if kw.arg == 'method' and isinstance(kw.value, ast.Constant):
                            val = str(kw.value.value).upper()
                            if val in self.FORBIDDEN_METHOD_LITERALS:
                                offenders.append(
                                    f'{rel}:{node.lineno} method= keyword: {val}'
                                )
                    # 3. Calls with bare string constants like 'POST' in args
                    for a in (node.args or []):
                        if isinstance(a, ast.Constant) and isinstance(a.value, str):
                            if a.value.upper() in self.FORBIDDEN_METHOD_LITERALS:
                                offenders.append(
                                    f'{rel}:{node.lineno} forbidden literal arg: {a.value!r}'
                                )
        return offenders

    def test_no_write_methods_in_etoro_package(self):
        offenders = self._collect_offenders()
        self.assertEqual(
            offenders, [],
            'M13.2 contract violated — write capability detected:\n  '
            + '\n  '.join(offenders),
        )

    def test_client_exposes_only_get(self):
        """Belt-and-braces: confirm EtoroClient public methods are exactly
        what the contract allows. No post/delete/put/patch attributes."""
        for forbidden in ('post', 'delete', 'put', 'patch', 'request', 'send'):
            self.assertFalse(
                hasattr(EtoroClient, forbidden),
                f'EtoroClient must NOT expose {forbidden}',
            )
        # Positive: must expose get
        self.assertTrue(callable(getattr(EtoroClient, 'get', None)))

    def test_read_adapter_does_not_subclass_broker_adapter(self):
        """EtoroReadAdapter is library code, not a BrokerAdapter."""
        from bot.brokers.base import BrokerAdapter
        self.assertFalse(issubclass(EtoroReadAdapter, BrokerAdapter))


# ===========================================================================
# Section 4: IMPORT SAFETY (ChatGPT correction #6)
# ===========================================================================
class TestImportSafety(unittest.TestCase):
    """Importing bot.etoro.* must not:
    - read environment variables
    - make any network call
    - instantiate any client with credentials
    """

    def test_import_does_not_read_environment(self):
        """AST proof: no top-level os.environ / os.getenv / getenv reads
        anywhere in bot/etoro/."""
        repo = Path(__file__).resolve().parent
        offenders = []
        for fname in sorted(glob.glob(str(repo / 'bot' / 'etoro' / '*.py'))):
            with open(fname) as f:
                src = f.read()
                tree = ast.parse(src, filename=fname)
            rel = os.path.relpath(fname, str(repo))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    if (isinstance(node.value, ast.Name)
                            and node.value.id == 'os'
                            and node.attr in ('environ', 'getenv')):
                        offenders.append(f'{rel}:{node.lineno} os.{node.attr}')
                if isinstance(node, ast.Name) and node.id == 'getenv':
                    offenders.append(f'{rel}:{node.lineno} bare getenv')
        self.assertEqual(offenders, [],
                         f'bot/etoro/ must not read env vars: {offenders}')

    def test_import_makes_no_network_calls(self):
        """Re-import the modules and assert nothing called the network.
        We achieve this by monkeypatching urllib.request.urlopen BEFORE
        re-importing — if any module-level code path tries to open a
        URL, the test fails."""
        import importlib
        import urllib.request as urlreq
        opened_urls: List[str] = []
        real = urlreq.urlopen
        def tracking(*args, **kwargs):
            opened_urls.append(str(args[0]) if args else '?')
            raise AssertionError(
                'urlopen called during import — forbidden in M13.2'
            )
        urlreq.urlopen = tracking
        try:
            # Force re-import to exercise the module-init path under the tracker
            for mod in ('bot.etoro.client', 'bot.etoro.read_adapter',
                        'bot.etoro.errors', 'bot.etoro'):
                if mod in sys.modules:
                    del sys.modules[mod]
            import bot.etoro  # noqa: F401
            import bot.etoro.errors  # noqa: F401
            import bot.etoro.client  # noqa: F401
            import bot.etoro.read_adapter  # noqa: F401
        finally:
            urlreq.urlopen = real
        self.assertEqual(opened_urls, [],
                         f'Imports made network calls: {opened_urls}')

    def test_import_does_not_instantiate_client(self):
        """No top-level EtoroClient(...) / EtoroReadAdapter(...) instances."""
        import importlib
        for mod_name in ('bot.etoro.client', 'bot.etoro.read_adapter'):
            if mod_name in sys.modules:
                del sys.modules[mod_name]
            mod = importlib.import_module(mod_name)
            # No instances should exist as module-level attributes
            for attr_name in dir(mod):
                if attr_name.startswith('_'):
                    continue
                val = getattr(mod, attr_name)
                # Anything that's an INSTANCE of EtoroClient or
                # EtoroReadAdapter at module level is forbidden.
                from bot.etoro.client import EtoroClient as _C
                from bot.etoro.read_adapter import EtoroReadAdapter as _A
                self.assertFalse(
                    isinstance(val, (_C, _A)),
                    f'{mod_name}.{attr_name} is a module-level instance',
                )


# ===========================================================================
# Section 5: ERROR MAPPING
# ===========================================================================
class TestErrorMapping(unittest.TestCase):

    def test_401_raises_auth_error(self):
        c, _, _ = make_client([(401, {}, {'errorCode': 'Unauthorized'})])
        with self.assertRaises(EtoroAuthError):
            EtoroReadAdapter(c).get_identity()

    def test_403_raises_auth_error(self):
        c, _, _ = make_client([(403, {}, {'errorCode': 'Forbidden'})])
        with self.assertRaises(EtoroAuthError):
            EtoroReadAdapter(c).get_identity()

    def test_404_raises_route_error(self):
        c, _, _ = make_client([(404, {}, {'errorCode': 'RouteNotFound'})])
        with self.assertRaises(EtoroRouteError):
            EtoroReadAdapter(c).get_identity()

    def test_429_raises_rate_limit_error_after_retries(self):
        c, _, _ = make_client(
            [(429, {'retry-after': '2'}, {})] * 5,
            max_retries=3,
        )
        with self.assertRaises(EtoroRateLimitError) as ctx:
            EtoroReadAdapter(c).get_identity()
        self.assertEqual(ctx.exception.retry_after, 2.0)

    def test_400_raises_validation_error(self):
        c, _, _ = make_client([(400, {}, {'errorCode': 'BadRequest'})])
        with self.assertRaises(EtoroValidationError):
            EtoroReadAdapter(c).get_identity()

    def test_500_raises_transient_after_retries(self):
        c, _, _ = make_client(
            [(500, {}, {'errorCode': 'ServerError'})] * 5,
            max_retries=3,
        )
        with self.assertRaises(EtoroTransientError):
            EtoroReadAdapter(c).get_identity()

    def test_network_error_raises_transient(self):
        def fail(*_a, **_k):
            raise EtoroTransientError('connection refused')
        c, _, clock = make_client([fail] * 5, max_retries=3)
        with self.assertRaises(EtoroTransientError):
            EtoroReadAdapter(c).get_identity()


# ===========================================================================
# Section 6: RETRY POLICY
# ===========================================================================
class TestRetryPolicy(unittest.TestCase):

    def test_4xx_never_retried(self):
        """401/403/404/400 are deterministic errors. Don't waste budget."""
        for status, exc in [
            (401, EtoroAuthError),
            (403, EtoroAuthError),
            (404, EtoroRouteError),
            (400, EtoroValidationError),
        ]:
            c, t, _ = make_client(
                [(status, {}, {'errorCode': 'X'})],
                max_retries=3,
            )
            with self.assertRaises(exc):
                EtoroReadAdapter(c).get_identity()
            self.assertEqual(
                len(t.calls), 1,
                f'{status} must not be retried, got {len(t.calls)} calls',
            )

    def test_500_retried_then_succeeds(self):
        c, t, clock = make_client([
            (500, {}, {}),
            (500, {}, {}),
            (200, {}, {'gcid': 7}),
        ], max_retries=3)
        r = EtoroReadAdapter(c).get_identity()
        self.assertEqual(r.gcid, 7)
        self.assertEqual(len(t.calls), 3)
        # Two backoff sleeps (after the two 500s) plus token-bucket sleeps
        backoffs = [s for s in clock.sleeps if s >= 1.0]
        self.assertGreaterEqual(len(backoffs), 2)

    def test_429_respects_retry_after(self):
        c, t, clock = make_client([
            (429, {'retry-after': '3'}, {}),
            (200, {}, {'gcid': 1}),
        ], max_retries=3)
        EtoroReadAdapter(c).get_identity()
        # Verify a 3-second sleep happened
        self.assertIn(3.0, clock.sleeps)

    def test_backoff_exponential(self):
        """500, 500, 500 — sleeps 2, 4 between retries."""
        c, t, clock = make_client([
            (500, {}, {}),
            (500, {}, {}),
            (500, {}, {}),
        ], max_retries=3)
        with self.assertRaises(EtoroTransientError):
            EtoroReadAdapter(c).get_identity()
        self.assertIn(2.0, clock.sleeps)
        self.assertIn(4.0, clock.sleeps)


# ===========================================================================
# Section 7: TOKEN-BUCKET RATE LIMITER (DETERMINISTIC, NO REAL SLEEP)
# ===========================================================================
class TestTokenBucket(unittest.TestCase):

    def test_bucket_starts_full(self):
        clock = VirtualClock()
        b = TokenBucket(
            capacity=60, refill_per_sec=1.0,
            clock=clock, sleeper=clock.sleep,
        )
        # Should be able to take 60 tokens without sleeping
        for _ in range(60):
            b.acquire(1)
        self.assertEqual(clock.sleeps, [])

    def test_bucket_sleeps_when_exhausted(self):
        clock = VirtualClock()
        b = TokenBucket(
            capacity=2, refill_per_sec=1.0,
            clock=clock, sleeper=clock.sleep,
        )
        b.acquire(1)
        b.acquire(1)
        b.acquire(1)  # forces a wait
        self.assertGreater(len(clock.sleeps), 0)
        # Total wait should approximately equal the time needed to
        # refill 1 token at 1 token/sec
        self.assertAlmostEqual(sum(clock.sleeps), 1.0, places=2)

    def test_independent_buckets_per_instance(self):
        """Two EtoroClient instances must have independent rate budgets
        (matches eToro's per-user-key semantics)."""
        c1, _, _ = make_client(
            [(200, {}, {'gcid': 1})] * 5, rate_limit_per_min=60,
        )
        c2, _, _ = make_client(
            [(200, {}, {'gcid': 2})] * 5, rate_limit_per_min=60,
        )
        # Confirmed by checking bucket identity
        self.assertIsNot(c1._bucket, c2._bucket)

    def test_unit_tests_complete_quickly(self):
        """Smoke: confirm no real time.sleep blocked the runner.
        Each test in this class records sleeps in a list; the test
        runner itself was not blocked."""
        import time as _time
        t0 = _time.monotonic()
        clock = VirtualClock()
        b = TokenBucket(
            capacity=1, refill_per_sec=1.0,
            clock=clock, sleeper=clock.sleep,
        )
        for _ in range(20):
            b.acquire(1)
        # Real wall-clock elapsed should be near zero — virtual clock
        # absorbs all the waits.
        self.assertLess(_time.monotonic() - t0, 0.1)


# ===========================================================================
# Section 8: DEFENSIVE PARSING
# ===========================================================================
class TestDefensiveParsing(unittest.TestCase):

    def test_missing_clientPortfolio_returns_empty(self):
        c, _, _ = make_client([(200, {}, {})])
        snap = EtoroReadAdapter(c).get_portfolio()
        self.assertEqual(snap.credit, None)
        self.assertEqual(snap.positions, [])

    def test_non_json_body_raises_validation(self):
        c, _, _ = make_client([(200, {}, b'<html>oops</html>')])
        with self.assertRaises(EtoroValidationError):
            EtoroReadAdapter(c).get_identity()

    def test_unexpected_extra_fields_ignored(self):
        c, _, _ = make_client([(200, {}, {
            'gcid': 1, 'realCid': 2, 'demoCid': 3,
            'extraField': 'ignored', 'nested': {'x': 1},
        })])
        r = EtoroReadAdapter(c).get_identity()
        self.assertEqual(r.gcid, 1)

    def test_null_fields_dont_crash(self):
        c, _, _ = make_client([(200, {}, {'clientPortfolio': {
            'credit': None, 'positions': None, 'orders': None,
        }})])
        snap = EtoroReadAdapter(c).get_portfolio()
        self.assertIsNone(snap.credit)
        self.assertEqual(snap.positions, [])
        self.assertEqual(snap.orders, [])


# ===========================================================================
# Section 9: get_rates CHUNKING
# ===========================================================================
class TestRatesChunking(unittest.TestCase):

    def test_250_ids_split_into_3_calls(self):
        ids = list(range(1, 251))
        responses = [
            (200, {}, {'rates': [{'instrumentID': i, 'bid': 1.0, 'ask': 1.0}
                                  for i in ids[0:100]]}),
            (200, {}, {'rates': [{'instrumentID': i, 'bid': 1.0, 'ask': 1.0}
                                  for i in ids[100:200]]}),
            (200, {}, {'rates': [{'instrumentID': i, 'bid': 1.0, 'ask': 1.0}
                                  for i in ids[200:250]]}),
        ]
        c, t, _ = make_client(responses)
        rates = EtoroReadAdapter(c).get_rates(ids)
        self.assertEqual(len(t.calls), 3)
        self.assertEqual(len(rates), 250)

    def test_empty_list_no_http_call(self):
        c, t, _ = make_client([])
        rates = EtoroReadAdapter(c).get_rates([])
        self.assertEqual(rates, [])
        self.assertEqual(len(t.calls), 0)


# ===========================================================================
# Section 10: SECRETS NEVER LOGGED
# ===========================================================================
class TestSecretsNotLogged(unittest.TestCase):

    def test_api_key_not_in_log_output(self):
        """Capture log output at DEBUG and assert the api_key/user_key
        substring does not appear anywhere."""
        c, _, _ = make_client([(200, {}, {'gcid': 1})])
        # Capture all log records emitted during the call
        buffer = io.StringIO()
        handler = logging.StreamHandler(buffer)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        prev_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            EtoroReadAdapter(c).get_identity()
        finally:
            root.removeHandler(handler)
            root.setLevel(prev_level)
        log_text = buffer.getvalue()
        # The known credential strings used in the test fixture
        self.assertNotIn('TEST-API-KEY-PUBLIC-HALF', log_text,
                         'api_key leaked into log output')
        self.assertNotIn('TEST-USER-KEY-USER-HALF', log_text,
                         'user_key leaked into log output')

    def test_redact_method_replaces_credentials(self):
        c, _, _ = make_client([(200, {}, {'gcid': 1})])
        redacted = c._redact({
            'x-api-key': 'TEST-API-KEY-PUBLIC-HALF',
            'x-user-key': 'TEST-USER-KEY-USER-HALF',
            'Accept': 'application/json',
        })
        self.assertNotIn('TEST-API-KEY', redacted['x-api-key'])
        self.assertNotIn('TEST-USER-KEY', redacted['x-user-key'])
        self.assertEqual(redacted['Accept'], 'application/json')


# ===========================================================================
# Section 11: CONSTRUCTOR-INJECTED CREDENTIALS, NEVER ENV
# ===========================================================================
class TestNoEnvCoupling(unittest.TestCase):

    def test_constructor_requires_creds(self):
        with self.assertRaises(EtoroAuthError):
            EtoroClient(api_key='', user_key='')
        with self.assertRaises(EtoroAuthError):
            EtoroClient(api_key='x', user_key='')
        with self.assertRaises(EtoroAuthError):
            EtoroClient(api_key='', user_key='y')

    def test_no_environ_or_getenv_in_etoro_source(self):
        """Hard source-level guarantee — AST-based, ignores docstrings
        and comments. Catches real code references to os.environ /
        os.getenv / getenv() but not documentation that names them."""
        repo = Path(__file__).resolve().parent
        offenders = []
        for fname in sorted(glob.glob(str(repo / 'bot' / 'etoro' / '*.py'))):
            with open(fname) as f:
                tree = ast.parse(f.read(), filename=fname)
            rel = os.path.relpath(fname, str(repo))
            for node in ast.walk(tree):
                # os.environ (Attribute access)
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == 'os'
                        and node.attr in ('environ', 'getenv')):
                    offenders.append(f'{rel}:{node.lineno} os.{node.attr}')
                # bare getenv() call (less common but possible)
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == 'getenv'):
                    offenders.append(f'{rel}:{node.lineno} getenv() call')
                # os.environ[...] subscript also counts
                if (isinstance(node, ast.Subscript)
                        and isinstance(node.value, ast.Attribute)
                        and isinstance(node.value.value, ast.Name)
                        and node.value.value.id == 'os'
                        and node.value.attr == 'environ'):
                    offenders.append(f'{rel}:{node.lineno} os.environ[...]')
        self.assertEqual(
            offenders, [],
            f'bot/etoro/ must not read env vars in real code: {offenders}',
        )


# ===========================================================================
# Section 12: REGISTRATION + INTEGRATION ABSENCE
# ===========================================================================
class TestNotRegistered(unittest.TestCase):
    """Verify the read adapter is NOT wired into any live execution path.
    Note: Since M13.3, bot/brokers/__init__.py DOES reference eToro — but
    only to register PaperEtoroBroker for BROKER=etoro_paper and to
    explicitly REJECT BROKER=etoro_real. Both invariants checked here."""

    def test_etoro_real_fails_loudly(self):
        """BROKER=etoro_real must raise ValueError, NEVER fall back silently."""
        from bot.brokers import get_broker
        prev = os.environ.get('BROKER')
        os.environ['BROKER'] = 'etoro_real'
        try:
            with self.assertRaises(ValueError) as ctx:
                get_broker()
            msg = str(ctx.exception).lower()
            self.assertIn('etoro_real', msg)
            self.assertIn('not implemented', msg)
        finally:
            if prev is None:
                os.environ.pop('BROKER', None)
            else:
                os.environ['BROKER'] = prev

    def test_main_py_does_not_import_etoro_package(self):
        repo = Path(__file__).resolve().parent
        with open(repo / 'main.py') as f:
            src = f.read()
        self.assertNotIn('from bot.etoro', src,
                         'main.py must not import bot.etoro in M13.2/M13.3')
        self.assertNotIn('import bot.etoro', src,
                         'main.py must not import bot.etoro in M13.2/M13.3')


if __name__ == '__main__':
    unittest.main(verbosity=2)
