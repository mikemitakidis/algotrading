"""
bot/backtest_job.py  —  Thread wrapper for dashboard integration

Keeps threading/state management completely separate from the engine.
The engine (backtest_v2.run) is a pure synchronous function.
This module adds:
  - BacktestJob: one background thread, real liveness tracking
  - get_status(): safe to call any time — no JSON files
  - start_job() / cancel_job() / reset_job()
"""

import threading
import time
import logging
from typing import Optional

log = logging.getLogger(__name__)

_LOCK      = threading.Lock()
_current   : Optional['BacktestJob'] = None


class BacktestJob:
    """Encapsulates one backtest run: thread, result, progress, cancellation."""

    def __init__(self, symbols, start_str, end_str):
        self.symbols    = symbols
        self.start_str  = start_str
        self.end_str    = end_str
        self._cancel    = threading.Event()
        self._thread    = None
        self.status     = 'pending'   # pending | running | done | error | cancelled
        self.progress   = 0
        self.msg        = 'Starting…'
        self.result     = None
        self.error      = None
        self.started_at = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self):
        self._thread    = threading.Thread(target=self._run, daemon=True)
        self.started_at = time.monotonic()
        self.status     = 'running'
        self._thread.start()

    def cancel(self):
        self._cancel.set()
        self.status  = 'cancelled'
        self.msg     = 'Cancelled by user.'
        self.progress = 0

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── Internal run ──────────────────────────────────────────────────────

    def _run(self):
        try:
            from bot.backtest_v2 import run as bt_run

            total = len(self.symbols)
            # Progress callback injected via monkey-patch on the module's logger
            # (simpler than threading through callback parameters)
            processed = [0]

            def _progress_hook(sym_idx, sym, msg_text):
                pct = int((sym_idx / total) * 95)
                self.progress = pct
                self.msg      = f'[{sym_idx+1}/{total}] {msg_text}'

            # Patch logging to capture progress messages
            import bot.backtest_v2 as bt_mod
            orig_info = bt_mod.log.info

            def _intercept_info(fmt, *args, **kwargs):
                orig_info(fmt, *args, **kwargs)
                if args and '[BT2]' in str(fmt):
                    msg = (fmt % args) if args else str(fmt)
                    sym_done = sum(1 for s in self.symbols
                                  if any(s in m for m in [msg]))
                    self.msg = msg.replace('[BT2] ', '')[:80]
            bt_mod.log.info = _intercept_info

            try:
                result = bt_run(
                    self.symbols, self.start_str, self.end_str, export=True
                )
            finally:
                bt_mod.log.info = orig_info

            if self._cancel.is_set():
                self.status = 'cancelled'
                self.msg    = 'Cancelled.'
                self.result = result
            elif result.get('status') == 'error':
                self.status = 'error'
                self.error  = result.get('error', 'Unknown engine error')
                self.msg    = f'Error: {self.error[:120]}'
                self.result = result
            else:
                self.status   = 'done'
                self.progress = 100
                total_trades  = result.get('stats', {}).get('total', 0)
                self.msg      = f"Done — {total_trades} trades"
                self.result   = result

        except Exception as e:
            log.error('[JOB] Backtest failed: %s', e, exc_info=True)
            self.status = 'error'
            self.error  = str(e)
            self.msg    = f'Error: {str(e)[:120]}'

    # ── Serialisable snapshot for /api/backtest/status ────────────────────

    def snapshot(self) -> dict:
        """Return JSON-safe status dict."""
        base = {
            'status':     self.status,
            'progress':   self.progress,
            'progress_msg': self.msg,
            'symbols':    self.symbols,
            'start_date': self.start_str,
            'end_date':   self.end_str,
        }
        if self.status in ('done', 'cancelled') and self.result:
            base['stats']               = self.result.get('stats', {})
            base['trades']              = self.result.get('trades', [])
            base['diagnostics']         = self.result.get('diagnostics', {})
            base['meta']                = self.result.get('meta', {})
            base['strategy_version']    = self.result.get('strategy_version', 1)
            base['strategy_confluence'] = self.result.get('strategy_confluence', {})
            base['benchmark']           = self.result.get('benchmark', {})
            base['outperformance_pct']  = self.result.get('outperformance_pct')
            base['report_folder']       = self.result.get('report_folder')
        if self.error:
            base['error'] = self.error
        return base


# ── Public module API ─────────────────────────────────────────────────────────

def start_job(symbols: list, start_str: str, end_str: str) -> None:
    global _current
    with _LOCK:
        if _current and _current.is_alive():
            raise RuntimeError('A backtest is already running')
        _current = BacktestJob(symbols, start_str, end_str)
        _current.start()
    log.info('[JOB] Started: %s %s→%s', symbols, start_str, end_str)


def cancel_job() -> None:
    global _current
    with _LOCK:
        if _current:
            _current.cancel()
    log.info('[JOB] Cancelled')


def reset_job() -> None:
    """Force-clear any state. Safe to call any time."""
    global _current
    with _LOCK:
        _current = None
    log.info('[JOB] Reset')


def is_running() -> bool:
    with _LOCK:
        return _current is not None and _current.is_alive()


def get_status() -> dict:
    """
    Always safe to call. Never raises.
    Returns idle state if no job exists.
    """
    with _LOCK:
        if _current is None:
            return {'status': 'idle', 'progress': 0, 'progress_msg': ''}
        return _current.snapshot()
