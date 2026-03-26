"""
bot/backtest_v2.py  —  Clean walk-forward backtesting engine (v2 rebuild)

Design principles:
  - Synchronous pure function: run(symbols, start, end) -> dict
  - No threads, no polling, no state files inside the engine
  - Uses IDENTICAL live components (no copies):
      bot.strategy.load(), bot.indicators.compute(), bot.scanner.score_timeframe()
  - Data fetched via yf.Ticker().history() + browser session (same as live bot)
  - Simple disk cache: data/bt_v2_cache/<sym>_<interval>.parquet (or json fallback)
  - All failures surface as explicit status fields — no silent zero-result runs
  - Caller decides threading/persistence; engine is unaware of dashboard

Entry point:
    from bot.backtest_v2 import run
    result = run(['AAPL'], '2025-03-20', '2026-03-20')
"""

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# ── Live components (unchanged) ───────────────────────────────────────────────
from bot.strategy   import load as load_strategy
from bot.indicators import compute
from bot.scanner    import score_timeframe, _build_timeframes
from bot.data       import resample_to_4h

log = logging.getLogger(__name__)

BASE_DIR      = Path(__file__).resolve().parent.parent
CACHE_DIR     = BASE_DIR / 'data' / 'bt_v2_cache'
REPORTS_DIR   = BASE_DIR / 'data' / 'reports'
MIN_BARS      = 60      # minimum bars needed before computing indicators
LOOKFWD_DAYS  = 20      # trading days to evaluate outcome
COOLDOWN_DAYS = 3       # min days between same symbol+direction signals


# ─────────────────────────────────────────────────────────────────────────────
# Data fetching
# ─────────────────────────────────────────────────────────────────────────────

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/122.0.0.0 Safari/537.36'
        ),
        'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
    })
    return s


def _cache_file(sym: str, interval: str, fetch_start: date, fetch_end: date) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = f'{sym}_{interval}_{fetch_start}_{fetch_end}.json'
    return CACHE_DIR / key


def _cache_load(sym: str, interval: str,
                fetch_start: date, fetch_end: date) -> Optional[pd.DataFrame]:
    """Load cached bars if file exists and is < 24h old."""
    p = _cache_file(sym, interval, fetch_start, fetch_end)
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
        return df if len(df) >= MIN_BARS else None
    except Exception:
        return None


def _cache_save(sym: str, interval: str,
                fetch_start: date, fetch_end: date,
                df: pd.DataFrame) -> None:
    try:
        rows = {str(k): v for k, v in df.to_dict(orient='index').items()}
        _cache_file(sym, interval, fetch_start, fetch_end).write_text(
            json.dumps({'ts': time.time(), 'rows': rows}, default=str)
        )
    except Exception:
        pass


def _fetch(sym: str, interval: str,
           backtest_start: date, backtest_end: date) -> tuple:
    """
    Fetch OHLCV bars for one symbol/interval, cache-first.
    Up to 3 attempts with backoff on rate limiting.
    Returns (df_or_None, status_str, bars_int, first_date, last_date).
    """
    warmup      = 120 if interval == '1d' else 60
    fetch_start = backtest_start - timedelta(days=warmup)
    fetch_end   = backtest_end   + timedelta(days=2)

    # Cache hit
    cached = _cache_load(sym, interval, fetch_start, fetch_end)
    if cached is not None:
        first = cached.index[0].strftime('%Y-%m-%d')
        last  = cached.index[-1].strftime('%Y-%m-%d')
        log.info('[BT2] %s %s: cache %d bars (%s→%s)', sym, interval, len(cached), first, last)
        return cached, 'ok_cached', len(cached), first, last

    # Network fetch — 3 attempts with backoff on rate limit
    for attempt in range(3):
        if attempt > 0:
            wait = attempt * 12   # 12s then 24s
            log.warning('[BT2] %s %s: rate limited — waiting %ds (attempt %d/3)',
                        sym, interval, wait, attempt + 1)
            time.sleep(wait)

        try:
            ses = _session()
            df  = yf.Ticker(sym, session=ses).history(
                start        = fetch_start.strftime('%Y-%m-%d'),
                end          = fetch_end.strftime('%Y-%m-%d'),
                interval     = interval,
                auto_adjust  = True,
                actions      = False,
                raise_errors = False,
            )

            if df is None or df.empty:
                log.warning('[BT2] %s %s: empty response from Yahoo', sym, interval)
                return None, 'empty_response', 0, None, None

            df.columns = [c.lower() for c in df.columns]
            keep = [c for c in ('open', 'high', 'low', 'close', 'volume') if c in df.columns]
            if not keep:
                return None, 'missing_columns', 0, None, None

            df = df[keep].dropna()

            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index, utc=True)
            elif df.index.tz is None:
                df.index = df.index.tz_localize('UTC')
            else:
                df.index = df.index.tz_convert('UTC')

            if len(df) < MIN_BARS:
                log.warning('[BT2] %s %s: only %d bars (need %d)', sym, interval, len(df), MIN_BARS)
                return None, f'too_few_bars_{len(df)}', len(df), None, None

            first = df.index[0].strftime('%Y-%m-%d')
            last  = df.index[-1].strftime('%Y-%m-%d')
            log.info('[BT2] %s %s: fetched %d bars (%s→%s)', sym, interval, len(df), first, last)

            _cache_save(sym, interval, fetch_start, fetch_end, df)
            time.sleep(1.5)
            return df, 'ok', len(df), first, last

        except Exception as e:
            err = str(e)
            is_rl = any(k in err for k in ('429', 'Too Many', 'rate', 'Rate', 'TooMany'))
            if is_rl and attempt < 2:
                continue   # retry with backoff
            if is_rl:
                return None, 'rate_limited', 0, None, None
            if any(k in err for k in ('403', 'Forbidden', 'proxy', 'tunnel')):
                return None, 'network_error', 0, None, None
            log.warning('[BT2] %s %s fetch error: %s', sym, interval, err[:80])
            return None, f'error:{err[:80]}', 0, None, None

    return None, 'rate_limited', 0, None, None   # exhausted retries


def _fetch_symbol_data(sym: str,
                       backtest_start: date, backtest_end: date,
                       timeframes: list) -> tuple:
    """
    Fetch all TFs for one symbol. Deduplicates: 4H and 1H share '1h' fetch.
    Returns (tf_data dict, fetch_report dict).
    tf_data:      label -> DataFrame
    fetch_report: label -> {status, bars, first, last}
    """
    tf_data      = {}
    fetch_report = {}
    raw_by_ivl   = {}   # interval -> (df, status, bars, first, last)

    for label, _period, interval, do_resample in timeframes:
        if interval not in raw_by_ivl:
            raw_by_ivl[interval] = _fetch(sym, interval, backtest_start, backtest_end)

        df, status, bars, first, last = raw_by_ivl[interval]

        fetch_report[label] = {
            'status': status, 'bars': bars, 'first': first, 'last': last
        }

        if df is None:
            continue

        working = resample_to_4h(df) if do_resample else df
        tf_data[label] = working

        if do_resample:
            fetch_report[label]['bars']  = len(working)
            fetch_report[label]['first'] = working.index[0].strftime('%Y-%m-%d') if len(working) else None
            fetch_report[label]['last']  = working.index[-1].strftime('%Y-%m-%d') if len(working) else None

    return tf_data, fetch_report


# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward scoring
# ─────────────────────────────────────────────────────────────────────────────

_TF_PRIORITY = ['1D', '4H', '1H', '15m']


def _score_day(tf_data: dict, cutoff: pd.Timestamp,
               direction: str, strategy: dict) -> tuple:
    """
    Score all available TFs up to cutoff.
    Returns (passing_tfs list, best_indicators dict or None).
    """
    passing  = []
    best_ind = None

    for label in _TF_PRIORITY:
        df = tf_data.get(label)
        if df is None:
            continue

        sl = df[df.index <= cutoff]
        if len(sl) < MIN_BARS:
            continue

        ind = compute(sl.tail(200))
        if ind is None:
            continue

        if score_timeframe(ind, direction, strategy):
            passing.append(label)
            if best_ind is None:
                best_ind = ind

    return passing, best_ind


def _evaluate_trade(daily_df: pd.DataFrame,
                    signal_date: pd.Timestamp,
                    direction: str,
                    entry: float, stop: float, target: float) -> dict:
    """
    Look forward up to LOOKFWD_DAYS daily bars.
    Returns outcome dict with outcome/bars_held/exit_price/return_pct.
    """
    future = daily_df[daily_df.index > signal_date].head(LOOKFWD_DAYS)

    for i, (_, row) in enumerate(future.iterrows()):
        hi = float(row.get('high', 0))
        lo = float(row.get('low',  0))

        if direction == 'long':
            hit_stop   = lo <= stop
            hit_target = hi >= target
        else:
            hit_stop   = hi >= stop
            hit_target = lo <= target

        # Stop wins on same-bar conflict (conservative)
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

    # Timeout
    last_close = float(future.iloc[-1]['close']) if len(future) else entry
    ret = (last_close - entry) / entry * 100 * (1 if direction == 'long' else -1)
    return {'outcome': 'TIMEOUT', 'bars_held': len(future),
            'exit_price': last_close, 'return_pct': round(ret, 3)}


def _walk_symbol(sym: str, tf_data: dict,
                 trading_days: list, strategy: dict) -> tuple:
    """
    Walk forward through trading_days for one symbol.
    Returns (trades list, diag dict).
    diag keys: candidates, rejected_by_reason, cooldown_skips
    """
    daily_df = tf_data.get('1D')
    if daily_df is None:
        return [], {'error': 'no_1D_data', 'candidates': 0, 'rejected': {}}

    confluence = strategy.get('confluence', {})
    risk       = strategy.get('risk', {})
    routing    = strategy.get('routing', {})

    cfg_min    = int(confluence.get('min_valid_tfs', 3))
    atr_stop   = float(risk.get('atr_stop_mult',   2.0))
    atr_target = float(risk.get('atr_target_mult', 3.0))
    etoro_min  = int(routing.get('etoro_min_tfs', 4))
    ibkr_min   = int(routing.get('ibkr_min_tfs',  2))
    strat_ver  = int(strategy.get('version', 1))

    available = len(tf_data)
    if   available >= 4: min_valid = cfg_min
    elif available >= 2: min_valid = max(2, cfg_min - 1)
    else:                min_valid = 1

    trades         = []
    cooldown       = {}   # direction -> last signal date
    candidates     = 0
    rejected       = {}   # reason -> count
    cooldown_skips = 0

    for day_ts in trading_days:
        day = day_ts.date()

        # Skip if no 1D bar (market closed)
        if daily_df[daily_df.index.date == day].empty:
            continue

        for direction in ('long', 'short'):
            # Cooldown check
            last = cooldown.get(direction)
            if last and (day - last).days < COOLDOWN_DAYS:
                cooldown_skips += 1
                continue

            cutoff = pd.Timestamp(day_ts)
            passing, best_ind = _score_day(tf_data, cutoff, direction, strategy)
            count = len(passing)

            if count < min_valid or best_ind is None:
                reason = 'no_indicators' if (best_ind is None and count == 0) \
                         else f'only_{count}_of_{min_valid}_tfs'
                rejected[reason] = rejected.get(reason, 0) + 1
                continue

            candidates += 1

            # Route label
            route = ('ETORO' if count >= etoro_min else
                     'IBKR'  if count >= ibkr_min  else 'WATCH')

            # Risk levels
            entry = best_ind['price']
            atr   = best_ind['atr']
            if direction == 'long':
                stop_loss    = round(entry - atr_stop   * atr, 4)
                target_price = round(entry + atr_target * atr, 4)
            else:
                stop_loss    = round(entry + atr_stop   * atr, 4)
                target_price = round(entry - atr_target * atr, 4)

            outcome = _evaluate_trade(
                daily_df, cutoff, direction, entry, stop_loss, target_price
            )

            trades.append({
                'symbol':           sym,
                'date':             day.isoformat(),
                'direction':        direction,
                'route':            route,
                'tfs_triggered':    passing,
                'valid_count':      count,
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
            })
            cooldown[direction] = day

    diag = {
        'candidates':     candidates,
        'rejected':       rejected,
        'cooldown_skips': cooldown_skips,
        'tfs_available':  list(tf_data.keys()),
        'min_valid':      min_valid,
    }
    return trades, diag


# ─────────────────────────────────────────────────────────────────────────────
# Statistics
# ─────────────────────────────────────────────────────────────────────────────

def _stats(trades: list, start_str: str, end_str: str) -> dict:
    if not trades:
        return {'total': 0}

    wins    = [t for t in trades if t['outcome'] == 'WIN']
    losses  = [t for t in trades if t['outcome'] == 'LOSS']
    tos     = [t for t in trades if t['outcome'] == 'TIMEOUT']
    total   = len(trades)
    rets    = [t['return_pct'] for t in trades]

    win_rate = round(len(wins) / total * 100, 1)
    avg_ret  = round(float(np.mean(rets)), 3) if rets else 0
    avg_win  = round(float(np.mean([t['return_pct'] for t in wins])),   3) if wins else 0
    avg_los  = round(float(np.mean([t['return_pct'] for t in losses])), 3) if losses else 0

    gp = sum(r for r in rets if r > 0)
    gl = abs(sum(r for r in rets if r < 0))
    pf = round(gp / gl, 3) if gl > 0 else None

    # Equity + drawdown
    eq = 100.0; peak = 100.0; max_dd = 0.0; eq_curve = []; eq_dates = []
    for t in sorted(trades, key=lambda x: x['date']):
        eq *= (1 + t['return_pct'] / 100)
        eq_curve.append(round(eq, 4))
        eq_dates.append({'d': t['date'], 'e': round(eq, 4)})
        peak   = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100)
    final_eq = round(eq_curve[-1], 2)

    # Annualised
    try:
        days = max((date.fromisoformat(end_str) - date.fromisoformat(start_str)).days, 1)
    except Exception:
        days = 365
    ann_ret = round(((1 + (final_eq / 100 - 1)) ** (365.0 / days) - 1) * 100, 2)

    # Hold days
    holds   = [t.get('bars_held', 0) for t in trades if t.get('bars_held')]
    avg_hld = round(float(np.mean(holds)), 1) if holds else 0

    # Streaks
    max_cw = max_cl = cw = cl = 0
    for t in sorted(trades, key=lambda x: x['date']):
        if t['outcome'] == 'WIN':
            cw += 1; cl = 0; max_cw = max(max_cw, cw)
        elif t['outcome'] == 'LOSS':
            cl += 1; cw = 0; max_cl = max(max_cl, cl)
        else:
            cw = cl = 0

    # By TF
    by_tf = {}
    for tf in ['1D', '4H', '1H', '15m']:
        sub = [t for t in trades if tf in t.get('tfs_triggered', [])]
        if not sub: continue
        by_tf[tf] = {
            'trades':   len(sub),
            'wins':     sum(1 for t in sub if t['outcome'] == 'WIN'),
            'win_rate': round(sum(1 for t in sub if t['outcome'] == 'WIN') / len(sub) * 100, 1),
            'avg_ret':  round(float(np.mean([t['return_pct'] for t in sub])), 3),
        }

    # By TF combo
    by_combo = {}
    for t in trades:
        k = '+'.join(sorted(t.get('tfs_triggered', [])))
        by_combo.setdefault(k, {'total': 0, 'wins': 0, 'rets': []})
        by_combo[k]['total'] += 1
        by_combo[k]['rets'].append(t['return_pct'])
        if t['outcome'] == 'WIN': by_combo[k]['wins'] += 1
    for k in by_combo:
        n = by_combo[k]['total']
        by_combo[k]['win_rate'] = round(by_combo[k]['wins'] / n * 100, 1)
        by_combo[k]['avg_ret']  = round(float(np.mean(by_combo[k]['rets'])), 3)
        del by_combo[k]['rets']

    # By confluence
    by_conf = {}
    for t in trades:
        k = f"{t['valid_count']}/4"
        by_conf.setdefault(k, {'total': 0, 'wins': 0})
        by_conf[k]['total'] += 1
        if t['outcome'] == 'WIN': by_conf[k]['wins'] += 1
    for k in by_conf:
        n = by_conf[k]['total']
        by_conf[k]['win_rate'] = round(by_conf[k]['wins'] / n * 100, 1)

    # By direction
    by_dir = {}
    for t in trades:
        d = t['direction']
        by_dir.setdefault(d, {'total': 0, 'wins': 0, 'rets': []})
        by_dir[d]['total'] += 1
        by_dir[d]['rets'].append(t['return_pct'])
        if t['outcome'] == 'WIN': by_dir[d]['wins'] += 1
    for d in by_dir:
        n = by_dir[d]['total']
        by_dir[d]['win_rate'] = round(by_dir[d]['wins'] / n * 100, 1)
        by_dir[d]['avg_ret']  = round(float(np.mean(by_dir[d]['rets'])), 3)
        del by_dir[d]['rets']

    # Monthly
    by_month = {}
    for t in trades:
        m = t['date'][:7]
        by_month.setdefault(m, {'total': 0, 'wins': 0, 'rets': []})
        by_month[m]['total'] += 1
        by_month[m]['rets'].append(t['return_pct'])
        if t['outcome'] == 'WIN': by_month[m]['wins'] += 1
    for m in by_month:
        n = by_month[m]['total']
        by_month[m]['win_rate'] = round(by_month[m]['wins'] / n * 100, 1)
        by_month[m]['avg_ret']  = round(float(np.mean(by_month[m]['rets'])), 3)
        del by_month[m]['rets']

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

    # By symbol
    by_sym = {}
    for t in trades:
        sym = t['symbol']
        by_sym.setdefault(sym, {'total': 0, 'wins': 0, 'rets': []})
        by_sym[sym]['total'] += 1
        by_sym[sym]['rets'].append(t['return_pct'])
        if t['outcome'] == 'WIN': by_sym[sym]['wins'] += 1
    for sym in by_sym:
        n = by_sym[sym]['total']
        by_sym[sym]['win_rate'] = round(by_sym[sym]['wins'] / n * 100, 1) if n else 0
        by_sym[sym]['avg_ret']  = round(float(np.mean(by_sym[sym]['rets'])), 3)
        del by_sym[sym]['rets']

    return {
        'total':                 total,
        'wins':                  len(wins),
        'losses':                len(losses),
        'timeouts':              len(tos),
        'win_rate':              win_rate,
        'avg_return_pct':        avg_ret,
        'avg_win_pct':           avg_win,
        'avg_loss_pct':          avg_los,
        'profit_factor':         pf,
        'max_drawdown_pct':      round(max_dd, 2),
        'final_equity':          final_eq,
        'annualised_return_pct': ann_ret,
        'avg_hold_days':         avg_hld,
        'max_consec_wins':       max_cw,
        'max_consec_losses':     max_cl,
        'by_timeframe':          by_tf,
        'by_tf_combo':           by_combo,
        'by_confluence':         by_conf,
        'by_direction':          by_dir,
        'by_month':              by_month,
        'equity_curve':          eq_curve[-100:],
        'equity_with_dates':     eq_dates[-100:],
        'by_route':              by_route,
        'by_symbol':             by_sym,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Report export
# ─────────────────────────────────────────────────────────────────────────────

def _export(result: dict) -> Path:
    """Save report.txt, trades.csv, results.json to timestamped folder."""
    ts     = datetime.now().strftime('%Y%m%d_%H%M%S')
    syms   = '_'.join(result.get('symbols', ['unknown'])[:3])
    folder = REPORTS_DIR / f'{ts}_{syms}'
    folder.mkdir(parents=True, exist_ok=True)

    s = result.get('stats', {})
    m = result.get('meta', {})

    lines = [
        '=' * 65,
        'ALGO TRADER  —  BACKTEST REPORT  (v2)',
        '=' * 65,
        f"Generated  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        '',
        '── RUN PARAMETERS ──────────────────────────────────────────',
        f"Symbols    : {', '.join(result.get('symbols', []))}",
        f"Period     : {result.get('start_date','')} → {result.get('end_date','')}",
        f"Strategy   : v{m.get('strategy_version',1)}  |  "
        f"Confluence ≥{m.get('confluence_min',3)}/4 TFs",
        '',
    ]

    if not s.get('total'):
        lines.append('No trades generated.')
        lines.append('')
        # Diagnostics
        diags = result.get('diagnostics', {})
        for sym, d in diags.items():
            lines.append(f'── {sym} DIAGNOSTICS ──────────────────────────────────────')
            lines.append(f"  TF coverage:")
            fst = d.get('fetch_status', {})
            cov = d.get('tf_coverage', {})
            fir = d.get('tf_first', {})
            fla = d.get('tf_last', {})
            for lbl in ['1D','4H','1H','15m']:
                if lbl not in fst: continue
                rng = f'  {fir[lbl]}→{fla[lbl]}' if fir.get(lbl) else ''
                lines.append(f'    {lbl}: {cov.get(lbl,0)} bars  [{fst[lbl]}]{rng}')
            if d.get('fetch_error'):
                lines.append(f"  Fetch error: {d['fetch_error']}")
            lines.append(f"  Candidates : {d.get('candidates', 0)}")
            rej = d.get('rejected', {})
            if rej: lines.append(f"  Rejected   : {rej}")
    else:
        lines += [
            '── PERFORMANCE ──────────────────────────────────────────────',
            f"Total trades   : {s['total']}",
            f"Win/Loss/TO    : {s['wins']} / {s['losses']} / {s['timeouts']}",
            f"Win rate       : {s['win_rate']}%",
            f"Profit factor  : {s.get('profit_factor', 'n/a')}",
            f"Avg return     : {s['avg_return_pct']}%",
            f"Max drawdown   : {s['max_drawdown_pct']}%",
            f"Final equity   : {s['final_equity']}  (start=100)",
            f"Annualised ret : {s['annualised_return_pct']}%",
            f"Avg hold days  : {s['avg_hold_days']}",
            f"Max consec W/L : {s['max_consec_wins']} / {s['max_consec_losses']}",
            '',
        ]
        by_tf = s.get('by_timeframe', {})
        if by_tf:
            lines.append('── BY TIMEFRAME ─────────────────────────────────────────────')
            for tf in ['1D', '4H', '1H', '15m']:
                if tf not in by_tf: continue
                v = by_tf[tf]
                lines.append(f"  {tf:<5}: {v['trades']:>3} trades  "
                             f"WR:{v['win_rate']:>5.1f}%  "
                             f"AvgRet:{v['avg_ret']:>+7.3f}%")
            lines.append('')

        by_combo = s.get('by_tf_combo', {})
        if by_combo:
            lines.append('── BY TF COMBINATION ────────────────────────────────────────')
            for combo, v in sorted(by_combo.items(), key=lambda x: -x[1]['total']):
                lines.append(f"  {combo:<22}: {v['total']:>3} trades  "
                             f"WR:{v['win_rate']:>5.1f}%  "
                             f"AvgRet:{v['avg_ret']:>+7.3f}%")
            lines.append('')

        by_month = s.get('by_month', {})
        if by_month:
            lines.append('── MONTHLY BREAKDOWN ────────────────────────────────────────')
            for month in sorted(by_month):
                v = by_month[month]
                lines.append(f"  {month}: {v['total']:>3} trades  "
                             f"WR:{v['win_rate']:>5.1f}%  "
                             f"AvgRet:{v['avg_ret']:>+7.3f}%")
            lines.append('')

        lines.append('── TRADE LIST ───────────────────────────────────────────────')
        hdr = (f"{'Date':<12} {'Sym':<6} {'Dir':<6} {'Rt':<6} "
               f"{'TFs':<14} {'Entry':>7} {'Stop':>7} {'Tgt':>7} "
               f"{'RSI':>5} {'Out':<8} {'Ret':>7}")
        lines.append(hdr)
        lines.append('-' * 90)
        for t in result.get('trades', []):
            tfs = '+'.join(t.get('tfs_triggered', []))
            lines.append(
                f"{t['date']:<12} {t['symbol']:<6} {t['direction']:<6} {t['route']:<6} "
                f"{tfs:<14} {t['entry_price']:>7.2f} {t['stop_loss']:>7.2f} "
                f"{t['target_price']:>7.2f} {t['rsi']:>5.1f} {t['outcome']:<8} "
                f"{t['return_pct']:>+6.3f}%"
            )

    lines += ['', '=' * 65, 'END OF REPORT', '=' * 65]

    (folder / 'report.txt').write_text('\n'.join(lines))
    (folder / 'results.json').write_text(
        json.dumps(result, default=str, indent=2)
    )

    trades = result.get('trades', [])
    if trades:
        import csv, io
        buf = io.StringIO()
        keys = ['date','symbol','direction','route','valid_count','tfs_triggered',
                'entry_price','stop_loss','target_price','outcome','return_pct',
                'bars_held','rsi','macd_hist','atr','bb_pos','vwap_dev','vol_ratio',
                'strategy_version']
        w = csv.DictWriter(buf, fieldnames=keys, extrasaction='ignore')
        w.writeheader()
        for t in trades:
            row = dict(t)
            row['tfs_triggered'] = '+'.join(t.get('tfs_triggered', []))
            w.writerow(row)
        (folder / 'trades.csv').write_text(buf.getvalue())

    _append_history(result)
    log.info('[BT2] Report saved: %s', folder)
    return folder


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def _append_history(result: dict) -> None:
    """Append compact run summary to data/backtest_history.json (last 20 runs)."""
    HISTORY_PATH = BASE_DIR / 'data' / 'backtest_history.json'
    MAX_HISTORY  = 20
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
            'run_at':                result.get('run_at', ''),
            'status':                result.get('status', 'ok'),
            'symbols':               result.get('symbols', []),
            'start_date':            result.get('start_date', ''),
            'end_date':              result.get('end_date', ''),
            'days_range':            (
                (date.fromisoformat(result.get('end_date','')) -
                 date.fromisoformat(result.get('start_date',''))).days
                if result.get('start_date') and result.get('end_date') else 0
            ),
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
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_PATH.write_text(json.dumps(history, default=str))
    except Exception as e:
        log.debug('[BT2] History append failed: %s', e)


def _flatten_diag(fetch_report: dict, walk_diag, error: str = '') -> dict:
    """
    Convert v2 internal diagnostics to the flat schema the dashboard expects:
      tf_coverage:  {label: bars}
      fetch_status: {label: status_str}
      tf_first:     {label: first_date}
      tf_last:      {label: last_date}
      fetch_error:  str (non-empty when all fetches failed)
      candidates:   int
      rejected:     {reason: count}
    """
    tf_cov    = {}
    tf_status = {}
    tf_first  = {}
    tf_last   = {}
    all_failed = True
    for lbl, info in fetch_report.items():
        tf_cov[lbl]    = info.get('bars', 0)
        tf_status[lbl] = info.get('status', 'unknown')
        if info.get('first'): tf_first[lbl] = info['first']
        if info.get('last'):  tf_last[lbl]  = info['last']
        if info.get('bars', 0) > 0: all_failed = False
    fetch_error = error or (
        ', '.join(f'{k}:{v}' for k, v in tf_status.items()) if all_failed else ''
    )
    return {
        'tf_coverage':  tf_cov,
        'fetch_status': tf_status,
        'tf_first':     tf_first,
        'tf_last':      tf_last,
        'fetch_error':  fetch_error,
        'candidates':   walk_diag.get('candidates', 0) if walk_diag else 0,
        'rejected':     walk_diag.get('rejected', {})  if walk_diag else {},
    }


def run(symbols: list, start_str: str, end_str: str,
        strategy: Optional[dict] = None,
        export: bool = True) -> dict:
    """
    Run a walk-forward backtest synchronously. Returns result dict.

    result keys:
        status         : 'ok' | 'error'
        symbols        : list
        start_date     : str
        end_date       : str
        meta           : run parameters + TF availability
        trades         : list of trade dicts
        stats          : performance statistics
        diagnostics    : per-symbol fetch + walk diagnostics
        report_folder  : Path string (if export=True)
    """
    t0 = time.monotonic()

    if strategy is None:
        strategy = load_strategy()

    try:
        start = date.fromisoformat(start_str)
        end   = date.fromisoformat(end_str)
    except ValueError as e:
        return {'status': 'error', 'error': f'Invalid dates: {e}'}

    if start >= end:
        return {'status': 'error', 'error': 'start_date must be before end_date'}
    if (end - start).days > 730:
        return {'status': 'error', 'error': 'Date range > 730 days (1H data limit)'}

    timeframes = _build_timeframes(strategy)
    if not timeframes:
        return {'status': 'error', 'error': 'No timeframes enabled in strategy'}

    all_trades  = []
    diagnostics = {}

    for sym in symbols:
        log.info('[BT2] Processing %s …', sym)

        tf_data, fetch_report = _fetch_symbol_data(sym, start, end, timeframes)

        if not tf_data:
            reasons = {k: v['status'] for k, v in fetch_report.items()}
            log.warning('[BT2] %s: no data loaded — %s', sym, reasons)
            diagnostics[sym] = _flatten_diag(fetch_report, None, error='no_data')
            continue

        # Trading days from 1D bars
        daily = tf_data.get('1D')
        if daily is None:
            diagnostics[sym] = _flatten_diag(fetch_report, None, error='no_1D_data')
            continue

        trading_days = daily[
            (daily.index.date >= start) & (daily.index.date <= end)
        ].index.tolist()

        trades, walk_diag = _walk_symbol(sym, tf_data, trading_days, strategy)
        all_trades.extend(trades)
        diagnostics[sym] = _flatten_diag(fetch_report, walk_diag)

        log.info('[BT2] %s: %d trades | candidates=%d | rejected=%s',
                 sym, len(trades),
                 walk_diag.get('candidates', 0),
                 walk_diag.get('rejected', {}))

    all_trades.sort(key=lambda t: t['date'])
    stats = _stats(all_trades, start_str, end_str)

    elapsed = round(time.monotonic() - t0, 1)
    run_ts  = datetime.now(timezone.utc).isoformat()

    # TF availability summary
    tf_summary = {}
    for sym_d in diagnostics.values():
        # diagnostics[sym] is now flat (_flatten_diag): use tf_coverage directly
        cov = sym_d.get('tf_coverage', {})
        for lbl, bars in cov.items():
            tf_summary.setdefault(lbl, {'syms_ok': 0, 'syms_total': 0, 'max_bars': 0})
            tf_summary[lbl]['syms_total'] += 1
            if bars > 0:
                tf_summary[lbl]['syms_ok'] += 1
                tf_summary[lbl]['max_bars'] = max(tf_summary[lbl]['max_bars'], bars)

    result = {
        'status':              (
            'no_data' if not any(
                any(v > 0 for v in d.get('tf_coverage', {}).values())
                for d in diagnostics.values()
            ) else 'ok'
        ),
        'run_at':              run_ts,
        'elapsed_s':           elapsed,
        'symbols':             symbols,
        'start_date':          start_str,
        'end_date':            end_str,
        # Top-level fields matched to dashboard expectations
        'strategy_version':    strategy.get('version', 1),
        'strategy_confluence': strategy.get('confluence', {}),
        'meta': {
            'strategy_version':      strategy.get('version', 1),
            'confluence_min':        strategy.get('confluence', {}).get('min_valid_tfs', 3),
            'timeframes_configured': [tf[0] for tf in timeframes],
            'tf_availability':       tf_summary,
            'symbols_count':         len(symbols),
            'elapsed_seconds':       elapsed,
            'data_source':           'yfinance (Yahoo Finance)',
        },
        'trades':      all_trades,
        'stats':       stats,
        'diagnostics': diagnostics,
    }

    if export:
        try:
            folder = _export(result)
            result['report_folder'] = str(folder)
        except Exception as e:
            result['report_folder'] = None
            log.warning('[BT2] Export failed: %s', e)

    return result
