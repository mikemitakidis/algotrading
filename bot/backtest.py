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
import threading
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


# ─────────────────────────────────────────────────────────────────────────────
# Data fetching (direct yfinance — no per-symbol delays needed for backtest)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_yf(sym: str, start: date, end: date, interval: str) -> tuple:
    """
    Fetch historical bars for one symbol using the same browser session
    as the live bot (bot.data._browser_session).

    Returns (df, status_str) where:
      df is a clean lowercase OHLCV DataFrame or None
      status_str is a human-readable fetch status for diagnostics
    """
    try:
        import yfinance as yf
        # Add warmup lookback so indicators have enough bars from day 1
        warmup_days = 120 if interval == '1d' else 40
        fetch_start = start - timedelta(days=warmup_days)
        fetch_end   = end   + timedelta(days=2)  # inclusive

        session = _browser_session()

        df = yf.download(
            sym,
            start    = fetch_start.strftime('%Y-%m-%d'),
            end      = fetch_end.strftime('%Y-%m-%d'),
            interval = interval,
            auto_adjust      = True,
            progress         = False,
            threads          = False,
            multi_level_index = False,   # flat columns: Close, High, Low, Open, Volume
            session          = session,  # same browser session as live bot
        )
        if df is None or df.empty:
            return None, 'empty_response'

        # Normalise column names to lowercase
        df.columns = [c.lower() if isinstance(c, str) else str(c[0]).lower()
                      for c in df.columns]
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
            return None, f'too_few_bars_{len(df)}'

        return df, 'ok'

    except Exception as e:
        err = str(e)
        if any(k in err for k in ('429', 'Too Many', 'rate', 'Rate', 'TooMany')):
            status = 'rate_limited'
        elif any(k in err for k in ('403', 'Forbidden', 'proxy', 'Proxy', 'tunnel')):
            status = 'network_error'
        elif 'No data found' in err or 'no data' in err.lower():
            status = 'no_data_from_provider'
        else:
            status = f'error: {err[:80]}'
        log.warning('[BT] Fetch failed %s/%s: %s', sym, interval, status)
        return None, status


def _fetch_all_tfs(sym: str, start: date, end: date,
                   timeframes: list) -> tuple:
    """
    Fetch data for all enabled timeframes for one symbol.
    Returns (result_dict, fetch_statuses) where:
      result_dict: tf_label -> DataFrame (only successful fetches)
      fetch_statuses: tf_label -> status string (for diagnostics)
    """
    result   = {}
    statuses = {}
    for tf_label, period, interval, do_resample in timeframes:
        raw, status = _fetch_yf(sym, start, end, interval)
        statuses[tf_label] = status
        if raw is None:
            log.warning('[BT] %s %s: %s', sym, tf_label, status)
            continue
        if do_resample:
            raw = resample_to_4h(raw)
        result[tf_label] = raw
        log.info('[BT] %s %s: %d bars loaded', sym, tf_label, len(raw))
    return result, statuses


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

def _compute_stats(trades: list) -> dict:
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

    return {
        'total':          total,
        'wins':           n_win,
        'losses':         n_loss,
        'timeouts':       n_to,
        'win_rate':       win_rate,
        'avg_return_pct': avg_ret,
        'avg_win_pct':    avg_win,
        'avg_loss_pct':   avg_los,
        'profit_factor':  profit_factor,
        'max_drawdown_pct': round(drawdown, 2),
        'final_equity':   final_equity,
        'by_confluence':  by_conf,
        'by_direction':   by_dir,
        'by_route':       by_route,
        'equity_curve':   eq_curve[-100:],   # last 100 points for chart
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
        for i, sym in enumerate(symbols):
            pct = int((i / total_syms) * 90)
            _write_results({
                'status':       'running',
                'progress':     pct,
                'progress_msg': f'Fetching {sym} ({i+1}/{total_syms})...',
                'symbols':      symbols,
                'start_date':   start_str,
                'end_date':     end_str,
                'strategy_version': strategy.get('version', 1),
            })
            log.info('[BT] Fetching %s (%d/%d)', sym, i+1, total_syms)

            tf_data, fetch_statuses = _fetch_all_tfs(sym, start, end, timeframes)
            if not tf_data:
                reasons = ', '.join(f'{k}:{v}' for k,v in fetch_statuses.items())
                log.warning('[BT] No data for %s — skipping. Fetch statuses: %s', sym, reasons)
                diag_per_sym[sym] = {
                    'tf_coverage': {},
                    'fetch_status': fetch_statuses,
                    'candidates': 0,
                    'rejected': {},
                    'fetch_error': reasons,
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

            sym_diag = {'tf_coverage': {}, 'fetch_status': {}, 'candidates': 0, 'rejected': {}}
            for tf_label, df in tf_data.items():
                sym_diag['tf_coverage'][tf_label] = len(df)
            for tf_label, status in fetch_statuses.items():
                sym_diag['fetch_status'][tf_label] = status
            sym_trades = _walk_symbol(sym, tf_data, trading_days, strategy, sym_diag)
            all_trades.extend(sym_trades)
            diag_per_sym[sym] = sym_diag
            log.info('[BT] %s: %d signals | TFs: %s | fetch: %s | candidates: %d | rejected: %s',
                     sym, len(sym_trades),
                     {k: v for k, v in sym_diag['tf_coverage'].items()},
                     sym_diag.get('fetch_status', {}),
                     sym_diag.get('candidates', 0),
                     sym_diag.get('rejected', {}))

        # Sort by date
        all_trades.sort(key=lambda t: t['date'])

        stats = _compute_stats(all_trades)
        log.info('[BT] Complete: %d trades | WR: %s%% | PF: %s',
                 stats.get('total', 0),
                 stats.get('win_rate', 0),
                 stats.get('profit_factor', 'n/a'))

        _write_results({
            'status':           'done',
            'completed_at':     datetime.now(timezone.utc).isoformat(),
            'symbols':          symbols,
            'start_date':       start_str,
            'end_date':         end_str,
            'strategy_version': strategy.get('version', 1),
            'strategy_confluence': strategy.get('confluence', {}),
            'trades':           all_trades,
            'stats':            stats,
            'diagnostics':      diag_per_sym,
            'progress':         100,
            'progress_msg':     f'Done — {stats.get("total",0)} trades',
        })

    except Exception as e:
        log.error('[BT] Failed: %s', e, exc_info=True)
        _write_results({
            'status':  'error',
            'error':   str(e),
            'symbols': symbols,
            'start_date': start_str,
            'end_date':   end_str,
        })


def start_backtest(symbols: list, start_str: str, end_str: str) -> None:
    """Launch backtest in a background thread."""
    strategy = load_strategy()
    t = threading.Thread(
        target=run_backtest,
        args=(symbols, start_str, end_str, strategy),
        daemon=True,
    )
    t.start()
