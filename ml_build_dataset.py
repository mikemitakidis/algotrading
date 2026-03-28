#!/usr/bin/env python3
"""
ml_build_dataset.py — M9 Phase 2: Automated ML dataset generation

Builds a large, clean, labeled ML dataset by running the exact live backtest
engine across many symbols and date windows. One command, repeatable, deterministic.

What this does:
  1. Defines a systematic grid of symbol groups × date windows
  2. Runs each combination through backtest_v2.run() — same engine as live strategy
  3. Collects all WIN/LOSS/TIMEOUT labeled trades
  4. Writes a single combined Parquet + summary JSON to data/ml/

What this does NOT do:
  - Does not invent signals or fake strategy logic
  - Does not use future-looking columns in the feature set
  - Does not trigger live orders or touch broker execution
  - Does not auto-retrain ml_train.py (generation and training are separate)

Output:
  data/ml/training_dataset.parquet   — full labeled dataset
  data/ml/training_dataset_summary.json  — manifest with counts, dates, features

Usage:
  python ml_build_dataset.py                    # full build
  python ml_build_dataset.py --mode append      # add new runs only
  python ml_build_dataset.py --mode overwrite   # rebuild from scratch (default)
  python ml_build_dataset.py --quick            # small subset for testing
  python ml_build_dataset.py --dry-run          # print plan, no fetches
"""

import argparse
import hashlib
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('ml_dataset')

BASE_DIR   = Path(__file__).resolve().parent
ML_DIR     = BASE_DIR / 'data' / 'ml'
CACHE_DIR  = BASE_DIR / 'data' / 'bt_v2_cache'

# ── Leakage-safe feature columns ─────────────────────────────────────────────
# These are the only columns that enter the ML feature matrix.
# All others (entry_price, stop_loss, return_pct, etc.) are metadata only.

ML_FEATURE_COLS = [
    'rsi', 'macd_hist', 'atr', 'bb_pos', 'vwap_dev',
    'vol_ratio', 'valid_count',
]
ENCODED_COLS = ['direction']          # encoded: long=1, short=0
LEAKAGE_COLS = {
    'entry_price', 'stop_loss', 'target_price',
    'return_pct', 'bars_held', 'exit_price',
    'tfs_triggered', 'outcome',
}

# ── Symbol groups — diversified, not random ───────────────────────────────────

SYMBOL_GROUPS = {
    'mega_tech':   ['AAPL', 'MSFT', 'NVDA', 'GOOGL', 'AMZN', 'META', 'TSLA'],
    'finance':     ['JPM', 'BAC', 'GS', 'MS', 'WFC', 'V', 'MA'],
    'healthcare':  ['UNH', 'JNJ', 'LLY', 'ABBV', 'MRK', 'TMO', 'ABT'],
    'energy':      ['XOM', 'CVX', 'COP', 'SLB', 'EOG', 'PXD', 'MPC'],
    'consumer':    ['WMT', 'HD', 'MCD', 'SBUX', 'NKE', 'LOW', 'TGT'],
    'semis':       ['AMD', 'INTC', 'QCOM', 'AVGO', 'TXN', 'MU', 'AMAT'],
    'mixed_large': ['AAPL', 'JPM', 'XOM', 'UNH', 'WMT', 'AMD', 'NFLX', 'BA', 'CAT', 'DE'],
}

# ── Date windows — cover multiple market regimes ──────────────────────────────
# Each window: (label, start, end)
# Windows chosen to cover bull, bear, volatile, and sideways periods.

def _build_date_windows() -> list:
    today    = date.today()
    windows  = []

    # Rolling 1-year windows going back 3 years
    for years_back in range(0, 3):
        end   = date(today.year - years_back, today.month, today.day)
        start = date(end.year - 1, end.month, end.day)
        if start < date(2020, 1, 1):
            break
        label = f'{start.year}_{end.year}_1y'
        windows.append((label, start.isoformat(), end.isoformat()))

    # Specific regime windows
    regime_windows = [
        ('covid_recovery_2020',   '2020-04-01', '2020-12-31'),
        ('bull_2021',             '2021-01-01', '2021-12-31'),
        ('bear_2022',             '2022-01-01', '2022-12-31'),
        ('recovery_2023',         '2023-01-01', '2023-12-31'),
        ('ai_bull_2024',          '2024-01-01', '2024-12-31'),
    ]
    for label, start, end in regime_windows:
        # Only include if within Alpaca's range
        if date.fromisoformat(start) >= date(2020, 1, 1):
            windows.append((label, start, end))

    # Deduplicate by (start, end)
    seen = set()
    unique = []
    for w in windows:
        key = (w[1], w[2])
        if key not in seen:
            seen.add(key)
            unique.append(w)

    return sorted(unique, key=lambda x: x[1])


# ── Run ID — deterministic hash for deduplication ────────────────────────────

def _run_id(symbols: list, start: str, end: str) -> str:
    key = f"{','.join(sorted(symbols))}|{start}|{end}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


# ── Single backtest run ───────────────────────────────────────────────────────

def run_one(symbols: list, start: str, end: str,
            window_label: str, group_label: str,
            dry_run: bool = False) -> list:
    """
    Run one backtest. Returns list of raw trade dicts with metadata added.
    Returns [] on failure or no data.
    """
    run_id = _run_id(symbols, start, end)
    log.info('[RUN] %s | %s | %s → %s (%d symbols)',
             run_id, window_label, start, end, len(symbols))

    if dry_run:
        return []

    try:
        from bot.backtest_v2 import run
        from bot.providers import get_provider_name
        result = run(symbols, start, end, export=False)
    except Exception as e:
        log.warning('[RUN] %s: exception — %s', run_id, str(e)[:80])
        return []

    if result.get('status') != 'ok':
        log.info('[RUN] %s: status=%s — no trades', run_id, result.get('status'))
        return []

    trades = result.get('trades', [])
    if not trades:
        log.info('[RUN] %s: 0 trades', run_id)
        return []

    provider = get_provider_name()

    # Enrich each trade with metadata
    enriched = []
    for t in trades:
        row = dict(t)
        row['run_id']       = run_id
        row['window_label'] = window_label
        row['group_label']  = group_label
        row['provider']     = provider
        row['generated_at'] = datetime.now(timezone.utc).isoformat()
        # tfs_triggered is a list — join to string for storage
        if isinstance(row.get('tfs_triggered'), list):
            row['tfs_triggered'] = '+'.join(row['tfs_triggered'])
        enriched.append(row)

    wins     = sum(1 for t in trades if t.get('outcome') == 'WIN')
    losses   = sum(1 for t in trades if t.get('outcome') == 'LOSS')
    timeouts = sum(1 for t in trades if t.get('outcome') == 'TIMEOUT')
    log.info('[RUN] %s: %d trades (W=%d L=%d T=%d)',
             run_id, len(trades), wins, losses, timeouts)
    return enriched


# ── Build plan ────────────────────────────────────────────────────────────────

def build_plan(quick: bool = False) -> list:
    """
    Build the list of (group_label, symbols, window_label, start, end) runs.
    quick=True uses a small subset for testing.
    """
    windows = _build_date_windows()
    plan    = []

    if quick:
        # Minimal plan: 2 groups × 2 windows = 4 runs
        groups  = {k: v for k, v in list(SYMBOL_GROUPS.items())[:2]}
        windows = windows[-2:]
    else:
        groups = SYMBOL_GROUPS

    for group_label, symbols in groups.items():
        for window_label, start, end in windows:
            plan.append((group_label, symbols, window_label, start, end))

    return plan


# ── Dataset assembly ──────────────────────────────────────────────────────────

def assemble_dataset(all_rows: list) -> pd.DataFrame:
    """Convert raw trade rows to clean DataFrame. No leakage columns in features."""
    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df = df.dropna(subset=['date', 'outcome']).sort_values('date').reset_index(drop=True)

    # Encode direction
    if 'direction' in df.columns:
        df['direction_enc'] = (df['direction'] == 'long').astype(int)

    # Binary label: WIN=1, LOSS=0, TIMEOUT=keep as metadata
    df['label'] = df['outcome'].map({'WIN': 1, 'LOSS': 0, 'TIMEOUT': -1})

    return df


# ── Summary manifest ──────────────────────────────────────────────────────────

def build_summary(df: pd.DataFrame, n_runs: int, plan_size: int) -> dict:
    outcome_counts = df['outcome'].value_counts().to_dict()
    return {
        'generated_at':    datetime.now(timezone.utc).isoformat(),
        'total_rows':      int(len(df)),
        'win_count':       int(outcome_counts.get('WIN',     0)),
        'loss_count':      int(outcome_counts.get('LOSS',    0)),
        'timeout_count':   int(outcome_counts.get('TIMEOUT', 0)),
        'trainable_rows':  int(outcome_counts.get('WIN', 0) + outcome_counts.get('LOSS', 0)),
        'symbols':         sorted(df['symbol'].unique().tolist()) if 'symbol' in df.columns else [],
        'symbol_count':    int(df['symbol'].nunique()) if 'symbol' in df.columns else 0,
        'date_range':      {
            'first': str(df['date'].min().date()) if len(df) else None,
            'last':  str(df['date'].max().date()) if len(df) else None,
        },
        'strategy_versions': sorted(df['strategy_version'].unique().tolist()) if 'strategy_version' in df.columns else [],
        'providers':       sorted(df['provider'].unique().tolist()) if 'provider' in df.columns else [],
        'window_labels':   sorted(df['window_label'].unique().tolist()) if 'window_label' in df.columns else [],
        'group_labels':    sorted(df['group_label'].unique().tolist()) if 'group_label' in df.columns else [],
        'ml_feature_cols': ML_FEATURE_COLS + ENCODED_COLS,
        'leakage_excluded': sorted(LEAKAGE_COLS),
        'runs_completed':  n_runs,
        'runs_planned':    plan_size,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='M9 Phase 2: ML dataset generator')
    parser.add_argument('--mode',    choices=['overwrite', 'append'], default='overwrite',
                        help='overwrite: rebuild from scratch | append: add new runs only')
    parser.add_argument('--quick',   action='store_true',
                        help='Run minimal subset (2 groups × 2 windows) for testing')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print plan without running backtests')
    parser.add_argument('--pause',   type=float, default=2.0,
                        help='Seconds to pause between backtest runs (default: 2)')
    args = parser.parse_args()

    ML_DIR.mkdir(parents=True, exist_ok=True)
    out_parquet = ML_DIR / 'training_dataset.parquet'
    out_summary = ML_DIR / 'training_dataset_summary.json'

    plan = build_plan(quick=args.quick)

    print('\n' + '='*65)
    print('ALGO TRADER  —  M9 PHASE 2: ML DATASET GENERATION')
    print('='*65)
    print(f'Mode:        {args.mode}')
    print(f'Runs planned:{len(plan)}')
    print(f'Groups:      {list(SYMBOL_GROUPS.keys()) if not args.quick else "quick subset"}')
    print(f'Output:      {out_parquet}')
    if args.dry_run:
        print('\n=== DRY RUN — plan only, no fetches ===')
        for i, (gl, syms, wl, start, end) in enumerate(plan, 1):
            print(f'  {i:>3}. [{gl}] {wl} | {start}→{end} | {len(syms)} symbols')
        print(f'\nTotal planned runs: {len(plan)}')
        return

    # Load existing dataset if appending
    existing_rows = []
    existing_run_ids = set()
    if args.mode == 'append' and out_parquet.exists():
        try:
            existing_df = pd.read_parquet(out_parquet)
            existing_rows = existing_df.to_dict('records')
            existing_run_ids = set(existing_df['run_id'].unique()) if 'run_id' in existing_df.columns else set()
            log.info('Append mode: loaded %d existing rows (%d run IDs)',
                     len(existing_rows), len(existing_run_ids))
        except Exception as e:
            log.warning('Could not load existing dataset: %s', e)

    all_rows    = list(existing_rows)
    n_runs      = 0
    n_skipped   = 0
    n_new_trades= 0
    t_start     = time.monotonic()

    for i, (group_label, symbols, window_label, start, end) in enumerate(plan, 1):
        run_id = _run_id(symbols, start, end)

        if run_id in existing_run_ids:
            log.info('[SKIP] %s already in dataset', run_id)
            n_skipped += 1
            continue

        print(f'\n[{i}/{len(plan)}] {group_label} | {window_label} | {start}→{end}')

        rows = run_one(symbols, start, end, window_label, group_label)
        all_rows.extend(rows)
        n_new_trades += len(rows)
        n_runs += 1

        # Save incrementally every 3 runs so progress isn't lost
        if n_runs % 3 == 0 and all_rows:
            df_tmp = assemble_dataset(all_rows)
            df_tmp.to_parquet(out_parquet, index=False)
            log.info('[SAVE] Checkpoint: %d rows saved', len(df_tmp))

        if args.pause > 0 and i < len(plan):
            time.sleep(args.pause)

    # Final assembly
    print('\n' + '─'*65)
    print('Assembling final dataset...')
    df = assemble_dataset(all_rows)

    if df.empty:
        log.error('Dataset is empty. Check provider config and strategy thresholds.')
        sys.exit(1)

    df.to_parquet(out_parquet, index=False)
    summary = build_summary(df, n_runs, len(plan))
    out_summary.write_text(json.dumps(summary, indent=2, default=str))

    elapsed = round(time.monotonic() - t_start)
    print(f'\n{"="*65}')
    print(f'DATASET COMPLETE')
    print(f'{"="*65}')
    print(f'  Total rows       : {summary["total_rows"]}')
    print(f'  Trainable W+L    : {summary["trainable_rows"]}')
    print(f'  WIN              : {summary["win_count"]}')
    print(f'  LOSS             : {summary["loss_count"]}')
    print(f'  TIMEOUT          : {summary["timeout_count"]}')
    print(f'  Symbols covered  : {summary["symbol_count"]}')
    print(f'  Date range       : {summary["date_range"]["first"]} → {summary["date_range"]["last"]}')
    print(f'  Runs completed   : {n_runs} ({n_skipped} skipped)')
    print(f'  New trades added : {n_new_trades}')
    print(f'  Elapsed          : {elapsed}s')
    print(f'  Saved to         : {out_parquet}')
    print(f'  Summary at       : {out_summary}')
    print(f'{"="*65}')
    print(f'\nTo train: python ml_train.py --dataset data/ml/training_dataset.parquet')


if __name__ == '__main__':
    main()
