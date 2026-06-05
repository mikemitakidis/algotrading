"""
M13.3 — PaperEtoroBroker (dry-run, no real eToro writes).

Implements BrokerAdapter. Validates intents against the eToro schema
(per docs/M13_1_order_schema_mapping.md) and returns OrderResult with
status='paper_logged' on success or status='rejected' on schema
validation failure.

Hard contract (M13.3):
  * NEVER calls a write HTTP method (no POST/DELETE/PUT/PATCH).
  * Only EtoroClient.get is reachable (transitively, via
    EtoroReadAdapter.search_instrument / get_rates if used).
  * NEVER calls log_intent() or update_intent_status().
    main.py is the SOLE writer to execution_intents — this broker
    returns OrderResult only, identical to the existing PaperBroker /
    IBKRBroker pattern. No duplicate rows.
  * Schema validation failures are BROKER/PAYLOAD failures, not
    portfolio risk. Returns status='rejected' with
    reason='etoro_validation_<rule>'. NEVER status='risk_rejected'.
  * Audit JSONL at data/paper_etoro_orders.jsonl is supplemental
    (mirrors existing PaperBroker pattern). NOT a duplicate of
    execution_intents.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from bot.brokers.base import BrokerAdapter, OrderIntent, OrderResult
from bot.etoro.instrument_cache import InstrumentCache
from bot.etoro.schema_validator import ValidationResult, validate_open

log = logging.getLogger(__name__)

_DEFAULT_AUDIT_FILE = (
    Path(__file__).resolve().parent.parent.parent
    / 'data' / 'paper_etoro_orders.jsonl'
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PaperEtoroBroker(BrokerAdapter):
    """eToro paper/dry-run broker. Validates, never writes."""

    def __init__(
        self,
        read_adapter=None,
        instrument_cache: Optional[InstrumentCache] = None,
        rates_provider: Optional[Callable[[int], Any]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        audit_file_path: Optional[Path] = None,
        min_amount_usd: float = 10.0,
    ) -> None:
        self._read_adapter = read_adapter
        self._instrument_cache = instrument_cache or InstrumentCache(read_adapter)
        self._rates_provider = rates_provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._audit_file_path = audit_file_path or _DEFAULT_AUDIT_FILE
        self._min_amount_usd = float(min_amount_usd)

    @property
    def name(self) -> str:
        return 'etoro_paper'

    @property
    def is_live(self) -> bool:
        return False

    # ---- BrokerAdapter contract ----

    def submit(self, intent: OrderIntent) -> OrderResult:
        """Validate intent against eToro schema and return paper result.

        NEVER calls log_intent / update_intent_status.
        NEVER calls a write HTTP method.
        """
        # P0-3 (audit, 2026-06-05): runtime M13.4A kill-switch
        # enforcement at submit time. Re-checks the broker-allocation
        # policy via TTL-cached read so operator dashboard toggles
        # take effect without scanner restart. Fail-safe per audit
        # Correction A: no cached policy + DB unavailable → signal-
        # only skip (never trade on unknown policy state).
        from bot.runtime_policy import get_signal_only_reason
        _skip, _reason = get_signal_only_reason(self.name)
        if _skip:
            log.info('[ETORO-PAPER] runtime policy says skip: reason=%s',
                       _reason)
            return OrderResult(
                intent=intent,
                status='signal_only_skipped',
                broker_order_id=None,
                reason=_reason,
                submitted_at=datetime.now(timezone.utc).isoformat(),
            )

        try:
            # 1. Resolve symbol → instrumentId (cache, or read_adapter GET)
            instrument_id = self._resolve_instrument(intent.symbol)
            # 2. Fetch current rate for side checks
            current_rate = self._fetch_rate(instrument_id) if instrument_id else None
            # 3. Validate against the eToro schema rules
            v: ValidationResult = validate_open(
                intent=intent,
                instrument_id=instrument_id,
                current_rate=current_rate,
                min_amount_usd=self._min_amount_usd,
            )
            # 4. Stash validation outcome in intent.risk_checks for audit.
            #    main.py will persist this JSON via the existing
            #    log_intent(..., risk_checks=risk_checks) call site.
            self._stash_audit(intent, v)
            # 5. Build result
            if v.ok:
                result = OrderResult(
                    intent=intent,
                    status='paper_logged',
                    broker_order_id=(
                        f'PAPER-ETORO-{intent.signal_id}-{intent.symbol}'
                    ),
                    reason='M13.3 dry-run — eToro schema validated; no write',
                    submitted_at=self._clock().isoformat(),
                )
            else:
                result = OrderResult(
                    intent=intent,
                    status='rejected',
                    broker_order_id=None,
                    reason=v.rejection_reason,
                    submitted_at=self._clock().isoformat(),
                )
            self._write_audit(intent, v, result)
            log.info(
                '[PAPER-ETORO] %s %s | status=%s reason=%s',
                intent.symbol, intent.direction, result.status, result.reason,
            )
            return result
        except Exception as e:
            # BrokerAdapter contract: submit must never raise.
            log.exception('[PAPER-ETORO] submit error (returning OrderResult)')
            # Still no duplicate logging — main.py owns execution_intents.
            return OrderResult(
                intent=intent,
                status='error',
                broker_order_id=None,
                reason=f'paper_etoro_internal_error:{type(e).__name__}',
                submitted_at=self._clock().isoformat(),
            )

    # ---- internals ----

    def _resolve_instrument(self, symbol: str) -> Optional[int]:
        try:
            return self._instrument_cache.resolve(symbol)
        except Exception as e:
            log.warning('[PAPER-ETORO] resolve failed for %s: %s', symbol, e)
            return None

    def _fetch_rate(self, instrument_id: int):
        # Explicit provider takes precedence (used by tests).
        if self._rates_provider is not None:
            try:
                return self._rates_provider(instrument_id)
            except Exception as e:
                log.warning('[PAPER-ETORO] rates_provider failed: %s', e)
                return None
        if self._read_adapter is None:
            return None
        try:
            rates = self._read_adapter.get_rates([instrument_id])
        except Exception as e:
            log.warning('[PAPER-ETORO] read_adapter.get_rates failed: %s', e)
            return None
        return rates[0] if rates else None

    @staticmethod
    def _stash_audit(intent: OrderIntent, v: ValidationResult) -> None:
        """Mutate intent.risk_checks so the existing main.py log_intent
        call captures the eToro validation detail. No new column, no new
        schema, no duplicate row."""
        if not isinstance(intent.risk_checks, dict):
            intent.risk_checks = {}
        intent.risk_checks['etoro_would_be_body'] = v.would_be_body
        if not v.ok:
            intent.risk_checks['etoro_validation_failure'] = v.rejection_reason

    def _write_audit(self, intent: OrderIntent, v: ValidationResult,
                     result: OrderResult) -> None:
        """Supplemental JSONL audit. Mirrors existing PaperBroker pattern.
        NOT a duplicate of execution_intents — different file, different
        schema, different purpose (broker-level diagnostics)."""
        try:
            self._audit_file_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                'ts':              result.submitted_at,
                'broker':          self.name,
                'signal_id':       intent.signal_id,
                'symbol':          intent.symbol,
                'direction':       intent.direction,
                'route':           intent.route,
                'position_size':   intent.position_size,
                'risk_usd':        intent.risk_usd,
                'validation_ok':   v.ok,
                'rejection_reason': v.rejection_reason,
                'would_be_body':   v.would_be_body,
                'result_status':   result.status,
                'broker_order_id': result.broker_order_id,
            }
            with open(self._audit_file_path, 'a') as f:
                f.write(json.dumps(record, default=str) + '\n')
        except Exception as e:
            # Audit failure must NEVER block the broker contract.
            log.warning('[PAPER-ETORO] audit write failed: %s', e)
