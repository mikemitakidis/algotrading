#!/usr/bin/env python3
"""main.py - Algo Trader v1"""
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR   = Path(__file__).resolve().parent
LOG_PATH   = BASE_DIR / 'logs' / 'bot.log'
STATE_PATH = BASE_DIR / 'data' / 'bot_state.json'

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

root = logging.getLogger()
if not root.handlers:
    root.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    fh  = logging.FileHandler(str(LOG_PATH))
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stdout.isatty():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)

log = logging.getLogger(__name__)

import warnings
warnings.filterwarnings('ignore', category=FutureWarning)

from bot.config   import load
from bot.focus    import FOCUS_SYMBOLS
from bot.universe.active_selection import get_scan_ready_symbols  # M20.UE (flag-gated)
from bot.runtime.paper_loop import run_paper_loop  # M20.I (flag-gated, simulation-only)
from bot.database import init_db, insert_signal, init_features_table, insert_signal_features
from bot.flywheel  import (init_flywheel_tables, log_candidate, log_intent, recent_intents,
                            update_intent_status, get_daily_state, get_persistent_state,
                            write_portfolio_snapshot)
from bot.brokers   import get_broker, get_broker_name
from bot.brokers.base import OrderIntent
from bot.risk       import RiskManager, PortfolioRiskPolicy, PortfolioRiskContext
from bot.portfolio_ctx import gather as _portfolio_ctx_gather
from bot.scanner  import scan_cycle
from bot.notifier import (alert_startup, alert_stopped,
                          alert_crash, alert_cycle_summary, alert_signal,
                          send_gateway_alert)
from bot.gateway_watchdog import GatewayWatchdog, WatchdogConfig
from bot.recovery_executor import RecoveryController, RecoveryExecutor
from bot.heartbeat import Heartbeat
import bot.flywheel as _flywheel_mod


# ---------------------------------------------------------------------------
# State file helpers
# ---------------------------------------------------------------------------

def _read_existing_state() -> dict:
    """Read existing state file so we can preserve last-cycle fields on restart."""
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def write_state(state: dict) -> None:
    """Atomically write bot state JSON so dashboard never reads a partial file."""
    tmp = STATE_PATH.with_suffix('.tmp')
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, default=str))
        tmp.replace(STATE_PATH)
    except Exception as exc:
        log.debug('[STATE] write failed: %s', exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info('=' * 55)
    log.info('ALGO TRADER v1 -- SHADOW MODE -- STARTING')
    log.info('=' * 55)

    prev = _read_existing_state()

    try:
        config = load()
        log.info('[STARTUP] Config loaded.')
        log.info('[STARTUP] Mode: %s | Scan: %ds | Focus: %d',
                 config['bot_mode'], config['scan_interval_secs'], config['focus_size'])
    except Exception as e:
        log.error('[STARTUP] Config error: %s', e)
        write_state({'phase': 'crashed', 'error': str(e)})
        sys.exit(1)

    (BASE_DIR / 'data').mkdir(parents=True, exist_ok=True)

    # ── Live trading startup hard-stop ───────────────────────────────────
    # If BROKER=ibkr_live, ALL safety config must be present before
    # the bot is allowed to start. This is a hard sys.exit(1), not a warning.
    _broker_name = config.get('broker', 'paper').lower().strip()
    if _broker_name == 'ibkr_live':
        from bot.brokers.ibkr_broker import _check_live_safety_config
        _safe, _reason = _check_live_safety_config()
        if not _safe:
            log.error('=' * 60)
            log.error('[STARTUP] LIVE TRADING STARTUP REFUSED')
            log.error('[STARTUP] Safety config incomplete: %s', _reason)
            log.error('[STARTUP] Fix .env and restart. Bot will NOT start.')
            log.error('=' * 60)
            write_state({'phase': 'refused', 'reason': f'live_safety: {_reason}'})
            sys.exit(1)
        log.info('[STARTUP] *** LIVE TRADING MODE — REAL MONEY ***')
        log.info('[STARTUP] Live safety config: OK')

    conn = init_db(config['db_path'])
    init_features_table(conn)
    init_flywheel_tables(conn)
    from bot.kill_switch import ensure_default_state as _ks_init
    _ks_init()
    risk_mgr  = RiskManager()
    port_risk = PortfolioRiskPolicy()
    broker    = get_broker()
    log.info('[STARTUP] Broker: %s | Risk: max_pos=%s max_open=%d portfolio=$%.0f',
             broker.name, risk_mgr.max_position_pct, risk_mgr.max_open, risk_mgr.portfolio_size)

    # ── M15.1 Gateway watchdog (single in-process timed prober) ──────────
    # Owned by main.py. Runs on its own thread independent of scan cycle.
    # Recovery is INERT in M15.1 (Option B): no real systemctl restart.
    _wd_broker_mode = 'live' if 'live' in broker.name else 'paper'
    _wd_cfg = WatchdogConfig.from_env(
        broker_mode=_wd_broker_mode,
        host=config.get('ibkr_host', '127.0.0.1'),
        port=int(config.get('ibkr_port', 4002)),
        systemd_unit=os.getenv('IBKR_SYSTEMD_UNIT', 'ibgateway'),
    )
    _recovery_ctrl = RecoveryController(
        mode=_wd_cfg.mode,
        min_restart_interval_min=_wd_cfg.min_restart_interval_min,
        max_restarts_per_hour=_wd_cfg.max_restarts_per_hour,
    )
    _recovery_exec = RecoveryExecutor(mode=_wd_cfg.mode,
                                      systemd_unit=_wd_cfg.systemd_unit)

    def _gw_alert_adapter(severity, text, payload):
        send_gateway_alert(config, severity, text, payload)

    gateway_watchdog = GatewayWatchdog(
        config=_wd_cfg, flywheel=_flywheel_mod,
        notifier_send_fn=_gw_alert_adapter,
        recovery_controller=_recovery_ctrl,
        recovery_executor=_recovery_exec,
    )
    if 'ibkr' in broker.name:
        gateway_watchdog.start()
    else:
        log.info('[GW-WATCHDOG] not started (broker=%s, IBKR-only)', broker.name)

    # ── M20.UE: symbol-selection seam (flag-gated, default OFF) ──
    # Default behaviour is unchanged: the curated FOCUS_SYMBOLS list. When
    # USE_REGISTRY_UNIVERSE is truthy, source bare tickers from the universe
    # registry's scan_ready=true records instead. Falls back to FOCUS_SYMBOLS
    # if the registry yields nothing (never scan an empty universe).
    _use_registry = os.getenv('USE_REGISTRY_UNIVERSE', '').strip().lower() \
        in ('1', 'true', 'yes', 'on')
    if _use_registry:
        try:
            _registry_syms = get_scan_ready_symbols()
        except Exception as _e:  # noqa: BLE001 — never let selection abort startup
            _registry_syms = []
            log.warning('[STARTUP] registry universe selection failed (%s); '
                        'falling back to FOCUS_SYMBOLS', _e)
        if _registry_syms:
            focus = _registry_syms[:config['focus_size']]
            log.info('[STARTUP] Universe source: registry scan_ready '
                     '(%d symbols available)', len(_registry_syms))
        else:
            focus = FOCUS_SYMBOLS[:config['focus_size']]
            log.info('[STARTUP] Universe source: FOCUS_SYMBOLS '
                     '(registry empty/unavailable)')
    else:
        focus = FOCUS_SYMBOLS[:config['focus_size']]
    log.info('[STARTUP] Focus: %d curated large-cap symbols (no Tier A ranking in V1)', len(focus))
    log.info('[STARTUP] Data: Yahoo Finance | 1 symbol/request | 8-12s delay | browser session | disk cache')
    log.info('[STARTUP] First cycle uses disk cache where available. Fresh fetches: 8-12s each.')

    if config['telegram_enabled']:
        log.info('[STARTUP] Telegram: ENABLED')
    else:
        log.info('[STARTUP] Telegram: disabled')

    alert_startup(config)

    # ── M20.I: runtime paper loop (flag-gated, default OFF, simulation-only) ──
    # When PAPER_LOOP_ENABLED is truthy, each cycle's scanner signals are also
    # run through the simulation-only paper loop (M19 scoring -> paper routing
    # -> paper engine). No live trading, no broker calls, no execution_intents.
    _paper_loop_enabled = os.getenv('PAPER_LOOP_ENABLED', '').strip().lower() \
        in ('1', 'true', 'yes', 'on')
    _paper_account = None
    if _paper_loop_enabled:
        try:
            from bot.paper import new_account as _new_paper_account
            _pa_equity = float(os.getenv('PAPER_START_EQUITY', '100000'))
            _pa_res = _new_paper_account(
                starting_equity=_pa_equity,
                as_of_utc=datetime.now(timezone.utc).isoformat())
            _paper_account = _pa_res.account_state if _pa_res.ok else None
            log.info('[STARTUP] Paper loop: ENABLED (sim-only, equity=%.0f)',
                     _pa_equity)
        except Exception as _pe:  # noqa: BLE001
            _paper_account = None
            log.warning('[STARTUP] Paper loop init failed (%s); disabled', _pe)
    else:
        log.info('[STARTUP] Paper loop: disabled')

    uptime_started = datetime.now(timezone.utc).isoformat()
    scan_interval  = config['scan_interval_secs']

    # ── M15.2 Heartbeat (independent of scan cycle, daemon thread) ──────
    # Writes data/heartbeat.json every HEARTBEAT_INTERVAL_SEC (default 45s)
    # so /api/health can detect process death/wedge even between scans.
    # Started unconditionally — heartbeat is about bot liveness, not
    # broker mode. Failures here must not block the trading loop.
    heartbeat = Heartbeat(scan_interval_sec=scan_interval)
    heartbeat.start()

    write_state({
        'phase':               'starting',
        'mode':                config['bot_mode'],
        'cycle':               0,
        'focus_count':         len(focus),
        'scan_interval_secs':  scan_interval,
        'uptime_started':      uptime_started,
        'last_cycle_at':       prev.get('last_cycle_at'),
        'last_cycle_signals':  prev.get('last_cycle_signals'),
        'last_cycle_tfs':      prev.get('last_cycle_tfs'),
        'last_cycle_tfs_list': prev.get('last_cycle_tfs_list', []),
        'last_cycle_symbols':  prev.get('last_cycle_symbols'),
        'last_cycle_duration_s': prev.get('last_cycle_duration_s'),
        'next_cycle_at':       None,
    })

    cycle = 0

    try:
        while True:
            cycle += 1
            now_iso = datetime.now(timezone.utc).isoformat()
            log.info('[MAIN] === Cycle %d starting | %d symbols ===', cycle, len(focus))
            heartbeat.record_scan_started()

            write_state({
                'phase':               'scanning',
                'mode':                config['bot_mode'],
                'cycle':               cycle,
                'focus_count':         len(focus),
                'scan_interval_secs':  scan_interval,
                'uptime_started':      uptime_started,
                'last_cycle_at':       prev.get('last_cycle_at'),
                'last_cycle_signals':  prev.get('last_cycle_signals'),
                'last_cycle_tfs':      prev.get('last_cycle_tfs'),
                'last_cycle_tfs_list': prev.get('last_cycle_tfs_list', []),
                'last_cycle_symbols':  prev.get('last_cycle_symbols'),
                'last_cycle_duration_s': prev.get('last_cycle_duration_s'),
                'next_cycle_at':       None,
                'scan_started_at':     now_iso,
            })

            cycle_start = time.monotonic()
            signals, meta = scan_cycle(focus, config, conn=conn, cycle_id=cycle)
            cycle_duration = round(time.monotonic() - cycle_start)

            # ── M20.I: feed this cycle's signals through the paper loop ──
            # Simulation-only; advances the in-memory paper account. Guarded so
            # a paper-loop error can never disrupt the scan/insert path.
            if _paper_loop_enabled and _paper_account is not None and signals:
                try:
                    _pl = run_paper_loop(
                        signals, _paper_account,
                        evaluated_at_utc=datetime.now(timezone.utc).isoformat())
                    _paper_account = _pl.account
                    log.info('[PAPER-LOOP] cycle %d: %d in, %d routed, %d opened, '
                             '%d skipped', cycle, _pl.signals_in,
                             _pl.routed_count, _pl.opened_count,
                             _pl.skipped_ineligible)
                except Exception as _ple:  # noqa: BLE001
                    log.warning('[PAPER-LOOP] cycle %d failed (%s)', cycle, _ple)

            inserted = 0
            for signal in signals:
                ml_feats = signal.pop('_ml_features', {})
                signal.pop('_candidate_stage', None)  # strip scanner tag
                row_id = insert_signal(conn, signal)
                if row_id:
                    inserted += 1
                    insert_signal_features(conn, row_id, signal, ml_feats)
                    # Log final_signal to flywheel
                    log_candidate(
                        conn, cycle, signal.get('symbol',''), signal.get('direction',''),
                        stage='final_signal',
                        valid_count=signal.get('valid_count', 0),
                        tfs_passing=[k for k in ('1D','4H','1H','15m')
                                     if signal.get(f'tf_{k.lower()}', 0)],
                        available_tfs=meta.get('tfs_available', 0),
                        min_valid=0,
                        route=signal.get('route',''),
                        strategy_version=signal.get('strategy_version', 1),
                        signal_id=row_id,
                        ind={k: signal.get(k) for k in ('rsi','macd_hist','atr','bb_pos','vwap_dev','vol_ratio')},
                    )
                    # Risk check + execution intent
                    intent = OrderIntent(
                        signal_id=row_id,
                        symbol=signal.get('symbol',''),
                        direction=signal.get('direction',''),
                        route=signal.get('route',''),
                        entry_price=signal.get('entry_price', 0.0),
                        stop_loss=signal.get('stop_loss', 0.0),
                        target_price=signal.get('target_price', 0.0),
                        valid_count=signal.get('valid_count', 0),
                        strategy_version=signal.get('strategy_version', 1),
                    )
                    risk_passed, risk_checks, risk_reason = risk_mgr.evaluate(intent)
                    intent.risk_checks = risk_checks

                    # M14: portfolio risk gate
                    if risk_passed:
                        # P0-4 (audit, 2026-06-05): populate the
                        # positions / open_orders / local_open_intents
                        # / kill_switch_active fields the dataclass
                        # declares. Previously left at empty defaults,
                        # which made PortfolioRiskPolicy's exposure +
                        # open-trade-count gates run blind.
                        #
                        # gather() reuses the live-mode reconcile dict
                        # that RiskManager.evaluate() just stashed at
                        # checks['_recon'] — no second IBKR round-trip
                        # (audit Correction B). For paper / eToro
                        # paper / IBKR-paper-no-recon, gather()
                        # derives positions from accepted local
                        # execution_intents instead.
                        _ctx_extra = _portfolio_ctx_gather(
                            broker.name, intent, conn,
                        )
                        _ctx = PortfolioRiskContext(
                            broker=broker.name,
                            mode='live' if 'live' in broker.name else 'paper',
                            portfolio_value=risk_mgr.portfolio_size,
                            portfolio_value_source='config',
                            sector_map=port_risk.sector_map,
                            daily_state=get_daily_state(conn),
                            persistent_state=get_persistent_state(conn),
                            positions=_ctx_extra['positions'],
                            open_orders=_ctx_extra['open_orders'],
                            local_open_intents=_ctx_extra['local_open_intents'],
                            kill_switch_active=_ctx_extra['kill_switch_active'],
                        )
                        p_passed, p_checks, p_reason = port_risk.evaluate(intent, _ctx)
                        if not p_passed:
                            risk_passed = False
                            risk_checks.update(p_checks)
                            risk_reason = p_reason
                            intent.risk_checks = risk_checks
                        else:
                            risk_checks.update(p_checks)
                            intent.risk_checks = risk_checks

                    if risk_passed:
                        # ── M15.1 broker-readiness gate (gateway watchdog) ──
                        # AFTER risk passes, BEFORE broker.submit(): block
                        # submission when watchdog reports gateway unhealthy.
                        # Order preserved: risk → watchdog → existing _gateway_available
                        # TCP probe (defense in depth, lives inside broker.submit).
                        # This is broker/infrastructure readiness, NOT portfolio risk —
                        # so it is here in main.py and NOT in RiskManager.evaluate().
                        if 'ibkr' in broker.name and not gateway_watchdog.is_healthy_for_submission():
                            _gw_health = gateway_watchdog.gateway_health_payload()
                            _gw_checks = dict(risk_checks or {})
                            _gw_checks['gateway_health'] = _gw_health
                            intent_id = log_intent(conn, row_id,
                                signal.get('symbol',''), signal.get('direction',''),
                                signal.get('route',''),
                                signal.get('entry_price',0), signal.get('stop_loss',0),
                                signal.get('target_price',0),
                                intent.position_size, intent.risk_usd,
                                signal.get('valid_count',0), signal.get('strategy_version',1),
                                broker.name, 'broker_unready',
                                rejection_reason='gateway_unhealthy_block',
                                risk_checks=_gw_checks)
                            if intent_id:
                                update_intent_status(
                                    conn, intent_id, 'broker_unready',
                                    event='broker_unready:gateway_unhealthy_block'
                                )
                            log.warning(
                                '[GW-WATCHDOG] submission blocked: %s state=%s',
                                signal.get('symbol',''),
                                _gw_health.get('watchdog_status'),
                            )
                            alert_signal(config, signal)
                            continue

                        result = broker.submit(intent)
                        intent_id = log_intent(conn, row_id,
                            signal.get('symbol',''), signal.get('direction',''),
                            signal.get('route',''),
                            signal.get('entry_price',0), signal.get('stop_loss',0),
                            signal.get('target_price',0),
                            intent.position_size, intent.risk_usd,
                            signal.get('valid_count',0), signal.get('strategy_version',1),
                            broker.name, result.status,
                            broker_order_id=result.broker_order_id,
                            risk_checks=risk_checks)
                        # Wire lifecycle: preserve truthful broker status
                        # Do NOT overwrite with generic 'error' — statuses like
                        # broker_rejected, kill_switch_active, live_safety_blocked,
                        # connection_failed, account_mismatch must be preserved
                        if intent_id:
                            update_intent_status(
                                conn, intent_id, result.status,
                                event=f'broker_response:{result.status}'
                            )
                    else:
                        intent_id = log_intent(conn, row_id,
                            signal.get('symbol',''), signal.get('direction',''),
                            signal.get('route',''),
                            signal.get('entry_price',0), signal.get('stop_loss',0),
                            signal.get('target_price',0),
                            intent.position_size, intent.risk_usd,
                            signal.get('valid_count',0), signal.get('strategy_version',1),
                            broker.name, 'risk_rejected',
                            rejection_reason=risk_reason,
                            risk_checks=risk_checks)
                        if intent_id:
                            update_intent_status(
                                conn, intent_id, 'risk_rejected',
                                event=f'risk_rejected:{risk_reason}'
                            )
                    alert_signal(config, signal)

            if inserted:
                log.info('[MAIN] DB: %d signals inserted', inserted)

            alert_cycle_summary(config, cycle, len(signals), len(focus))

            # M14: portfolio risk snapshot every cycle
            try:
                _snap_ctx = PortfolioRiskContext(
                    broker=broker.name,
                    mode='live' if 'live' in broker.name else 'paper',
                    portfolio_value=risk_mgr.portfolio_size,
                    portfolio_value_source='config',
                    sector_map=port_risk.sector_map,
                    daily_state=get_daily_state(conn),
                    persistent_state=get_persistent_state(conn),
                )
                write_portfolio_snapshot(conn, cycle, broker.name, _snap_ctx)
            except Exception as _snap_err:
                log.warning('[M14] Snapshot write failed: %s', _snap_err)

            # ── M15.1: cached watchdog state log (READ-ONLY, no probe, no alert) ──
            # Single prober is the GatewayWatchdog thread. The duplicate
            # cycle-time _gateway_available probe + Telegram _send call have
            # been removed (M15.1 architecture: one prober, one alerter).
            # broker.submit() still runs its own _gateway_available TCP probe
            # as final defense-in-depth.
            if 'ibkr' in broker.name and gateway_watchdog._thread is not None:
                _gw = gateway_watchdog.current_state()
                log.info(
                    '[GW-WATCHDOG] cycle %d cached state=%s '
                    'service=%s tcp=%s api=%s probe_age=%ss',
                    cycle, _gw.get('state'),
                    _gw.get('service_running'), _gw.get('tcp_ok'),
                    _gw.get('api_ok'), _gw.get('probe_age_seconds'),
                )

            last_cycle_at = datetime.now(timezone.utc).isoformat()
            next_cycle_at = datetime.fromtimestamp(
                time.time() + scan_interval, tz=timezone.utc
            ).isoformat()

            log.info('[MAIN] === Cycle %d complete | Signals: %d | Next in %ds ===',
                     cycle, len(signals), scan_interval)
            heartbeat.record_scan_completed()

            state = {
                'phase':               'cooldown',
                'mode':                config['bot_mode'],
                'cycle':               cycle,
                'focus_count':         len(focus),
                'scan_interval_secs':  scan_interval,
                'uptime_started':      uptime_started,
                'last_cycle_at':       last_cycle_at,
                'last_cycle_signals':  len(signals),
                'last_cycle_tfs':      meta.get('tfs_available', 0),
                'last_cycle_tfs_list': meta.get('tfs_list', []),
                'last_cycle_symbols':  meta.get('symbols_scanned', len(focus)),
                'last_cycle_duration_s': cycle_duration,
                'next_cycle_at':       next_cycle_at,
            }
            write_state(state)
            prev = state

            time.sleep(scan_interval)

    except KeyboardInterrupt:
        log.info('[MAIN] Stopped by user.')
        alert_stopped(config, 'Clean shutdown')
        write_state({**prev, 'phase': 'stopped'})

    except Exception as e:
        log.error('[MAIN] Unhandled crash: %s', e, exc_info=True)
        alert_crash(config, str(e))
        write_state({**prev, 'phase': 'crashed', 'error': str(e)})
        raise


if __name__ == '__main__':
    main()
