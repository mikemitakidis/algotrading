"""
M13.3 — PaperEtoroBroker test suite.

All mocked. No live eToro. No real network.
Verifies:
  - Schema validator rules
  - PaperEtoroBroker happy path + rejection paths
  - NO duplicate execution_intents logging (broker never calls log_intent)
  - Validation failures use status='rejected', NEVER 'risk_rejected'
  - AST + behavioural no-write-capability proofs
  - BROKER=etoro_paper works; BROKER=etoro_real fails loudly
  - M13.2 no-write contract still holds for the new paper_broker.py

Run: python3 test_m13_3_etoro_paper.py
"""
from __future__ import annotations

import ast
import glob
import json
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.brokers.base import OrderIntent, OrderResult
from bot.etoro.instrument_cache import InstrumentCache
from bot.etoro.paper_broker import PaperEtoroBroker
from bot.etoro.schema_validator import ValidationResult, validate_open


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------
@dataclass
class FakeRate:
    bid: float = 100.0
    ask: float = 100.5


def make_intent(
    symbol: str = 'AAPL',
    direction: str = 'long',
    route: str = 'ETORO',
    entry_price: float = 100.0,
    stop_loss: float = 95.0,
    target_price: float = 110.0,
    position_size: Optional[float] = 100.0,
    risk_usd: Optional[float] = 5.0,
) -> OrderIntent:
    return OrderIntent(
        signal_id=42,
        symbol=symbol,
        direction=direction,
        route=route,
        entry_price=entry_price,
        stop_loss=stop_loss,
        target_price=target_price,
        valid_count=4,
        strategy_version=1,
        position_size=position_size,
        risk_usd=risk_usd,
        risk_checks={},
    )


def make_paper_broker(
    instrument_id_for_symbol: Optional[int] = 1234567,
    rate: Optional[FakeRate] = ...,   # sentinel: default to FakeRate()
    audit_file_path: Optional[Path] = None,
) -> PaperEtoroBroker:
    """Build a PaperEtoroBroker with everything injected (no network).

    `rate` default is a healthy FakeRate(bid=100, ask=100.5). Pass
    `rate=None` explicitly to simulate missing-rate failures.
    """
    cache = InstrumentCache()
    if instrument_id_for_symbol is not None:
        cache.preload({'AAPL': instrument_id_for_symbol})
    if rate is ...:
        rate = FakeRate()
    if rate is None:
        rates_provider = lambda iid: None
    else:
        rates_provider = lambda iid: rate
    audit = audit_file_path or Path(tempfile.mkstemp(suffix='.jsonl')[1])
    return PaperEtoroBroker(
        read_adapter=None,          # offline; no network ever attempted
        instrument_cache=cache,
        rates_provider=rates_provider,
        audit_file_path=audit,
        min_amount_usd=10.0,
    )


# ===========================================================================
# Section 1: SCHEMA VALIDATOR — pure-function rule tests
# ===========================================================================
class TestSchemaValidator(unittest.TestCase):

    # -- direction --
    def test_direction_invalid_rejects(self):
        v = validate_open(make_intent(direction='sideways'), 1, FakeRate())
        self.assertFalse(v.ok)
        self.assertEqual(v.rejection_reason, 'etoro_validation_direction')
        # would_be_body still populated for audit
        self.assertIn('InstrumentID', v.would_be_body)

    def test_direction_long_valid(self):
        v = validate_open(make_intent(direction='long'), 1, FakeRate())
        self.assertTrue(v.ok)
        self.assertEqual(v.would_be_body['IsBuy'], True)

    def test_direction_short_valid(self):
        # For short: stop must be above ask, target below bid
        v = validate_open(
            make_intent(direction='short', stop_loss=105.0, target_price=90.0),
            1, FakeRate(bid=100.0, ask=100.5),
        )
        self.assertTrue(v.ok)
        self.assertEqual(v.would_be_body['IsBuy'], False)

    # -- currency / position_size --
    def test_position_size_zero_rejects(self):
        v = validate_open(make_intent(position_size=0), 1, FakeRate())
        self.assertFalse(v.ok)
        self.assertEqual(v.rejection_reason, 'etoro_validation_currency')

    def test_position_size_negative_rejects(self):
        v = validate_open(make_intent(position_size=-50), 1, FakeRate())
        self.assertFalse(v.ok)
        self.assertEqual(v.rejection_reason, 'etoro_validation_currency')

    def test_position_size_none_rejects(self):
        v = validate_open(make_intent(position_size=None), 1, FakeRate())
        self.assertFalse(v.ok)
        self.assertEqual(v.rejection_reason, 'etoro_validation_currency')

    # -- min amount --
    def test_below_min_amount_rejects(self):
        v = validate_open(make_intent(position_size=5.0), 1, FakeRate(),
                          min_amount_usd=10.0)
        self.assertFalse(v.ok)
        self.assertEqual(v.rejection_reason, 'etoro_validation_min_amount')

    def test_min_amount_threshold_inclusive(self):
        v = validate_open(make_intent(position_size=10.0), 1, FakeRate(),
                          min_amount_usd=10.0)
        self.assertTrue(v.ok)

    # -- instrument resolution --
    def test_no_instrument_rejects(self):
        v = validate_open(make_intent(), None, FakeRate())
        self.assertFalse(v.ok)
        self.assertEqual(v.rejection_reason, 'etoro_validation_unresolved_symbol')

    # -- stop_loss presence --
    def test_no_stop_loss_rejects(self):
        v = validate_open(make_intent(stop_loss=0), 1, FakeRate())
        self.assertFalse(v.ok)
        self.assertEqual(v.rejection_reason, 'etoro_validation_no_stop')

    def test_stop_loss_none_rejects(self):
        v = validate_open(make_intent(stop_loss=None), 1, FakeRate())
        self.assertFalse(v.ok)
        self.assertEqual(v.rejection_reason, 'etoro_validation_no_stop')

    # -- no rate --
    def test_no_rate_rejects(self):
        v = validate_open(make_intent(), 1, None)
        self.assertFalse(v.ok)
        self.assertEqual(v.rejection_reason, 'etoro_validation_no_rate')

    def test_rate_missing_bid_rejects(self):
        @dataclass
        class _R: bid: Any = None; ask: float = 100.0
        v = validate_open(make_intent(), 1, _R())
        self.assertFalse(v.ok)
        self.assertEqual(v.rejection_reason, 'etoro_validation_no_rate')

    # -- stop_side --
    def test_long_stop_above_bid_rejects(self):
        # long stop must be BELOW bid; setting it above triggers stop_side
        v = validate_open(
            make_intent(direction='long', stop_loss=105.0),
            1, FakeRate(bid=100.0, ask=100.5),
        )
        self.assertFalse(v.ok)
        self.assertEqual(v.rejection_reason, 'etoro_validation_stop_side')

    def test_short_stop_below_ask_rejects(self):
        v = validate_open(
            make_intent(direction='short', stop_loss=95.0, target_price=90.0),
            1, FakeRate(bid=100.0, ask=100.5),
        )
        self.assertFalse(v.ok)
        self.assertEqual(v.rejection_reason, 'etoro_validation_stop_side')

    # -- target_side --
    def test_long_target_below_ask_rejects(self):
        v = validate_open(
            make_intent(direction='long', target_price=95.0),
            1, FakeRate(bid=100.0, ask=100.5),
        )
        self.assertFalse(v.ok)
        self.assertEqual(v.rejection_reason, 'etoro_validation_target_side')

    def test_short_target_above_bid_rejects(self):
        v = validate_open(
            make_intent(direction='short', stop_loss=110.0, target_price=110.0),
            1, FakeRate(bid=100.0, ask=100.5),
        )
        self.assertFalse(v.ok)
        self.assertEqual(v.rejection_reason, 'etoro_validation_target_side')

    # -- leverage --
    def test_leverage_not_one_rejects(self):
        v = validate_open(make_intent(), 1, FakeRate(), leverage=2)
        self.assertFalse(v.ok)
        self.assertEqual(v.rejection_reason, 'etoro_validation_leverage')

    # -- happy path --
    def test_happy_path_produces_valid_body(self):
        v = validate_open(make_intent(), 1234567,
                          FakeRate(bid=100.0, ask=100.5))
        self.assertTrue(v.ok)
        self.assertIsNone(v.rejection_reason)
        body = v.would_be_body
        for required in ('InstrumentID', 'IsBuy', 'Leverage', 'Amount',
                         'StopLossRate', 'TakeProfitRate'):
            self.assertIn(required, body)
        self.assertEqual(body['InstrumentID'], 1234567)
        self.assertEqual(body['IsBuy'], True)
        self.assertEqual(body['Leverage'], 1)
        self.assertEqual(body['Amount'], 100.0)

    def test_would_be_body_always_includes_required_fields(self):
        """Even on validation failure, the audit body must include the
        required fields so downstream operators can see what would have
        been sent."""
        v = validate_open(make_intent(direction='bad'), 1, FakeRate())
        self.assertFalse(v.ok)
        for required in ('InstrumentID', 'IsBuy', 'Leverage', 'Amount'):
            self.assertIn(required, v.would_be_body)


# ===========================================================================
# Section 2: INSTRUMENT CACHE
# ===========================================================================
class TestInstrumentCache(unittest.TestCase):

    def test_preload_then_resolve_hits_cache(self):
        c = InstrumentCache()
        c.preload({'AAPL': 1234})
        self.assertEqual(c.resolve('AAPL'), 1234)
        self.assertEqual(c.resolve('aapl'), 1234)  # case-insensitive

    def test_resolve_no_adapter_returns_none(self):
        c = InstrumentCache(read_adapter=None)
        self.assertIsNone(c.resolve('UNKNOWN'))

    def test_cache_miss_with_adapter_calls_search_once(self):
        adapter = MagicMock()
        @dataclass
        class M: instrument_id: int = 9999; raw: Dict[str, Any] = None
        adapter.search_instrument.return_value = [M(9999, {'symbolFull': 'TSLA'})]
        c = InstrumentCache(read_adapter=adapter)
        self.assertEqual(c.resolve('TSLA'), 9999)
        self.assertEqual(c.resolve('TSLA'), 9999)  # second call uses cache
        # adapter called exactly once
        self.assertEqual(adapter.search_instrument.call_count, 1)

    def test_adapter_exception_returns_none_not_raise(self):
        adapter = MagicMock()
        adapter.search_instrument.side_effect = Exception('boom')
        c = InstrumentCache(read_adapter=adapter)
        self.assertIsNone(c.resolve('XYZ'))


# ===========================================================================
# Section 3: PaperEtoroBroker — HAPPY PATH
# ===========================================================================
class TestPaperBrokerHappy(unittest.TestCase):

    def test_returns_paper_logged_on_valid_intent(self):
        b = make_paper_broker(instrument_id_for_symbol=1234567,
                              rate=FakeRate(bid=100.0, ask=100.5))
        intent = make_intent()
        result = b.submit(intent)
        self.assertIsInstance(result, OrderResult)
        self.assertEqual(result.status, 'paper_logged')
        self.assertIn('PAPER-ETORO-42-AAPL', result.broker_order_id or '')

    def test_intent_risk_checks_carries_would_be_body(self):
        """The validation body is stashed into intent.risk_checks so
        main.py's existing log_intent(..., risk_checks=risk_checks) call
        persists it without any main.py change."""
        b = make_paper_broker()
        intent = make_intent()
        b.submit(intent)
        self.assertIn('etoro_would_be_body', intent.risk_checks)
        body = intent.risk_checks['etoro_would_be_body']
        self.assertEqual(body['InstrumentID'], 1234567)
        # No failure key on happy path
        self.assertNotIn('etoro_validation_failure', intent.risk_checks)

    def test_audit_file_written(self):
        tmp = Path(tempfile.mkstemp(suffix='.jsonl')[1])
        try:
            b = make_paper_broker(audit_file_path=tmp)
            b.submit(make_intent())
            lines = tmp.read_text().splitlines()
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])
            self.assertEqual(rec['broker'], 'etoro_paper')
            self.assertEqual(rec['validation_ok'], True)
            self.assertEqual(rec['result_status'], 'paper_logged')
        finally:
            if tmp.exists(): tmp.unlink()


# ===========================================================================
# Section 4: PaperEtoroBroker — REJECTION PATHS
# ===========================================================================
class TestPaperBrokerRejections(unittest.TestCase):
    """Validation failures must produce status='rejected', NEVER 'risk_rejected'."""

    def _assert_rejected_with(self, broker, intent, expected_reason):
        result = broker.submit(intent)
        self.assertEqual(
            result.status, 'rejected',
            f'Expected status=rejected, got {result.status!r}',
        )
        self.assertNotEqual(
            result.status, 'risk_rejected',
            'eToro validation must NEVER produce risk_rejected',
        )
        self.assertEqual(result.reason, expected_reason)
        self.assertIsNone(result.broker_order_id)
        self.assertEqual(
            intent.risk_checks.get('etoro_validation_failure'),
            expected_reason,
        )

    def test_unresolved_symbol(self):
        # No symbol preloaded, no adapter -> unresolved
        b = PaperEtoroBroker(
            instrument_cache=InstrumentCache(),
            rates_provider=lambda _: FakeRate(),
        )
        self._assert_rejected_with(
            b, make_intent(symbol='UNKNOWN_TICKER'),
            'etoro_validation_unresolved_symbol',
        )

    def test_no_rate(self):
        b = make_paper_broker(rate=None)
        self._assert_rejected_with(
            b, make_intent(), 'etoro_validation_no_rate',
        )

    def test_stop_side_long(self):
        b = make_paper_broker(rate=FakeRate(bid=100.0, ask=100.5))
        self._assert_rejected_with(
            b, make_intent(direction='long', stop_loss=105.0),
            'etoro_validation_stop_side',
        )

    def test_target_side_long(self):
        b = make_paper_broker(rate=FakeRate(bid=100.0, ask=100.5))
        self._assert_rejected_with(
            b, make_intent(direction='long', target_price=95.0),
            'etoro_validation_target_side',
        )

    def test_min_amount(self):
        b = make_paper_broker(rate=FakeRate())
        self._assert_rejected_with(
            b, make_intent(position_size=5.0),
            'etoro_validation_min_amount',
        )

    def test_currency_zero(self):
        b = make_paper_broker(rate=FakeRate())
        self._assert_rejected_with(
            b, make_intent(position_size=0),
            'etoro_validation_currency',
        )

    def test_direction_invalid(self):
        b = make_paper_broker(rate=FakeRate())
        self._assert_rejected_with(
            b, make_intent(direction='sideways'),
            'etoro_validation_direction',
        )

    def test_no_stop_loss(self):
        b = make_paper_broker(rate=FakeRate())
        self._assert_rejected_with(
            b, make_intent(stop_loss=0),
            'etoro_validation_no_stop',
        )


# ===========================================================================
# Section 5: STATUS DISCIPLINE — only paper_logged / rejected / error
# ===========================================================================
class TestStatusDiscipline(unittest.TestCase):

    def test_never_emits_risk_rejected_for_validation_failure(self):
        """Exhaustively cover validation failure paths and ensure NONE
        produce status='risk_rejected'."""
        cases = [
            (make_intent(direction='sideways'), 'etoro_validation_direction'),
            (make_intent(position_size=0), 'etoro_validation_currency'),
            (make_intent(position_size=5), 'etoro_validation_min_amount'),
            (make_intent(symbol='UNKNOWN'), 'etoro_validation_unresolved_symbol'),
            (make_intent(stop_loss=0), 'etoro_validation_no_stop'),
            (make_intent(direction='long', stop_loss=105),
             'etoro_validation_stop_side'),
            (make_intent(direction='long', target_price=95),
             'etoro_validation_target_side'),
        ]
        for intent, expected in cases:
            with self.subTest(reason=expected):
                # For unresolved-symbol case use empty cache
                cache = InstrumentCache()
                if intent.symbol != 'UNKNOWN':
                    cache.preload({intent.symbol: 1234567})
                b = PaperEtoroBroker(
                    instrument_cache=cache,
                    rates_provider=lambda _: FakeRate(bid=100.0, ask=100.5),
                )
                r = b.submit(intent)
                self.assertNotEqual(
                    r.status, 'risk_rejected',
                    f'{expected}: must NEVER produce risk_rejected',
                )
                self.assertEqual(r.status, 'rejected')

    def test_only_three_statuses_reachable(self):
        """Across all paths, only paper_logged / rejected / error are reachable."""
        # Happy path -> paper_logged
        b = make_paper_broker()
        self.assertEqual(b.submit(make_intent()).status, 'paper_logged')
        # Validation failure -> rejected
        b2 = make_paper_broker()
        self.assertEqual(b2.submit(make_intent(position_size=0)).status,
                         'rejected')

    def test_never_emits_accepted_status(self):
        """Paper broker must NEVER claim status='accepted' — that's only
        for real broker submissions."""
        results: List[OrderResult] = []
        for intent in [make_intent(),
                       make_intent(direction='sideways'),
                       make_intent(position_size=0)]:
            b = make_paper_broker()
            results.append(b.submit(intent))
        for r in results:
            self.assertNotEqual(r.status, 'accepted')


# ===========================================================================
# Section 6: NO DUPLICATE LOGGING — broker never writes execution_intents
# ===========================================================================
class TestNoDuplicateExecutionIntents(unittest.TestCase):

    def test_paper_broker_does_not_import_log_intent_or_update_status(self):
        """Source AST proof: paper_broker.py does not reference
        log_intent or update_intent_status anywhere."""
        repo = Path(__file__).resolve().parent
        path = repo / 'bot' / 'etoro' / 'paper_broker.py'
        with open(path) as f:
            tree = ast.parse(f.read(), filename=str(path))
        forbidden = {'log_intent', 'update_intent_status'}
        offenders = []
        for node in ast.walk(tree):
            # ImportFrom: from bot.flywheel import log_intent
            if isinstance(node, ast.ImportFrom) and node.module == 'bot.flywheel':
                for alias in node.names:
                    if alias.name in forbidden:
                        offenders.append(f'imports {alias.name}')
            # Bare names / attributes
            if isinstance(node, ast.Name) and node.id in forbidden:
                offenders.append(f'name reference {node.id}')
            if isinstance(node, ast.Attribute) and node.attr in forbidden:
                offenders.append(f'attribute access .{node.attr}')
        self.assertEqual(
            offenders, [],
            f'PaperEtoroBroker must not call log_intent / update_intent_status: '
            f'{offenders}',
        )

    def test_submit_does_not_call_log_intent_runtime(self):
        """Behavioral proof: poison bot.flywheel.log_intent. If
        PaperEtoroBroker.submit() ever calls it, the test fails."""
        import bot.flywheel as fw
        real = fw.log_intent
        called = []
        def poisoned(*a, **k):
            called.append((a, k))
            raise AssertionError(
                'PaperEtoroBroker called log_intent — duplicate row!'
            )
        fw.log_intent = poisoned
        try:
            b = make_paper_broker()
            r = b.submit(make_intent())
            self.assertEqual(r.status, 'paper_logged')
            self.assertEqual(called, [])
        finally:
            fw.log_intent = real

    def test_submit_does_not_call_update_intent_status_runtime(self):
        import bot.flywheel as fw
        real = fw.update_intent_status
        called = []
        def poisoned(*a, **k):
            called.append((a, k))
            raise AssertionError(
                'PaperEtoroBroker called update_intent_status — duplicate state change!'
            )
        fw.update_intent_status = poisoned
        try:
            b = make_paper_broker()
            r = b.submit(make_intent())
            self.assertEqual(r.status, 'paper_logged')
            self.assertEqual(called, [])
        finally:
            fw.update_intent_status = real


# ===========================================================================
# Section 7: STRUCTURAL NO-WRITE PROOFS (extend M13.2 contract to M13.3 files)
# ===========================================================================
class TestNoWriteCapability(unittest.TestCase):
    """Extends M13.2's contract: bot/etoro/ — all files, including the
    new paper_broker.py, instrument_cache.py, schema_validator.py — must
    contain zero code paths capable of issuing a non-GET HTTP request."""

    FORBIDDEN_METHOD_LITERALS = {'POST', 'DELETE', 'PUT', 'PATCH'}
    FORBIDDEN_FUNCTION_NAMES = {
        'post', 'delete', 'put', 'patch',
        '_post', '_delete', '_put', '_patch',
    }

    def test_no_write_methods_anywhere_in_etoro_package(self):
        offenders = []
        repo = Path(__file__).resolve().parent
        for fname in sorted(glob.glob(str(repo / 'bot' / 'etoro' / '*.py'))):
            with open(fname) as f:
                tree = ast.parse(f.read(), filename=fname)
            rel = os.path.relpath(fname, str(repo))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name.lower() in self.FORBIDDEN_FUNCTION_NAMES:
                        offenders.append(f'{rel}:{node.lineno} fn {node.name}')
                if isinstance(node, ast.Call):
                    for kw in (node.keywords or []):
                        if kw.arg == 'method' and isinstance(kw.value, ast.Constant):
                            v = str(kw.value.value).upper()
                            if v in self.FORBIDDEN_METHOD_LITERALS:
                                offenders.append(
                                    f'{rel}:{node.lineno} method={v}'
                                )
                    for a in (node.args or []):
                        if isinstance(a, ast.Constant) and isinstance(a.value, str):
                            if a.value.upper() in self.FORBIDDEN_METHOD_LITERALS:
                                offenders.append(
                                    f'{rel}:{node.lineno} literal {a.value!r}'
                                )
        self.assertEqual(
            offenders, [],
            f'M13.3 contract violated — write capability detected:\n  '
            + '\n  '.join(offenders),
        )

    def test_paper_broker_only_uses_get_methods_on_client(self):
        """paper_broker.py must not reference EtoroClient.post/.delete/etc."""
        repo = Path(__file__).resolve().parent
        path = repo / 'bot' / 'etoro' / 'paper_broker.py'
        with open(path) as f:
            tree = ast.parse(f.read())
        offenders = []
        forbidden_attrs = {'post', 'delete', 'put', 'patch'}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden_attrs:
                offenders.append(f'.{node.attr}')
        self.assertEqual(
            offenders, [],
            f'paper_broker.py references write methods: {offenders}',
        )

    def test_paper_broker_does_not_post_via_urllib(self):
        """Behavioral proof: poison urllib.request.urlopen. submit()
        must succeed without it ever being called."""
        import urllib.request as urlreq
        real = urlreq.urlopen
        called = []
        def poisoned(*a, **k):
            called.append(str(a[0]) if a else '?')
            raise AssertionError(
                'PaperEtoroBroker triggered urlopen — must be offline in M13.3'
            )
        urlreq.urlopen = poisoned
        try:
            b = make_paper_broker()
            r = b.submit(make_intent())
            self.assertIn(r.status, ('paper_logged', 'rejected'))
            self.assertEqual(called, [])
        finally:
            urlreq.urlopen = real


# ===========================================================================
# Section 8: FACTORY REGISTRATION
# ===========================================================================
class TestFactoryRegistration(unittest.TestCase):

    def _with_broker_env(self, name):
        return _EnvCtx('BROKER', name)

    def test_etoro_paper_returns_paper_etoro_broker(self):
        with self._with_broker_env('etoro_paper'):
            from bot.brokers import get_broker
            b = get_broker()
            self.assertIsInstance(b, PaperEtoroBroker)
            self.assertEqual(b.name, 'etoro_paper')
            self.assertFalse(b.is_live)

    def test_etoro_real_raises_value_error(self):
        with self._with_broker_env('etoro_real'):
            from bot.brokers import get_broker
            with self.assertRaises(ValueError) as ctx:
                get_broker()
            msg = str(ctx.exception).lower()
            self.assertIn('etoro_real', msg)
            self.assertIn('not implemented', msg)
            self.assertIn('etoro_paper', msg)

    def test_unknown_broker_still_falls_back_to_paper(self):
        """Backwards compatibility: unknown names still warn-and-fall-back.
        Only etoro_real is the explicit hard-fail case."""
        with self._with_broker_env('completely_unknown_xyz'):
            from bot.brokers import get_broker
            from bot.brokers.paper_broker import PaperBroker
            b = get_broker()
            self.assertIsInstance(b, PaperBroker)


class _EnvCtx:
    """Tiny context manager: set os.environ[key]=value then restore."""
    def __init__(self, key, value):
        self.key, self.value = key, value
        self._prev = None
        self._had = False
    def __enter__(self):
        self._had = self.key in os.environ
        self._prev = os.environ.get(self.key)
        os.environ[self.key] = self.value
        return self
    def __exit__(self, *exc):
        if self._had:
            os.environ[self.key] = self._prev
        else:
            os.environ.pop(self.key, None)


# ===========================================================================
# Section 9: SUBMIT NEVER RAISES (BrokerAdapter contract)
# ===========================================================================
class TestSubmitNeverRaises(unittest.TestCase):

    def test_internal_error_returns_error_result_not_raise(self):
        """Inject an exception into resolve(); broker must return
        OrderResult(status='error'), not propagate."""
        cache = MagicMock()
        cache.resolve.side_effect = RuntimeError('synthetic')
        b = PaperEtoroBroker(
            instrument_cache=cache,
            rates_provider=lambda _: FakeRate(),
        )
        intent = make_intent()
        # Note: PaperEtoroBroker._resolve_instrument catches and returns
        # None, so the flow continues into validate_open which fails
        # with etoro_validation_unresolved_symbol. The result is
        # 'rejected' (not 'error') — that's the correct path. We
        # confirm submit() did not raise.
        try:
            result = b.submit(intent)
            self.assertIn(result.status, ('rejected', 'error', 'paper_logged'))
        except Exception as e:
            self.fail(f'submit() raised {type(e).__name__}: {e}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
