"""
bot/backtest.py
Walk-forward backtesting engine.

Uses the EXACT same code path as the live bot:
  - bot.indicators.compute()        — identical indicator computation
  - bot.scanner.score_timeframe()   — identical scoring logic
  - bot.strategy.load()             — identical thresholds from strategy.json

No separate strategy code. One source of truth.

Walk-forward logic:
  For each trading day D in [start, end]:
    For each symbol:
      For each enabled TF: slice historical data to bars ending at D
      Compute indicators on trailing window (same as live)
      Score each TF (same as live)
      Check confluence (same threshold as live)
      If signal: record entry/stop/target, evaluate outcome vs future bars

Outcome evaluation (daily resolution):
  Look forward up to LOOKFORWARD_DAYS daily bars.
  LONG  WIN:  any future bar has high  >= target
  LONG  LOSS: any future bar has low   <= stop
  SHORT WIN:  any future bar has low   <= target
  SHORT LOSS: any future bar has high  >= stop
  TIMEOUT: neither hit within LOOKFORWARD_DAYS

Limitations vs live bot:
  - 1H data: yfinance limits to ~730 days history
  - 15m data: yfinance limits to ~60 days history
  - 4H: resampled from 1H (same as live)
  - Outcome uses daily OHLC only (good enough for swing signals)
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

BASE_DIR        = Path(__file__).resolve().parent.parent
RESULTS_PATH    = BASE_DIR / 'data' / 'backtest_results.json'
HISTORY_PATH    = BASE_DIR / 'data' / 'backtest_history.json'
MAX_HISTORY     = 20    # keep last N runs
LOOKFORWARD_DAYS = 20     # max bars to look forward when evaluating outcome
COOLDOWN_DAYS    = 3      # min days between signals on same symbol+direction
MIN_WARMUP_BARS  = 60     # bars needed before computing indicators


# ─────────────────────────────────────────────────────────────────────────────
# State file helpers
# ─────────────────────────────────────────────────────────────────────────────

def _write_results(data: dict):
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
    """Append a completed run summary to backtest_history.json (last MAX_HISTORY runs)."""
    try:
        history = []
        if HISTORY_PATH.exists():
            try:
                history = json.loads(HISTORY_PATH.read_text())
            except Exception:
                history = []
        # Store compact summary only (not full trade list)
        s = result.get('stats', {})
        m = result.get('meta', {})
        entry = {
            'run_at':           result.get('completed_at', ''),
            'symbols':          result.get('symbols', []),
            'start_date':       result.get('start_date', ''),
            'end_date':         result.get('end_date', ''),
            'days_range':       m.get('days_range', 0),
            'strategy_version': result.get('strategy_version', 1),
            'confluence_min':   m.get('confluence_min', 3),
            'total_trades':     s.get('total', 0),
            'win_rate':         s.get('win_rate', 0),
            'profit_factor':    s.get('profit_factor'),
            'max_drawdown_pct': s.get('max_drawdown_pct', 0),
            'final_equity':     s.get('final_equity', 100),
            'annualised_return_pct': s.get('annualised_return_pct', 0),
            'tf_availability':  m.get('tf_availability', {}),
            'symbols_with_data': m.get('symbols_with_data', 0),
        }
        history.insert(0, entry)
        history = history[:MAX_HISTORY]
        HISTORY_PATH.write_text(json.dumps(history, default=str))
    except Exception as e:
        log.debug('[BT] History append failed: %s', e)


def read_history() -> list:
    """Return last MAX_HISTORY run summaries, newest first."""
    try:
        if HISTORY_PATH.exists():
            return json.loads(HISTORY_PATH.read_text())
    except Exception:
        pass
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Data fetching (direct yfinance — no per-symbol delays needed for backtest)
# ─────────────────────────────────────────────────────────────────────────────

def _bt_cache_path(sym: str, interval: str) -> Path:
    """Backtest-specific disk cache — separate from live bot cache."""
    d = BASE_DIR / 'data' / 'bt_cache'
    d.mkdir(parents=True, exist_ok=True)
    return d / f'{sym}_{interval}.json'


def _bt_cache_load(sym: str, interval: str,
                   required_start: Optional[date] = None) -> Optional[pd.DataFrame]:
    """
    Load from backtest cache. 24h TTL for daily, 4h for intraday.
    required_start: earliest date the data must cover (with warmup included).
    Returns None if cache exists but doesn't reach back far enough.
    """
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
        # Reject if data doesn't cover far enough back for this backtest range
        if required_start is not None:
            req_ts = pd.Timestamp(required_start, tz='UTC')
            if df.index[0] > req_ts:
                log.debug('[BT] %s %s: bt_cache too recent (%s, need %s)',
                          sym, interval, df.index[0].date(), required_start)
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
    """
    Reuse the live bot's existing bar cache (data/bar_cache/).
    The live bot stores SHORT windows (3mo daily, 1mo hourly, 5d 15m).
    Accepts only if data covers required_start — otherwise falls through
    to a full network fetch with the correct date range.
    """
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
            req_ts = pd.Timestamp(required_start, tz='UTC')
            if df.index[0] > req_ts:
                log.debug('[BT] %s %s: live_cache too recent (%s, need %s) — will fetch full range',
                          sym, interval, df.index[0].date(), required_start)
                return None
        return df
    except Exception:
        return None


def _fetch_yf_single(sym: str, start: date, end: date,
                     interval: str, progress_cb=None) -> tuple:
    """
    Fetch one interval for one symbol.
    Order: bt_cache -> live bot cache -> network (paced, max 2 retries).

    progress_cb: optional callable(msg: str) to update UI during fetch.
    Returns (df, status_str).
    """
    import yfinance as yf

    # Compute how far back we need data (backtest start minus warmup bars)
    warmup_days = 120 if interval == '1d' else 60
    fetch_start = start - timedelta(days=warmup_days)

    # ── Tier 1: backtest cache (only if it covers the full range) ─────────
    cached = _bt_cache_load(sym, interval, required_start=fetch_start)
    if cached is not None:
        first = cached.index[0].strftime('%Y-%m-%d')
        last  = cached.index[-1].strftime('%Y-%m-%d')
        if progress_cb:
            progress_cb(f'{sym} {interval}: cache hit — {len(cached)} bars ({first}→{last})')
        log.info('[BT] %s %s: bt_cache hit  %d bars  %s->%s', sym, interval, len(cached), first, last)
        return cached, 'ok_cached'

    # ── Tier 2: live bot cache (only if it covers the full range) ─────────
    live = _live_cache_load(sym, interval, required_start=fetch_start)
    if live is not None:
        _bt_cache_save(sym, interval, live)
        first = live.index[0].strftime('%Y-%m-%d')
        last  = live.index[-1].strftime('%Y-%m-%d')
        if progress_cb:
            progress_cb(f'{sym} {interval}: live cache — {len(live)} bars ({first}→{last})')
        log.info('[BT] %s %s: live_cache  %d bars  %s->%s', sym, interval, len(live), first, last)
        return live, 'ok_live_cache'

    # ── Tier 3: network fetch — full date range, max 2 retries ───────────
    fetch_end   = end   + timedelta(days=2)

    for attempt in range(3):   # initial + 2 retries
        if attempt > 0:
            wait = attempt * 8   # 8s, then 16s — short, not 30/60/120
            if progress_cb:
                progress_cb(f'{sym} {interval}: rate limited — waiting {wait}s (attempt {attempt+1}/3)...')
            log.warning('[BT] %s %s: rate limited — waiting %ds (attempt %d/3)',
                        sym, interval, wait, attempt + 1)
            time.sleep(wait)

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
                log.warning('[BT] %s %s: empty_response', sym, interval)
                # Small pace on empty — do NOT do the full 8-12s sleep
                time.sleep(2)
                return None, 'empty_response'

            df.columns = [c.lower() for c in df.columns]
            keep = [c for c in ('open', 'high', 'low', 'close', 'volume')
                    if c in df.columns]
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
                log.warning('[BT] %s %s: only %d bars', sym, interval, len(df))
                time.sleep(2)
                return None, f'too_few_bars_{len(df)}'

            first = df.index[0].strftime('%Y-%m-%d')
            last  = df.index[-1].strftime('%Y-%m-%d')
            if progress_cb:
                progress_cb(f'{sym} {interval}: got {len(df)} bars ({first}→{last}) — pacing...')
            log.info('[BT] %s %s: fetched %d bars  %s -> %s',
                     sym, interval, len(df), first, last)

            _bt_cache_save(sym, interval, df)

            # Pace after successful network fetch — same as live bot
            wait = 8.0 + random.random() * 4.0
            time.sleep(wait)
            return df, 'ok'

        except Exception as e:
            err = str(e)
            is_rl = any(k in err for k in
                        ('429', 'Too Many', 'rate', 'Rate', 'TooMany'))
            is_net = any(k in err for k in
                         ('403', 'Forbidden', 'proxy', 'Proxy', 'tunnel'))
            log.warning('[BT] %s %s attempt %d: %s', sym, interval, attempt+1, err[:80])
            if is_net:
                if progress_cb:
                    progress_cb(f'{sym} {interval}: network error — {err[:60]}')
                return None, 'network_error'
            if is_rl and attempt < 2:
                continue   # retry with wait at top of loop
            if is_rl:
                if progress_cb:
                    progress_cb(f'{sym} {interval}: rate limited after 3 attempts — skipping')
                return None, 'rate_limited'
            if progress_cb:
                progress_cb(f'{sym} {interval}: error — {err[:60]}')
            return None, f'error:{err[:80]}'

    return None, 'rate_limited'


def _fetch_all_tfs(sym: str, start: date, end: date,
                   timeframes: list, progress_cb=None) -> tuple:
    """
    Fetch all enabled timeframes for one symbol.
    4H and 1H share interval '1h' — fetched ONCE, 4H resampled.
    Returns (result_dict, fetch_meta).
    """
    result    = {}
    meta      = {}
    raw_cache = {}   # interval -> (df, status)

    for tf_label, period, interval, do_resample in timeframes:
        if interval not in raw_cache:
            raw_cache[interval] = _fetch_yf_single(
                sym, start, end, interval, progress_cb
            )
        raw, status = raw_cache[interval]

        if raw is None:
            meta[tf_label] = {'status': status, 'bars': 0,
                              'first': None, 'last': None}
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



# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward engine
# ─────────────────────────────────────────────────────────────────────────────

def _score_all_tfs(tf_data: dict, cutoff: pd.Timestamp,
                   direction: str, strategy: dict) -> dict:
    """
    For each TF, slice data to cutoff, compute indicators, score.
    Returns dict of tf_label -> 1 for passing TFs.
    Also returns best_ind (from highest-priority passing TF).
    """
    scores   = {}
    best_ind = None
    priority = ['1D', '4H', '1H', '15m']

    for tf_label in priority:
        df = tf_data.get(tf_label)
        if df is None:
            continue

        # Strict walk-forward: only bars up to and including cutoff
        sl = df[df.index <= cutoff]
        if len(sl) < MIN_WARMUP_BARS:
            continue

        # Use trailing window — same as live (compute uses last bar)
        ind = compute(sl.tail(200))
        if ind is None:
            continue

        if score_timeframe(ind, direction, strategy):
            scores[tf_label] = 1
            if best_ind is None:
                best_ind = ind

    return scores, best_ind


def _evaluate_outcome(daily_df: pd.DataFrame,
                      signal_date: pd.Timestamp,
                      direction: str,
                      entry: float, stop: float, target: float) -> dict:
    """
    Look at up to LOOKFORWARD_DAYS daily bars after signal_date.
    Returns outcome dict.
    """
    future = daily_df[daily_df.index > signal_date].head(LOOKFORWARD_DAYS)

    for i, (idx, row) in enumerate(future.iterrows()):
        hi = float(row.get('high', 0))
        lo = float(row.get('low',  0))

        if direction == 'long':
            hit_target = hi >= target
            hit_stop   = lo <= stop
        else:
            hit_target = lo <= target
            hit_stop   = hi >= stop

        # If both triggered on same bar, conservative: stop wins
        if hit_stop:
            # Long LOSS: price fell to stop (-)
            # Short LOSS: price rose to stop — (entry-stop)/entry is negative
            if direction == 'long':
                ret_pct = round((stop - entry) / entry * 100, 3)
            else:
                ret_pct = round((entry - stop) / entry * 100, 3)
            return {'outcome': 'LOSS', 'bars_held': i + 1,
                    'exit_price': stop, 'return_pct': ret_pct}
        if hit_target:
            # Long WIN: price rose to target (+)
            # Short WIN: price fell to target — (entry-target)/entry is positive
            if direction == 'long':
                ret_pct = round((target - entry) / entry * 100, 3)
            else:
                ret_pct = round((entry - target) / entry * 100, 3)
            return {'outcome': 'WIN', 'bars_held': i + 1,
                    'exit_price': target, 'return_pct': ret_pct}

    # Neither hit
    if len(future) > 0:
        last_close = float(future.iloc[-1].get('close', entry))
        ret_pct = round((last_close - entry) / entry * 100 *
                        (1 if direction == 'long' else -1), 3)
    else:
        ret_pct = 0.0
    return {'outcome': 'TIMEOUT', 'bars_held': LOOKFORWARD_DAYS,
            'exit_price': None, 'return_pct': ret_pct}


def _walk_symbol(sym: str, tf_data: dict, trading_days: list,
                 strategy: dict, diag: dict = None) -> list:
    """
    Walk forward through trading_days for one symbol.
    Returns list of signal/trade dicts.
    diag: optional dict to collect diagnostics (mutated in place)
    """
    if not tf_data:
        if diag is not None: diag.setdefault('no_data', []).append(sym)
        return []

    daily_df  = tf_data.get('1D')
    if daily_df is None:
        if diag is not None: diag.setdefault('no_daily', []).append(sym)
        return []

    confluence = strategy.get('confluence', {})
    risk_cfg   = strategy.get('risk', {})
    routing    = strategy.get('routing', {})

    cfg_min    = int(confluence.get('min_valid_tfs', 3))
    atr_stop   = float(risk_cfg.get('atr_stop_mult',   2.0))
    atr_target = float(risk_cfg.get('atr_target_mult', 3.0))
    etoro_min  = int(routing.get('etoro_min_tfs', 4))
    ibkr_min   = int(routing.get('ibkr_min_tfs',  2))
    strat_ver  = int(strategy.get('version', 1))

    trades   = []
    cooldown = {}   # direction -> last signal date

    for day_ts in trading_days:
        day = day_ts.date()

        # Check if this day has a daily bar (market open)
        day_bar = daily_df[daily_df.index.date == day]
        if day_bar.empty:
            continue

        # Scale min_valid to available TFs (same logic as live scanner)
        available = len(tf_data)
        if available >= 4:
            min_valid = cfg_min
        elif available >= 2:
            min_valid = max(2, cfg_min - 1)
        else:
            min_valid = 1

        for direction in ('long', 'short'):
            # Cooldown check
            last = cooldown.get(direction)
            if last and (day - last).days < COOLDOWN_DAYS:
                continue

            cutoff = pd.Timestamp(day_ts)
            scores, best_ind = _score_all_tfs(tf_data, cutoff, direction, strategy)
            count = len(scores)

            if count < min_valid or best_ind is None:
                if diag is not None:
                    reason = 'no_indicators' if best_ind is None else f'only_{count}_of_{min_valid}_tfs'
                    diag.setdefault('rejected', {}).setdefault(reason, 0)
                    diag['rejected'][reason] += 1
                continue
            if diag is not None:
                diag.setdefault('candidates', 0)
                diag['candidates'] = diag.get('candidates', 0) + 1

            # Route label
            if count >= etoro_min:
                route = 'ETORO'
            elif count >= ibkr_min:
                route = 'IBKR'
            else:
                route = 'WATCH'

            # Risk levels — identical to live scanner
            entry  = best_ind['price']
            atr    = best_ind['atr']
            if direction == 'long':
                stop_loss    = round(entry - atr_stop   * atr, 4)
                target_price = round(entry + atr_target * atr, 4)
            else:
                stop_loss    = round(entry + atr_stop   * atr, 4)
                target_price = round(entry - atr_target * atr, 4)

            # Evaluate outcome
            outcome = _evaluate_outcome(
                daily_df, cutoff, direction,
                entry, stop_loss, target_price
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
                'rsi':              round(best_ind['rsi'],       2),
                'macd_hist':        round(best_ind['macd_hist'], 6),
                'atr':              round(atr,                   4),
                'bb_pos':           round(best_ind['bb_pos'],    4),
                'vwap_dev':         round(best_ind['vwap_dev'],  4),
                'vol_ratio':        round(best_ind['vol_ratio'], 3),
                'strategy_version': strat_ver,
                **outcome,
            }
            trades.append(trade)
            cooldown[direction] = day

    return trades


# ─────────────────────────────────────────────────────────────────────────────
# Statistics
# ─────────────────────────────────────────────────────────────────────────────

def _compute_stats(trades: list, start_str: str = '', end_str: str = '') -> dict:
    if not trades:
        return {'total': 0}

    wins    = [t for t in trades if t['outcome'] == 'WIN']
    losses  = [t for t in trades if t['outcome'] == 'LOSS']
    timeouts = [t for t in trades if t['outcome'] == 'TIMEOUT']

    total   = len(trades)
    n_win   = len(wins)
    n_loss  = len(losses)
    n_to    = len(timeouts)
    win_rate = round(n_win / total * 100, 1) if total else 0

    all_rets = [t['return_pct'] for t in trades]
    win_rets = [t['return_pct'] for t in wins]
    los_rets = [t['return_pct'] for t in losses]

    avg_ret = round(float(np.mean(all_rets)), 3) if all_rets else 0
    avg_win = round(float(np.mean(win_rets)), 3) if win_rets else 0
    avg_los = round(float(np.mean(los_rets)), 3) if los_rets else 0

    gross_profit = sum(r for r in all_rets if r > 0)
    gross_loss   = abs(sum(r for r in all_rets if r < 0))
    profit_factor = round(gross_profit / gross_loss, 3) if gross_loss > 0 else None

    # Equity curve + max drawdown
    equity   = 100.0
    peak     = equity
    drawdown = 0.0
    eq_curve = []
    for t in sorted(trades, key=lambda x: x['date']):
        equity *= (1 + t['return_pct'] / 100)
        eq_curve.append(round(equity, 4))
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak * 100)

    # By confluence level
    by_conf = {}
    for t in trades:
        k = str(t['valid_count']) + '/4'
        by_conf.setdefault(k, {'total': 0, 'wins': 0})
        by_conf[k]['total'] += 1
        if t['outcome'] == 'WIN':
            by_conf[k]['wins'] += 1
    for k in by_conf:
        n = by_conf[k]['total']
        by_conf[k]['win_rate'] = round(by_conf[k]['wins'] / n * 100, 1) if n else 0

    # By direction
    by_dir = {}
    for t in trades:
        d = t['direction']
        by_dir.setdefault(d, {'total': 0, 'wins': 0, 'rets': []})
        by_dir[d]['total'] += 1
        by_dir[d]['rets'].append(t['return_pct'])
        if t['outcome'] == 'WIN':
            by_dir[d]['wins'] += 1
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
        if t['outcome'] == 'WIN':
            by_route[r]['wins'] += 1
    for r in by_route:
        n = by_route[r]['total']
        by_route[r]['win_rate'] = round(by_route[r]['wins'] / n * 100, 1) if n else 0

    final_equity = round(eq_curve[-1], 2) if eq_curve else 100.0

    # ── Annualised return ─────────────────────────────────────────────────
    try:
        days_range = (date.fromisoformat(end_str) - date.fromisoformat(start_str)).days
    except Exception:
        days_range = 365
    days_range = max(days_range, 1)
    total_ret  = (final_equity / 100.0) - 1.0
    ann_ret    = round(((1 + total_ret) ** (365.0 / days_range) - 1) * 100, 2)

    # ── Average holding period ────────────────────────────────────────────
    bars_held   = [t.get('bars_held', 0) for t in trades if t.get('bars_held')]
    avg_hold    = round(float(np.mean(bars_held)), 1) if bars_held else 0
    max_hold    = max(bars_held) if bars_held else 0

    # ── Consecutive wins / losses ─────────────────────────────────────────
    max_consec_win = max_consec_loss = cur_win = cur_loss = 0
    for t in sorted(trades, key=lambda x: x['date']):
        if t['outcome'] == 'WIN':
            cur_win  += 1; cur_loss = 0
            max_consec_win = max(max_consec_win, cur_win)
        elif t['outcome'] == 'LOSS':
            cur_loss += 1; cur_win  = 0
            max_consec_loss = max(max_consec_loss, cur_loss)
        else:
            cur_win = cur_loss = 0

    # ── Monthly breakdown ─────────────────────────────────────────────────
    by_month = {}
    for t in trades:
        m = t['date'][:7]   # 'YYYY-MM'
        by_month.setdefault(m, {'total': 0, 'wins': 0, 'rets': 0.0})
        by_month[m]['total'] += 1
        by_month[m]['rets']  += t['return_pct']
        if t['outcome'] == 'WIN':
            by_month[m]['wins'] += 1
    for m in by_month:
        n = by_month[m]['total']
        by_month[m]['win_rate'] = round(by_month[m]['wins'] / n * 100, 1) if n else 0
        by_month[m]['avg_ret']  = round(by_month[m]['rets'] / n, 3) if n else 0
        del by_month[m]['rets']

    # ── Per-symbol stats ──────────────────────────────────────────────────
    by_sym = {}
    for t in trades:
        s = t['symbol']
        by_sym.setdefault(s, {'total': 0, 'wins': 0, 'rets': []})
        by_sym[s]['total'] += 1
        by_sym[s]['rets'].append(t['return_pct'])
        if t['outcome'] == 'WIN':
            by_sym[s]['wins'] += 1
    for s in by_sym:
        n = by_sym[s]['total']
        by_sym[s]['win_rate'] = round(by_sym[s]['wins'] / n * 100, 1) if n else 0
        by_sym[s]['avg_ret']  = round(float(np.mean(by_sym[s]['rets'])), 3)
        del by_sym[s]['rets']

    # ── Equity curve with dates ───────────────────────────────────────────
    eq_with_dates = []
    running = 100.0
    for t in sorted(trades, key=lambda x: x['date']):
        running *= (1 + t['return_pct'] / 100)
        eq_with_dates.append({'d': t['date'], 'e': round(running, 4)})

    return {
        'total':            total,
        'wins':             n_win,
        'losses':           n_loss,
        'timeouts':         n_to,
        'win_rate':         win_rate,
        'avg_return_pct':   avg_ret,
        'avg_win_pct':      avg_win,
        'avg_loss_pct':     avg_los,
        'profit_factor':    profit_factor,
        'max_drawdown_pct': round(drawdown, 2),
        'final_equity':     final_equity,
        'annualised_return_pct': ann_ret,
        'avg_hold_days':    avg_hold,
        'max_hold_days':    max_hold,
        'max_consec_wins':  max_consec_win,
        'max_consec_losses': max_consec_loss,
        'by_confluence':    by_conf,
        'by_direction':     by_dir,
        'by_route':         by_route,
        'by_month':         by_month,
        'by_symbol':        by_sym,
        'equity_curve':     eq_curve[-100:],
        'equity_with_dates': eq_with_dates[-100:],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest(symbols: list, start_str: str, end_str: str,
                 strategy: Optional[dict] = None) -> None:
    """
    Run a full backtest asynchronously. Writes progress to backtest_results.json.
    Called in a background thread by the dashboard.
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
    })

    try:
        start = date.fromisoformat(start_str)
        end   = date.fromisoformat(end_str)
        if start >= end:
            raise ValueError('start_date must be before end_date')
        if (end - start).days > 730:
            raise ValueError('Date range cannot exceed 730 days (1H data limit)')

        timeframes = _build_timeframes(strategy)
        if not timeframes:
            raise ValueError('No timeframes enabled in strategy')

        all_trades  = []
        total_syms  = len(symbols)

        diag_per_sym = {}
        HARD_TIMEOUT = 300   # 5 min max for entire run
        run_started  = time.monotonic()

        for i, sym in enumerate(symbols):
            if time.monotonic() - run_started > HARD_TIMEOUT:
                log.warning('[BT] Hard timeout reached — stopping after %d/%d symbols', i, total_syms)
                break

            pct = int((i / total_syms) * 90)

            # Progress callback — called by _fetch_yf_single per-TF
            def _progress(msg, _sym=sym, _i=i, _n=total_syms, _pct=pct):
                _write_results({
                    'status':       'running',
                    'progress':     _pct,
                    'progress_msg': f'[{_i+1}/{_n}] {msg}',
                    'symbols':      symbols,
                    'start_date':   start_str,
                    'end_date':     end_str,
                    'strategy_version': strategy.get('version', 1),
                })

            _write_results({
                'status':       'running',
                'progress':     pct,
                'progress_msg': f'[{i+1}/{total_syms}] {sym}: checking cache...',
                'symbols':      symbols,
                'start_date':   start_str,
                'end_date':     end_str,
                'strategy_version': strategy.get('version', 1),
            })
            log.info('[BT] Fetching %s (%d/%d)', sym, i+1, total_syms)

            tf_data, fetch_statuses = _fetch_all_tfs(sym, start, end, timeframes, _progress)
            if not tf_data:
                reasons = ', '.join(
                    f'{k}:{v.get("status","?")}' for k,v in fetch_statuses.items()
                )
                log.warning('[BT] No data for %s — skipping. Statuses: %s', sym, reasons)
                diag_per_sym[sym] = {
                    'tf_coverage':  {k: v.get('bars',0) for k,v in fetch_statuses.items()},
                    'fetch_status': {k: v.get('status','?') for k,v in fetch_statuses.items()},
                    'tf_first':     {},
                    'tf_last':      {},
                    'candidates':   0,
                    'rejected':     {},
                    'fetch_error':  reasons,
                }
                continue

            # Build trading day index from daily bars
            daily_df = tf_data.get('1D')
            if daily_df is None:
                continue
            trading_days = daily_df[
                (daily_df.index.date >= start) &
                (daily_df.index.date <= end)
            ].index.tolist()

            sym_diag = {
                'tf_coverage':  {},
                'fetch_status': {},
                'tf_first':     {},
                'tf_last':      {},
                'candidates':   0,
                'rejected':     {},
            }
            for tf_label, m in fetch_statuses.items():
                sym_diag['tf_coverage'][tf_label]  = m.get('bars', 0)
                sym_diag['fetch_status'][tf_label] = m.get('status', 'unknown')
                if m.get('first'): sym_diag['tf_first'][tf_label] = m['first']
                if m.get('last'):  sym_diag['tf_last'][tf_label]  = m['last']
            sym_trades = _walk_symbol(sym, tf_data, trading_days, strategy, sym_diag)
            all_trades.extend(sym_trades)
            diag_per_sym[sym] = sym_diag
            cov_summary = {k: f'{v}bars' for k,v in sym_diag['tf_coverage'].items()}
            log.info('[BT] %s: %d signals | coverage: %s | candidates: %d | rejected: %s',
                     sym, len(sym_trades), cov_summary,
                     sym_diag.get('candidates', 0),
                     sym_diag.get('rejected', {}))

        # Sort by date
        all_trades.sort(key=lambda t: t['date'])

        stats = _compute_stats(all_trades, start_str, end_str)
        log.info('[BT] Complete: %d trades | WR: %s%% | PF: %s',
                 stats.get('total', 0),
                 stats.get('win_rate', 0),
                 stats.get('profit_factor', 'n/a'))

        run_ts  = datetime.now(timezone.utc).isoformat()
        days_range = (end - start).days

        # Collect TF availability summary across all symbols
        tf_summary = {}
        for sym_d in diag_per_sym.values():
            for tf, bars in sym_d.get('tf_coverage', {}).items():
                tf_summary.setdefault(tf, {'syms_ok': 0, 'syms_total': 0, 'max_bars': 0})
                tf_summary[tf]['syms_total'] += 1
                if bars > 0:
                    tf_summary[tf]['syms_ok'] += 1
                    tf_summary[tf]['max_bars'] = max(tf_summary[tf]['max_bars'], bars)

        result = {
            'status':           'done',
            'completed_at':     run_ts,
            # ── Run metadata ──────────────────────────────────────────────
            'meta': {
                'symbols':          symbols,
                'start_date':       start_str,
                'end_date':         end_str,
                'days_range':       days_range,
                'run_timestamp':    run_ts,
                'data_source':      'yfinance (Yahoo Finance)',
                'strategy_version': strategy.get('version', 1),
                'strategy_updated': strategy.get('updated_at'),
                'confluence_min':   strategy.get('confluence', {}).get('min_valid_tfs', 3),
                'timeframes_configured': [tf[0] for tf in timeframes],
                'tf_availability':  tf_summary,
                'symbols_count':    len(symbols),
                'symbols_with_data': sum(
                    1 for d in diag_per_sym.values()
                    if any(v > 0 for v in d.get('tf_coverage', {}).values())
                ),
            },
            'symbols':          symbols,
            'start_date':       start_str,
            'end_date':         end_str,
            'strategy_version': strategy.get('version', 1),
            'strategy_confluence': strategy.get('confluence', {}),
            'trades':           all_trades,
            'stats':            stats,
            'diagnostics':      diag_per_sym,
            'progress':         100,
            'progress_msg':     f'Done — {stats.get("total",0)} trades across {len(symbols)} symbol(s)',
        }
        _write_results(result)
        _append_history(result)

    except Exception as e:
        log.error('[BT] Failed: %s', e, exc_info=True)
        _write_results({
            'status':  'error',
            'error':   str(e),
            'symbols': symbols,
            'start_date': start_str,
            'end_date':   end_str,
        })


def cancel_backtest() -> None:
    """Write idle status to unblock a stuck run from the dashboard."""
    _write_results({'status': 'idle', 'progress_msg': 'Cancelled by user.'})


def start_backtest(symbols: list, start_str: str, end_str: str) -> None:
    """Launch backtest in a background thread."""
    strategy = load_strategy()
    t = threading.Thread(
        target=run_backtest,
        args=(symbols, start_str, end_str, strategy),
        daemon=True,
    )
    t.start()
