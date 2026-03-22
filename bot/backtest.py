"""
bot/backtest.py
Walk-forward backtesting engine.

Uses the EXACT same code path as the live bot:
  - bot.indicators.compute()        — identical indicator computation
  - bot.scanner.score_timeframe()   — identical scoring logic
  - bot.strategy.load()             — identical thresholds from strategy.json

Cancellation: uses threading.Event + run-token so cancel() immediately
stops the running thread and stale background threads cannot overwrite results.

Statuses: running | done | partial | cancelled | timeout | error
"""

import json
import logging
import random
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from bot.indicators import compute
from bot.scanner    import score_timeframe, _build_timeframes
from bot.strategy   import load as load_strategy
from bot.data       import resample_to_4h, _browser_session

log = logging.getLogger(__name__)

BASE_DIR         = Path(__file__).resolve().parent.parent
RESULTS_PATH     = BASE_DIR / 'data' / 'backtest_results.json'
HISTORY_PATH     = BASE_DIR / 'data' / 'backtest_history.json'
REPORTS_DIR      = BASE_DIR / 'data' / 'reports'
MAX_HISTORY      = 20
LOOKFORWARD_DAYS = 20
COOLDOWN_DAYS    = 3
MIN_WARMUP_BARS  = 60
HARD_TIMEOUT     = 300   # 5 minutes max per run

# ── Cancel / run-token mechanism ─────────────────────────────────────────────
_CANCEL_EVENT = threading.Event()          # set to request cancellation
_RUN_TOKEN    = {'value': None}            # UUID of current run; stale threads check this
_RUN_LOCK     = threading.Lock()           # guards start/cancel


def _new_token() -> str:
    import uuid
    return str(uuid.uuid4())


def _is_cancelled(my_token: str) -> bool:
    """Return True if this run should stop (cancelled or superseded)."""
    return _CANCEL_EVENT.is_set() or _RUN_TOKEN['value'] != my_token


# ── State file helpers ────────────────────────────────────────────────────────

def _write_results(data: dict, token: Optional[str] = None) -> None:
    """Write results atomically. If token given, skip write if run was superseded."""
    if token and _RUN_TOKEN['value'] != token:
        return   # stale run — do not overwrite
    tmp = RESULTS_PATH.with_suffix('.tmp')
    try:
        RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, default=str))
        tmp.replace(RESULTS_PATH)
    except Exception as e:
        log.warning('[BT] Results write failed: %s', e)


def read_results() -> dict:
    try:
        return json.loads(RESULTS_PATH.read_text())
    except Exception:
        return {'status': 'idle'}


def _append_history(result: dict) -> None:
    """Append compact run summary to history (last MAX_HISTORY entries)."""
    try:
        history = []
        if HISTORY_PATH.exists():
            try:
                history = json.loads(HISTORY_PATH.read_text())
            except Exception:
                history = []
        s = result.get('stats', {})
        m = result.get('meta', {})
        entry = {
            'run_at':                result.get('completed_at', ''),
            'status':                result.get('status', 'done'),
            'symbols':               result.get('symbols', []),
            'start_date':            result.get('start_date', ''),
            'end_date':              result.get('end_date', ''),
            'days_range':            m.get('days_range', 0),
            'symbols_completed':     m.get('symbols_completed', 0),
            'symbols_total':         m.get('symbols_count', 0),
            'strategy_version':      result.get('strategy_version', 1),
            'confluence_min':        m.get('confluence_min', 3),
            'total_trades':          s.get('total', 0),
            'win_rate':              s.get('win_rate', 0),
            'profit_factor':         s.get('profit_factor'),
            'max_drawdown_pct':      s.get('max_drawdown_pct', 0),
            'final_equity':          s.get('final_equity', 100),
            'annualised_return_pct': s.get('annualised_return_pct', 0),
            'tf_availability':       m.get('tf_availability', {}),
        }
        history.insert(0, entry)
        history = history[:MAX_HISTORY]
        HISTORY_PATH.write_text(json.dumps(history, default=str))
    except Exception as e:
        log.debug('[BT] History append failed: %s', e)


def read_history() -> list:
    try:
        if HISTORY_PATH.exists():
            return json.loads(HISTORY_PATH.read_text())
    except Exception:
        pass
    return []


# ── Disk cache ────────────────────────────────────────────────────────────────

def _bt_cache_path(sym: str, interval: str) -> Path:
    d = BASE_DIR / 'data' / 'bt_cache'
    d.mkdir(parents=True, exist_ok=True)
    return d / f'{sym}_{interval}.json'


def _bt_cache_load(sym: str, interval: str,
                   required_start: Optional[date] = None) -> Optional[pd.DataFrame]:
    p = _bt_cache_path(sym, interval)
    if not p.exists():
        return None
    try:
        d   = json.loads(p.read_text())
        ttl = 86400 if interval == '1d' else 14400
        if time.time() - d.get('ts', 0) > ttl:
            return None
        df = pd.DataFrame.from_dict(d['rows'], orient='index')
        df.index = pd.to_datetime(df.index, utc=True)
        df.columns = [c.lower() for c in df.columns]
        if len(df) < MIN_WARMUP_BARS:
            return None
        if required_start is not None:
            req_ts    = pd.Timestamp(required_start, tz='UTC')
            # Allow 5-day tolerance: Yahoo's earliest available intraday
            # data may be a few days later than computed fetch_start
            # due to market-open offsets and weekends.
            if df.index[0] > req_ts + pd.Timedelta(days=5):
                return None
        return df
    except Exception:
        return None


def _bt_cache_save(sym: str, interval: str, df: pd.DataFrame) -> None:
    try:
        rows = {str(k): v for k, v in df.to_dict(orient='index').items()}
        _bt_cache_path(sym, interval).write_text(
            json.dumps({'ts': time.time(), 'rows': rows}, default=str)
        )
    except Exception:
        pass


def _live_cache_load(sym: str, interval: str,
                     required_start: Optional[date] = None) -> Optional[pd.DataFrame]:
    p = BASE_DIR / 'data' / 'bar_cache' / f'{sym}_{interval}.json'
    if not p.exists():
        return None
    try:
        d  = json.loads(p.read_text())
        df = pd.DataFrame.from_dict(d['rows'], orient='index')
        df.index = pd.to_datetime(df.index, utc=True)
        df.columns = [c.lower() for c in df.columns]
        if len(df) < MIN_WARMUP_BARS:
            return None
        if required_start is not None:
            req_ts    = pd.Timestamp(required_start, tz='UTC')
            # Allow 5-day tolerance: Yahoo's earliest available intraday
            # data may be a few days later than computed fetch_start
            # due to market-open offsets and weekends.
            if df.index[0] > req_ts + pd.Timedelta(days=5):
                return None
        return df
    except Exception:
        return None


# ── Network fetch ─────────────────────────────────────────────────────────────

def _fetch_yf_single(sym: str, start: date, end: date,
                     interval: str, progress_cb=None,
                     token: Optional[str] = None) -> tuple:
    """
    Fetch one interval. Cache → live cache → network.
    Checks cancel token before every network attempt.
    Returns (df, status_str).
    """
    import yfinance as yf

    warmup_days = 120 if interval == '1d' else 60
    fetch_start = start - timedelta(days=warmup_days)
    fetch_end   = end   + timedelta(days=2)

    # Tier 1: backtest cache
    cached = _bt_cache_load(sym, interval, required_start=fetch_start)
    if cached is not None:
        first = cached.index[0].strftime('%Y-%m-%d')
        last  = cached.index[-1].strftime('%Y-%m-%d')
        if progress_cb:
            progress_cb(f'{sym} {interval}: cache — {len(cached)} bars ({first}→{last})')
        return cached, 'ok_cached'

    # Tier 2: live bot cache
    live = _live_cache_load(sym, interval, required_start=fetch_start)
    if live is not None:
        _bt_cache_save(sym, interval, live)
        first = live.index[0].strftime('%Y-%m-%d')
        last  = live.index[-1].strftime('%Y-%m-%d')
        if progress_cb:
            progress_cb(f'{sym} {interval}: live cache — {len(live)} bars ({first}→{last})')
        return live, 'ok_live_cache'

    # Tier 3: network fetch (cancel-aware)
    for attempt in range(3):
        if token and _is_cancelled(token):
            return None, 'cancelled'

        if attempt > 0:
            wait = attempt * 8
            if progress_cb:
                progress_cb(f'{sym} {interval}: rate limited — waiting {wait}s (attempt {attempt+1}/3)...')
            time.sleep(wait)
            if token and _is_cancelled(token):
                return None, 'cancelled'

        if progress_cb:
            progress_cb(f'{sym} {interval}: fetching from Yahoo ({attempt+1}/3)...')

        try:
            session = _browser_session()
            ticker  = yf.Ticker(sym, session=session)
            df = ticker.history(
                start        = fetch_start.strftime('%Y-%m-%d'),
                end          = fetch_end.strftime('%Y-%m-%d'),
                interval     = interval,
                auto_adjust  = True,
                actions      = False,
                raise_errors = False,
            )

            if df is None or df.empty:
                if progress_cb:
                    progress_cb(f'{sym} {interval}: empty response from Yahoo')
                time.sleep(2)
                return None, 'empty_response'

            df.columns = [c.lower() for c in df.columns]
            keep = [c for c in ('open', 'high', 'low', 'close', 'volume') if c in df.columns]
            if not keep:
                return None, 'missing_ohlcv_columns'
            df = df[keep].dropna()

            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index, utc=True)
            elif df.index.tz is None:
                df.index = df.index.tz_localize('UTC')
            else:
                df.index = df.index.tz_convert('UTC')

            if len(df) < MIN_WARMUP_BARS:
                time.sleep(2)
                return None, f'too_few_bars_{len(df)}'

            first = df.index[0].strftime('%Y-%m-%d')
            last  = df.index[-1].strftime('%Y-%m-%d')
            if progress_cb:
                progress_cb(f'{sym} {interval}: got {len(df)} bars ({first}→{last}) — pacing...')
            _bt_cache_save(sym, interval, df)
            wait = 8.0 + random.random() * 4.0
            time.sleep(wait)
            return df, 'ok'

        except Exception as e:
            err = str(e)
            is_rl  = any(k in err for k in ('429', 'Too Many', 'rate', 'Rate', 'TooMany'))
            is_net = any(k in err for k in ('403', 'Forbidden', 'proxy', 'Proxy', 'tunnel'))
            log.warning('[BT] %s %s attempt %d: %s', sym, interval, attempt+1, err[:80])
            if is_net:
                return None, 'network_error'
            if is_rl and attempt < 2:
                continue
            if is_rl:
                return None, 'rate_limited'
            return None, f'error:{err[:80]}'

    return None, 'rate_limited'


def _fetch_all_tfs(sym: str, start: date, end: date,
                   timeframes: list, progress_cb=None,
                   token: Optional[str] = None) -> tuple:
    result    = {}
    meta      = {}
    raw_cache = {}

    for tf_label, period, interval, do_resample in timeframes:
        if token and _is_cancelled(token):
            break
        if interval not in raw_cache:
            raw_cache[interval] = _fetch_yf_single(
                sym, start, end, interval, progress_cb, token
            )
        raw, status = raw_cache[interval]

        if raw is None:
            meta[tf_label] = {'status': status, 'bars': 0, 'first': None, 'last': None}
            continue

        df = resample_to_4h(raw) if do_resample else raw
        result[tf_label] = df
        meta[tf_label] = {
            'status': status,
            'bars':   len(df),
            'first':  df.index[0].strftime('%Y-%m-%d') if len(df) else None,
            'last':   df.index[-1].strftime('%Y-%m-%d') if len(df) else None,
        }

    return result, meta


# ── Walk-forward engine ───────────────────────────────────────────────────────

def _score_all_tfs(tf_data: dict, cutoff: pd.Timestamp,
                   direction: str, strategy: dict) -> tuple:
    scores   = {}
    best_ind = None
    for tf_label in ['1D', '4H', '1H', '15m']:
        df = tf_data.get(tf_label)
        if df is None:
            continue
        sl = df[df.index <= cutoff]
        if len(sl) < MIN_WARMUP_BARS:
            continue
        ind = compute(sl.tail(200))
        if ind is None:
            continue
        if score_timeframe(ind, direction, strategy):
            scores[tf_label] = 1
            if best_ind is None:
                best_ind = ind
    return scores, best_ind


def _evaluate_outcome(daily_df: pd.DataFrame, signal_date: pd.Timestamp,
                      direction: str, entry: float, stop: float, target: float) -> dict:
    future = daily_df[daily_df.index > signal_date].head(LOOKFORWARD_DAYS)
    for i, (idx, row) in enumerate(future.iterrows()):
        hi = float(row.get('high', 0))
        lo = float(row.get('low',  0))
        hit_target = (hi >= target) if direction == 'long' else (lo <= target)
        hit_stop   = (lo <= stop)   if direction == 'long' else (hi >= stop)
        if hit_stop:
            ret = (stop - entry) / entry * 100 if direction == 'long' \
                  else (entry - stop) / entry * 100
            return {'outcome': 'LOSS', 'bars_held': i+1,
                    'exit_price': stop, 'return_pct': round(ret, 3)}
        if hit_target:
            ret = (target - entry) / entry * 100 if direction == 'long' \
                  else (entry - target) / entry * 100
            return {'outcome': 'WIN', 'bars_held': i+1,
                    'exit_price': target, 'return_pct': round(ret, 3)}
    if len(future) > 0:
        last_close = float(future.iloc[-1].get('close', entry))
        ret = (last_close - entry) / entry * 100 * (1 if direction == 'long' else -1)
    else:
        ret = 0.0
    return {'outcome': 'TIMEOUT', 'bars_held': LOOKFORWARD_DAYS,
            'exit_price': None, 'return_pct': round(ret, 3)}


def _walk_symbol(sym: str, tf_data: dict, trading_days: list,
                 strategy: dict, diag: dict = None,
                 token: Optional[str] = None) -> list:
    if not tf_data:
        if diag is not None: diag.setdefault('no_data', []).append(sym)
        return []
    daily_df = tf_data.get('1D')
    if daily_df is None:
        if diag is not None: diag.setdefault('no_daily', []).append(sym)
        return []

    confluence = strategy.get('confluence', {})
    risk_cfg   = strategy.get('risk', {})
    routing    = strategy.get('routing', {})
    cfg_min    = int(confluence.get('min_valid_tfs', 3))
    atr_stop   = float(risk_cfg.get('atr_stop_mult', 2.0))
    atr_target = float(risk_cfg.get('atr_target_mult', 3.0))
    etoro_min  = int(routing.get('etoro_min_tfs', 4))
    ibkr_min   = int(routing.get('ibkr_min_tfs',  2))
    strat_ver  = int(strategy.get('version', 1))

    trades   = []
    cooldown = {}
    available = len(tf_data)
    if available >= 4:
        min_valid = cfg_min
    elif available >= 2:
        min_valid = max(2, cfg_min - 1)
    else:
        min_valid = 1

    for day_ts in trading_days:
        if token and _is_cancelled(token):
            break
        day = day_ts.date()
        if daily_df[daily_df.index.date == day].empty:
            continue
        for direction in ('long', 'short'):
            last = cooldown.get(direction)
            if last and (day - last).days < COOLDOWN_DAYS:
                continue
            cutoff = pd.Timestamp(day_ts)
            scores, best_ind = _score_all_tfs(tf_data, cutoff, direction, strategy)
            count = len(scores)
            if count < min_valid or best_ind is None:
                if diag is not None:
                    reason = 'no_indicators' if best_ind is None \
                             else f'only_{count}_of_{min_valid}_tfs'
                    diag.setdefault('rejected', {}).setdefault(reason, 0)
                    diag['rejected'][reason] += 1
                continue
            if diag is not None:
                diag['candidates'] = diag.get('candidates', 0) + 1

            route = 'ETORO' if count >= etoro_min else \
                    'IBKR'  if count >= ibkr_min  else 'WATCH'
            entry  = best_ind['price']
            atr    = best_ind['atr']
            if direction == 'long':
                stop_loss    = round(entry - atr_stop   * atr, 4)
                target_price = round(entry + atr_target * atr, 4)
            else:
                stop_loss    = round(entry + atr_stop   * atr, 4)
                target_price = round(entry - atr_target * atr, 4)

            outcome = _evaluate_outcome(
                daily_df, cutoff, direction, entry, stop_loss, target_price
            )
            trade = {
                'symbol':           sym,
                'date':             day.isoformat(),
                'direction':        direction,
                'route':            route,
                'valid_count':      count,
                'tfs_triggered':    list(scores.keys()),
                'entry_price':      round(entry, 4),
                'stop_loss':        stop_loss,
                'target_price':     target_price,
                'rsi':              round(best_ind['rsi'], 2),
                'macd_hist':        round(best_ind['macd_hist'], 6),
                'atr':              round(atr, 4),
                'bb_pos':           round(best_ind['bb_pos'], 4),
                'vwap_dev':         round(best_ind['vwap_dev'], 4),
                'vol_ratio':        round(best_ind['vol_ratio'], 3),
                'strategy_version': strat_ver,
                **outcome,
            }
            trades.append(trade)
            cooldown[direction] = day

    return trades


# ── Statistics ────────────────────────────────────────────────────────────────

def _compute_stats(trades: list, start_str: str = '', end_str: str = '') -> dict:
    if not trades:
        return {'total': 0}

    wins     = [t for t in trades if t['outcome'] == 'WIN']
    losses   = [t for t in trades if t['outcome'] == 'LOSS']
    timeouts = [t for t in trades if t['outcome'] == 'TIMEOUT']
    total    = len(trades)
    n_win    = len(wins)
    n_loss   = len(losses)
    n_to     = len(timeouts)
    win_rate = round(n_win / total * 100, 1) if total else 0

    all_rets = [t['return_pct'] for t in trades]
    win_rets = [t['return_pct'] for t in wins]
    los_rets = [t['return_pct'] for t in losses]
    avg_ret  = round(float(np.mean(all_rets)), 3) if all_rets else 0
    avg_win  = round(float(np.mean(win_rets)), 3) if win_rets else 0
    avg_los  = round(float(np.mean(los_rets)), 3) if los_rets else 0

    gross_profit  = sum(r for r in all_rets if r > 0)
    gross_loss    = abs(sum(r for r in all_rets if r < 0))
    profit_factor = round(gross_profit / gross_loss, 3) if gross_loss > 0 else None

    # Equity curve + drawdown
    equity = 100.0; peak = equity; drawdown = 0.0; eq_curve = []
    eq_with_dates = []
    for t in sorted(trades, key=lambda x: x['date']):
        equity *= (1 + t['return_pct'] / 100)
        eq_curve.append(round(equity, 4))
        eq_with_dates.append({'d': t['date'], 'e': round(equity, 4)})
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak * 100)
    final_equity = round(eq_curve[-1], 2) if eq_curve else 100.0

    # Annualised return
    try:
        days_range = max((date.fromisoformat(end_str) - date.fromisoformat(start_str)).days, 1)
    except Exception:
        days_range = 365
    total_ret = (final_equity / 100.0) - 1.0
    ann_ret   = round(((1 + total_ret) ** (365.0 / days_range) - 1) * 100, 2)

    # Hold period
    bars_held = [t.get('bars_held', 0) for t in trades if t.get('bars_held')]
    avg_hold  = round(float(np.mean(bars_held)), 1) if bars_held else 0
    max_hold  = max(bars_held) if bars_held else 0

    # Consecutive streaks
    max_cw = max_cl = cur_w = cur_l = 0
    for t in sorted(trades, key=lambda x: x['date']):
        if t['outcome'] == 'WIN':
            cur_w += 1; cur_l = 0; max_cw = max(max_cw, cur_w)
        elif t['outcome'] == 'LOSS':
            cur_l += 1; cur_w = 0; max_cl = max(max_cl, cur_l)
        else:
            cur_w = cur_l = 0

    # By confluence
    by_conf = {}
    for t in trades:
        k = str(t['valid_count']) + '/4'
        by_conf.setdefault(k, {'total': 0, 'wins': 0})
        by_conf[k]['total'] += 1
        if t['outcome'] == 'WIN': by_conf[k]['wins'] += 1
    for k in by_conf:
        n = by_conf[k]['total']
        by_conf[k]['win_rate'] = round(by_conf[k]['wins'] / n * 100, 1) if n else 0

    # By direction
    by_dir = {}
    for t in trades:
        d = t['direction']
        by_dir.setdefault(d, {'total': 0, 'wins': 0, 'rets': []})
        by_dir[d]['total'] += 1; by_dir[d]['rets'].append(t['return_pct'])
        if t['outcome'] == 'WIN': by_dir[d]['wins'] += 1
    for d in by_dir:
        n = by_dir[d]['total']
        by_dir[d]['win_rate'] = round(by_dir[d]['wins'] / n * 100, 1) if n else 0
        by_dir[d]['avg_ret']  = round(float(np.mean(by_dir[d]['rets'])), 3) if by_dir[d]['rets'] else 0
        del by_dir[d]['rets']

    # By route
    by_route = {}
    for t in trades:
        r = t['route']
        by_route.setdefault(r, {'total': 0, 'wins': 0})
        by_route[r]['total'] += 1
        if t['outcome'] == 'WIN': by_route[r]['wins'] += 1
    for r in by_route:
        n = by_route[r]['total']
        by_route[r]['win_rate'] = round(by_route[r]['wins'] / n * 100, 1) if n else 0

    # By timeframe participation
    by_tf = {}
    for tf in ['1D', '4H', '1H', '15m']:
        trades_with_tf = [t for t in trades if tf in t.get('tfs_triggered', [])]
        if not trades_with_tf:
            continue
        wins_tf = [t for t in trades_with_tf if t['outcome'] == 'WIN']
        by_tf[tf] = {
            'trades':   len(trades_with_tf),
            'wins':     len(wins_tf),
            'win_rate': round(len(wins_tf) / len(trades_with_tf) * 100, 1),
            'avg_ret':  round(float(np.mean([t['return_pct'] for t in trades_with_tf])), 3),
        }

    # By TF combination
    by_tf_combo = {}
    for t in trades:
        combo = '+'.join(sorted(t.get('tfs_triggered', [])))
        by_tf_combo.setdefault(combo, {'total': 0, 'wins': 0, 'rets': []})
        by_tf_combo[combo]['total'] += 1
        by_tf_combo[combo]['rets'].append(t['return_pct'])
        if t['outcome'] == 'WIN': by_tf_combo[combo]['wins'] += 1
    for k in by_tf_combo:
        n = by_tf_combo[k]['total']
        by_tf_combo[k]['win_rate'] = round(by_tf_combo[k]['wins'] / n * 100, 1) if n else 0
        by_tf_combo[k]['avg_ret']  = round(float(np.mean(by_tf_combo[k]['rets'])), 3)
        del by_tf_combo[k]['rets']

    # Monthly
    by_month = {}
    for t in trades:
        m = t['date'][:7]
        by_month.setdefault(m, {'total': 0, 'wins': 0, 'rets': 0.0})
        by_month[m]['total'] += 1; by_month[m]['rets'] += t['return_pct']
        if t['outcome'] == 'WIN': by_month[m]['wins'] += 1
    for m in by_month:
        n = by_month[m]['total']
        by_month[m]['win_rate'] = round(by_month[m]['wins'] / n * 100, 1) if n else 0
        by_month[m]['avg_ret']  = round(by_month[m]['rets'] / n, 3) if n else 0
        del by_month[m]['rets']

    # Per symbol
    by_sym = {}
    for t in trades:
        s = t['symbol']
        by_sym.setdefault(s, {'total': 0, 'wins': 0, 'rets': []})
        by_sym[s]['total'] += 1; by_sym[s]['rets'].append(t['return_pct'])
        if t['outcome'] == 'WIN': by_sym[s]['wins'] += 1
    for s in by_sym:
        n = by_sym[s]['total']
        by_sym[s]['win_rate'] = round(by_sym[s]['wins'] / n * 100, 1) if n else 0
        by_sym[s]['avg_ret']  = round(float(np.mean(by_sym[s]['rets'])), 3)
        del by_sym[s]['rets']

    return {
        'total':                 total,
        'wins':                  n_win,
        'losses':                n_loss,
        'timeouts':              n_to,
        'win_rate':              win_rate,
        'avg_return_pct':        avg_ret,
        'avg_win_pct':           avg_win,
        'avg_loss_pct':          avg_los,
        'profit_factor':         profit_factor,
        'max_drawdown_pct':      round(drawdown, 2),
        'final_equity':          final_equity,
        'annualised_return_pct': ann_ret,
        'avg_hold_days':         avg_hold,
        'max_hold_days':         max_hold,
        'max_consec_wins':       max_cw,
        'max_consec_losses':     max_cl,
        'by_confluence':         by_conf,
        'by_direction':          by_dir,
        'by_route':              by_route,
        'by_timeframe':          by_tf,
        'by_tf_combo':           by_tf_combo,
        'by_month':              by_month,
        'by_symbol':             by_sym,
        'equity_curve':          eq_curve[-100:],
        'equity_with_dates':     eq_with_dates[-100:],
    }


# ── Benchmark ─────────────────────────────────────────────────────────────────

def _fetch_benchmark(start_str: str, end_str: str, symbols: list) -> dict:
    import yfinance as yf
    bm_sym = symbols[0] if len(symbols) == 1 else 'SPY'
    try:
        start = date.fromisoformat(start_str)
        end   = date.fromisoformat(end_str)
        session = _browser_session()
        df = yf.Ticker(bm_sym, session=session).history(
            start       = start.strftime('%Y-%m-%d'),
            end         = (end + timedelta(days=2)).strftime('%Y-%m-%d'),
            interval    = '1d',
            auto_adjust = True, actions=False, raise_errors=False,
        )
        if df is None or df.empty:
            return {}
        df.columns = [c.lower() for c in df.columns]
        if 'close' not in df.columns:
            return {}
        closes = df['close'].dropna()
        closes.index = pd.to_datetime(closes.index, utc=True) \
            if closes.index.tz is None else closes.index.tz_convert('UTC')
        closes = closes[(closes.index.date >= start) & (closes.index.date <= end)]
        if len(closes) < 2:
            return {}
        sp = float(closes.iloc[0]); ep = float(closes.iloc[-1])
        total_ret = (ep / sp - 1.0) * 100
        days_range = max((end - start).days, 1)
        ann_ret = round(((1 + total_ret/100) ** (365.0/days_range) - 1) * 100, 2)
        eq_curve = []; peak_eq = 100.0; max_dd = 0.0
        for price in closes:
            eq = round(float(price) / sp * 100, 4)
            eq_curve.append(eq)
            peak_eq = max(peak_eq, eq)
            max_dd  = max(max_dd, (peak_eq - eq) / peak_eq * 100)
        eq_dates = []
        for dt, price in closes.items():
            eq_dates.append({'d': dt.strftime('%Y-%m-%d'), 'e': round(float(price)/sp*100, 4)})
        return {
            'symbol':            bm_sym,
            'label':             bm_sym + ' buy-and-hold',
            'return_pct':        round(total_ret, 3),
            'annualised_pct':    ann_ret,
            'max_drawdown_pct':  round(max_dd, 2),
            'final_equity':      round(eq_dates[-1]['e'], 2) if eq_dates else 100.0,
            'equity_with_dates': eq_dates,
        }
    except Exception as e:
        log.debug('[BT] Benchmark fetch failed: %s', e)
        return {}


# ── Report export ─────────────────────────────────────────────────────────────

def _export_report(result: dict) -> Optional[Path]:
    """Write a human-readable text report to data/reports/TIMESTAMP/. Returns path."""
    try:
        ts        = datetime.now().strftime('%Y%m%d_%H%M%S')
        syms      = '_'.join(result.get('symbols', ['unknown'])[:3])
        folder    = REPORTS_DIR / f'{ts}_{syms}'
        folder.mkdir(parents=True, exist_ok=True)

        s     = result.get('stats', {})
        m     = result.get('meta', {})
        bm    = result.get('benchmark', {})
        status = result.get('status', 'done')

        lines = [
            '=' * 65,
            'ALGO TRADER — BACKTEST REPORT',
            '=' * 65,
            f"Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Status    : {status.upper()}",
            '',
            '── RUN PARAMETERS ──────────────────────────────────────────',
            f"Symbols   : {', '.join(result.get('symbols', []))}",
            f"Period    : {result.get('start_date','')} → {result.get('end_date','')} ({m.get('days_range',0)} days)",
            f"Strategy  : v{result.get('strategy_version',1)}  |  Confluence ≥{m.get('confluence_min',3)}/4 TFs",
            f"TFs used  : {', '.join(m.get('timeframes_configured', []))}",
            f"Data src  : {m.get('data_source','yfinance')}",
            '',
        ]

        if status in ('partial', 'cancelled', 'timeout'):
            comp = m.get('symbols_completed', 0)
            tot  = m.get('symbols_count', 0)
            lines += [
                f"⚠  PARTIAL RESULTS — {comp}/{tot} symbols completed",
                f"   Results below cover only {comp} symbol(s).",
                '',
            ]

        if not s.get('total'):
            lines += ['No trades generated.', '']
        else:
            lines += [
                '── PERFORMANCE SUMMARY ──────────────────────────────────────',
                f"Total trades   : {s.get('total',0)}",
                f"Win / Loss / TO: {s.get('wins',0)} / {s.get('losses',0)} / {s.get('timeouts',0)}",
                f"Win rate       : {s.get('win_rate',0)}%",
                f"Profit factor  : {s.get('profit_factor','n/a')}",
                f"Avg return     : {s.get('avg_return_pct',0)}%",
                f"Avg win        : {s.get('avg_win_pct',0)}%",
                f"Avg loss       : {s.get('avg_loss_pct',0)}%",
                f"Max drawdown   : {s.get('max_drawdown_pct',0)}%",
                f"Final equity   : {s.get('final_equity',100)} (start=100)",
                f"Annualised ret : {s.get('annualised_return_pct',0)}%",
                f"Avg hold       : {s.get('avg_hold_days',0)} days",
                f"Max consec W/L : {s.get('max_consec_wins',0)} / {s.get('max_consec_losses',0)}",
                '',
            ]

            if bm.get('symbol'):
                op = result.get('outperformance_pct', 0)
                sign = '+' if op >= 0 else ''
                lines += [
                    '── BENCHMARK COMPARISON ─────────────────────────────────────',
                    f"Benchmark      : {bm['symbol']} buy-and-hold",
                    f"Strat ann.ret  : {s.get('annualised_return_pct',0)}%",
                    f"Bench ann.ret  : {bm.get('annualised_pct',0)}%",
                    f"Outperformance : {sign}{op}% annualised",
                    f"Bench drawdown : {bm.get('max_drawdown_pct',0)}%",
                    '',
                ]

            by_tf = s.get('by_timeframe', {})
            if by_tf:
                lines.append('── BY TIMEFRAME ─────────────────────────────────────────────')
                for tf in ['1D', '4H', '1H', '15m']:
                    if tf not in by_tf: continue
                    v = by_tf[tf]
                    lines.append(
                        f"  {tf:<5}: {v['trades']:>3} trades  WR:{v['win_rate']:>5.1f}%  "
                        f"AvgRet:{v['avg_ret']:>+7.3f}%"
                    )
                lines.append('')

            by_combo = s.get('by_tf_combo', {})
            if by_combo:
                lines.append('── BY TF COMBINATION ────────────────────────────────────────')
                for combo, v in sorted(by_combo.items(), key=lambda x: -x[1]['trades']):
                    lines.append(
                        f"  {combo:<20}: {v['trades']:>3} trades  WR:{v['win_rate']:>5.1f}%  "
                        f"AvgRet:{v['avg_ret']:>+7.3f}%"
                    )
                lines.append('')

            by_month = s.get('by_month', {})
            if by_month:
                lines.append('── MONTHLY BREAKDOWN ────────────────────────────────────────')
                for month in sorted(by_month):
                    v = by_month[month]
                    lines.append(
                        f"  {month}:  {v['total']:>3} trades  WR:{v['win_rate']:>5.1f}%  "
                        f"AvgRet:{v['avg_ret']:>+7.3f}%"
                    )
                lines.append('')

            lines.append('── TRADE LIST ───────────────────────────────────────────────')
            header = f"{'Date':<12} {'Symbol':<8} {'Dir':<6} {'Route':<7} {'TFs':<12} " \
                     f"{'Entry':>8} {'Stop':>8} {'Target':>8} {'RSI':>6} {'Out':<8} {'Ret':>8}"
            lines.append(header)
            lines.append('-' * 95)
            for t in result.get('trades', []):
                tfs = '+'.join(t.get('tfs_triggered', []))
                lines.append(
                    f"{t['date']:<12} {t['symbol']:<8} {t['direction']:<6} {t['route']:<7} "
                    f"{tfs:<12} {t['entry_price']:>8.2f} {t['stop_loss']:>8.2f} "
                    f"{t['target_price']:>8.2f} {t['rsi']:>6.1f} {t['outcome']:<8} "
                    f"{t['return_pct']:>+7.3f}%"
                )

        lines += ['', '=' * 65, 'END OF REPORT', '=' * 65]

        report_txt  = folder / 'report.txt'
        results_json = folder / 'results.json'
        trades_csv  = folder / 'trades.csv'

        report_txt.write_text('\n'.join(lines))

        # Full JSON
        results_json.write_text(json.dumps(result, default=str, indent=2))

        # CSV
        trades = result.get('trades', [])
        if trades:
            keys = ['date','symbol','direction','route','valid_count','tfs_triggered',
                    'entry_price','stop_loss','target_price','outcome','return_pct',
                    'bars_held','rsi','macd_hist','atr','bb_pos','vwap_dev','vol_ratio',
                    'strategy_version']
            import csv, io
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=keys, extrasaction='ignore')
            w.writeheader()
            for t in trades:
                row = dict(t)
                row['tfs_triggered'] = '+'.join(t.get('tfs_triggered', []))
                w.writerow(row)
            trades_csv.write_text(buf.getvalue())

        log.info('[BT] Report saved to %s', folder)
        return folder

    except Exception as e:
        log.warning('[BT] Report export failed: %s', e)
        return None


# ── Main run function ─────────────────────────────────────────────────────────

def run_backtest(symbols: list, start_str: str, end_str: str,
                 strategy: Optional[dict] = None,
                 my_token: Optional[str] = None,
                 skip_benchmark: bool = False) -> None:
    """
    Walk-forward backtest. Writes progress to backtest_results.json.
    my_token: run identifier — stale threads skip writes if superseded.
    """
    if strategy is None:
        strategy = load_strategy()

    _write_results({
        'status':     'running',
        'started_at': datetime.now(timezone.utc).isoformat(),
        'symbols':    symbols,
        'start_date': start_str,
        'end_date':   end_str,
        'progress':   0,
        'progress_msg': 'Starting...',
        'strategy_version': strategy.get('version', 1),
    }, my_token)

    all_trades      = []
    diag_per_sym    = {}
    symbols_done    = 0
    final_status    = 'done'
    stop_reason     = None

    try:
        start = date.fromisoformat(start_str)
        end   = date.fromisoformat(end_str)
        if start >= end:
            raise ValueError('start_date must be before end_date')
        if (end - start).days > 730:
            raise ValueError('Date range cannot exceed 730 days (1H data limit)')

        timeframes  = _build_timeframes(strategy)
        total_syms  = len(symbols)
        run_started = time.monotonic()

        for i, sym in enumerate(symbols):
            # Cancel check
            if _is_cancelled(my_token):
                stop_reason  = 'cancelled'
                final_status = 'cancelled'
                break

            # Hard timeout
            elapsed = time.monotonic() - run_started
            if elapsed > HARD_TIMEOUT:
                stop_reason  = f'timeout after {int(elapsed)}s'
                final_status = 'timeout'
                log.warning('[BT] Hard timeout — processed %d/%d symbols', i, total_syms)
                break

            pct = int((i / total_syms) * 90)

            def _progress(msg, _i=i, _n=total_syms, _pct=pct, _tok=my_token):
                if _tok and _RUN_TOKEN['value'] != _tok:
                    return
                _write_results({
                    'status':       'running',
                    'progress':     _pct,
                    'progress_msg': f'[{_i+1}/{_n}] {msg}',
                    'symbols':      symbols,
                    'start_date':   start_str,
                    'end_date':     end_str,
                    'strategy_version': strategy.get('version', 1),
                }, _tok)

            _write_results({
                'status':       'running',
                'progress':     pct,
                'progress_msg': f'[{i+1}/{total_syms}] {sym}: checking cache...',
                'symbols':      symbols,
                'start_date':   start_str,
                'end_date':     end_str,
                'strategy_version': strategy.get('version', 1),
            }, my_token)

            tf_data, fetch_statuses = _fetch_all_tfs(
                sym, start, end, timeframes, _progress, my_token
            )

            # Check cancel again after potentially long fetch
            if _is_cancelled(my_token):
                stop_reason = 'cancelled'; final_status = 'cancelled'
                break

            if not tf_data:
                reasons = ', '.join(
                    f'{k}:{v.get("status","?")}' for k,v in fetch_statuses.items()
                )
                diag_per_sym[sym] = {
                    'tf_coverage':  {k: v.get('bars',0) for k,v in fetch_statuses.items()},
                    'fetch_status': {k: v.get('status','?') for k,v in fetch_statuses.items()},
                    'tf_first': {}, 'tf_last': {}, 'candidates': 0,
                    'rejected': {}, 'fetch_error': reasons,
                }
                symbols_done += 1
                continue

            daily_df = tf_data.get('1D')
            if daily_df is None:
                symbols_done += 1
                continue

            trading_days = daily_df[
                (daily_df.index.date >= start) & (daily_df.index.date <= end)
            ].index.tolist()

            sym_diag = {
                'tf_coverage': {}, 'fetch_status': {},
                'tf_first': {}, 'tf_last': {}, 'candidates': 0, 'rejected': {},
            }
            for tf_label, m in fetch_statuses.items():
                sym_diag['tf_coverage'][tf_label]  = m.get('bars', 0)
                sym_diag['fetch_status'][tf_label] = m.get('status', 'unknown')
                if m.get('first'): sym_diag['tf_first'][tf_label] = m['first']
                if m.get('last'):  sym_diag['tf_last'][tf_label]  = m['last']

            sym_trades = _walk_symbol(
                sym, tf_data, trading_days, strategy, sym_diag, my_token
            )
            all_trades.extend(sym_trades)
            diag_per_sym[sym] = sym_diag
            symbols_done += 1
            log.info('[BT] %s: %d signals', sym, len(sym_trades))

        # Determine final status
        if final_status == 'done' and symbols_done < len(symbols):
            final_status = 'partial'

        all_trades.sort(key=lambda t: t['date'])
        stats = _compute_stats(all_trades, start_str, end_str)

        run_ts     = datetime.now(timezone.utc).isoformat()
        days_range = (end - start).days
        tf_summary = {}
        for sym_d in diag_per_sym.values():
            for tf, bars in sym_d.get('tf_coverage', {}).items():
                tf_summary.setdefault(tf, {'syms_ok': 0, 'syms_total': 0, 'max_bars': 0})
                tf_summary[tf]['syms_total'] += 1
                if bars > 0:
                    tf_summary[tf]['syms_ok'] += 1
                    tf_summary[tf]['max_bars'] = max(tf_summary[tf]['max_bars'], bars)

        progress_msg = f'Done — {stats.get("total",0)} trades'
        if final_status == 'cancelled':
            progress_msg = f'Cancelled — {symbols_done}/{len(symbols)} symbols, {stats.get("total",0)} trades'
        elif final_status == 'timeout':
            progress_msg = f'Timeout — {symbols_done}/{len(symbols)} symbols, {stats.get("total",0)} trades'
        elif final_status == 'partial':
            progress_msg = f'Partial — {symbols_done}/{len(symbols)} symbols, {stats.get("total",0)} trades'

        result = {
            'status':           final_status,
            'completed_at':     run_ts,
            'stop_reason':      stop_reason,
            'meta': {
                'symbols':               symbols,
                'start_date':            start_str,
                'end_date':              end_str,
                'days_range':            days_range,
                'run_timestamp':         run_ts,
                'data_source':           'yfinance (Yahoo Finance)',
                'strategy_version':      strategy.get('version', 1),
                'strategy_updated':      strategy.get('updated_at'),
                'confluence_min':        strategy.get('confluence', {}).get('min_valid_tfs', 3),
                'timeframes_configured': [tf[0] for tf in timeframes],
                'tf_availability':       tf_summary,
                'symbols_count':         len(symbols),
                'symbols_completed':     symbols_done,
                'symbols_with_data':     sum(
                    1 for d in diag_per_sym.values()
                    if any(v > 0 for v in d.get('tf_coverage', {}).values())
                ),
            },
            'symbols':             symbols,
            'start_date':          start_str,
            'end_date':            end_str,
            'strategy_version':    strategy.get('version', 1),
            'strategy_confluence': strategy.get('confluence', {}),
            'trades':              all_trades,
            'stats':               stats,
            'diagnostics':         diag_per_sym,
            'progress':            100,
            'progress_msg':        progress_msg,
        }
        _write_results(result, my_token)

        if not _is_cancelled(my_token) and not skip_benchmark:
            try:
                benchmark = _fetch_benchmark(start_str, end_str, symbols)
                result['benchmark'] = benchmark
                if benchmark:
                    strat_ret = stats.get('annualised_return_pct', 0) or 0
                    bm_ret    = benchmark.get('annualised_pct', 0) or 0
                    result['outperformance_pct'] = round(strat_ret - bm_ret, 2)
                _write_results(result, my_token)
            except Exception:
                pass

        _append_history(result)
        _export_report(result)

    except Exception as e:
        log.error('[BT] Failed: %s', e, exc_info=True)
        _write_results({
            'status':  'error',
            'error':   str(e),
            'symbols': symbols,
            'start_date': start_str,
            'end_date':   end_str,
            'progress_msg': f'Error: {str(e)[:120]}',
        }, my_token)


# ── Public API ────────────────────────────────────────────────────────────────

def cancel_backtest() -> None:
    """Signal the running backtest to stop cleanly."""
    with _RUN_LOCK:
        _CANCEL_EVENT.set()
        # Invalidate the current run token so stale threads stop writing
        _RUN_TOKEN['value'] = None
    # Write cancelled state immediately so dashboard updates
    _write_results({'status': 'cancelled', 'progress': 0,
                    'progress_msg': 'Cancelled by user.'})
    log.info('[BT] Cancel requested')


def start_backtest(symbols: list, start_str: str, end_str: str,
                   skip_benchmark: bool = False) -> None:
    """Launch backtest in a background daemon thread."""
    with _RUN_LOCK:
        _CANCEL_EVENT.clear()
        token = _new_token()
        _RUN_TOKEN['value'] = token

    strategy = load_strategy()
    t = threading.Thread(
        target=run_backtest,
        args=(symbols, start_str, end_str, strategy, token, skip_benchmark),
        daemon=True,
    )
    t.start()
