#!/usr/bin/env python3
"""
ml_build_dataset.py — M9 Phase 3: ML dataset generation (revised)

ROOT CAUSE OF PREVIOUS APPROACH FAILURE:
  When Alpaca provides 1D+4H+1H+15m (4 TFs), the strategy requires min_valid=3.
  This fires very rarely. The 2025-2026 recent window only has 3 TFs
  (15m blocked by Alpaca SIP) so min_valid=2, which fires much more often.
  Running 56 historical windows with 4 TFs active → near-zero trades.

CORRECT APPROACH (this script):
  Run ALL 89 live focus symbols in batches of 10 across MULTIPLE overlapping
  date windows within the working regime (recent 1D+4H+1H, min_valid=2).
  More symbols × more overlapping windows = more labeled trades.
  Strategy logic is UNCHANGED. No confluence relaxation.

Plan:
  9 symbol batches (full 89-symbol focus list) × N date windows
  Date windows: recent rolling windows where the 3-TF regime fires

Output:
  data/ml/training_dataset.parquet
  data/ml/training_dataset_summary.json

Modes:
  python ml_build_dataset.py              # full build
  python ml_build_dataset.py --mode append    # add new runs only
  python ml_build_dataset.py --quick     # 2 batches × 2 windows
  python ml_build_dataset.py --dry-run   # print plan only
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

# Load .env before any bot imports so DATA_PROVIDER etc. are set
from pathlib import Path as _Path
_env = _Path(__file__).resolve().parent / '.env'
if _env.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env)
    except ImportError:
        pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('ml_dataset')

BASE_DIR = Path(__file__).resolve().parent
ML_DIR   = BASE_DIR / 'data' / 'ml'

# ── Full focus list in batches of 10 (same as live bot) ───────────────────────

def _get_symbol_batches() -> list:
    from bot.focus import FOCUS_SYMBOLS
    return [FOCUS_SYMBOLS[i:i+10] for i in range(0, len(FOCUS_SYMBOLS), 10)]


# ── Date windows — overlapping recent windows where strategy fires ─────────────
# These windows use the 1D+4H+1H regime (15m blocked by Alpaca SIP for recent data)
# which gives min_valid=2 and actually fires signals.

def _build_date_windows() -> list:
    today   = date.today()
    windows = []

    # Overlapping 6-month windows going back 18 months
    for months_back in range(0, 19, 3):   # 0, 3, 6, 9, 12, 15, 18
        end_offset   = timedelta(days=months_back * 30)
        start_offset = timedelta(days=(months_back + 6) * 30)
        end_date     = today - end_offset
        start_date   = today - start_offset
        if start_date < date(2023, 1, 1):
            break
        label = f'{start_date.strftime("%Y%m")}_to_{end_date.strftime("%Y%m")}'
        windows.append((label, start_date.isoformat(), end_date.isoformat()))

    # Also add a full 18-month window for maximum coverage
    windows.append((
        'full_18mo',
        (today - timedelta(days=540)).isoformat(),
        today.isoformat(),
    ))

    return windows


# ── Run ID ────────────────────────────────────────────────────────────────────

def _run_id(symbols: list, start: str, end: str) -> str:
    key = f"{','.join(sorted(symbols))}|{start}|{end}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


# ── Single backtest run ───────────────────────────────────────────────────────

def run_one(symbols: list, start: str, end: str,
            window_label: str, batch_idx: int,
            dry_run: bool = False) -> list:
    run_id = _run_id(symbols, start, end)
    log.info('[RUN] %s | batch_%02d | %s→%s (%d syms)',
             run_id, batch_idx, start, end, len(symbols))
    if dry_run:
        return []

    try:
        from bot.backtest_v2 import run
        from bot.providers import get_provider_name
        result = run(symbols, start, end, export=False)
    except Exception as e:
        log.warning('[RUN] %s: exception — %s', run_id, str(e)[:100])
        return []

    if result.get('status') != 'ok':
        log.info('[RUN] %s: status=%s', run_id, result.get('status'))
        return []

    trades = result.get('trades', [])
    if not trades:
        return []

    provider = get_provider_name()
    enriched = []
    for t in trades:
        row = dict(t)
        row['run_id']       = run_id
        row['window_label'] = window_label
        row['batch_idx']    = batch_idx
        row['provider']     = provider
        row['generated_at'] = datetime.now(timezone.utc).isoformat()
        if isinstance(row.get('tfs_triggered'), list):
            row['tfs_triggered'] = '+'.join(row['tfs_triggered'])
        enriched.append(row)

    wins     = sum(1 for t in trades if t.get('outcome') == 'WIN')
    losses   = sum(1 for t in trades if t.get('outcome') == 'LOSS')
    timeouts = sum(1 for t in trades if t.get('outcome') == 'TIMEOUT')
    log.info('[RUN] %s: %d trades  W=%d L=%d T=%d',
             run_id, len(trades), wins, losses, timeouts)
    return enriched


# ── Build plan ────────────────────────────────────────────────────────────────

def build_plan(quick: bool = False) -> list:
    batches = _get_symbol_batches()
    windows = _build_date_windows()

    if quick:
        batches = batches[:2]
        windows = windows[:2]

    plan = []
    for i, symbols in enumerate(batches):
        for window_label, start, end in windows:
            plan.append((i+1, symbols, window_label, start, end))
    return plan


# ── Assembly + summary ────────────────────────────────────────────────────────

def assemble_dataset(all_rows: list) -> pd.DataFrame:
    if not all_rows:
        return pd.DataFrame()
    df = pd.DataFrame(all_rows)
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df = df.dropna(subset=['date', 'outcome']).sort_values('date').reset_index(drop=True)
    if 'direction' in df.columns:
        df['direction_enc'] = (df['direction'] == 'long').astype(int)
    df['label'] = df['outcome'].map({'WIN': 1, 'LOSS': 0, 'TIMEOUT': -1})
    return df


def build_summary(df: pd.DataFrame, n_runs: int, n_zero: int, plan_size: int) -> dict:
    oc = df['outcome'].value_counts().to_dict()
    return {
        'generated_at':   datetime.now(timezone.utc).isoformat(),
        'total_rows':     int(len(df)),
        'win_count':      int(oc.get('WIN',     0)),
        'loss_count':     int(oc.get('LOSS',    0)),
        'timeout_count':  int(oc.get('TIMEOUT', 0)),
        'trainable_rows': int(oc.get('WIN', 0) + oc.get('LOSS', 0)),
        'symbols':        sorted(df['symbol'].unique().tolist()) if 'symbol' in df.columns else [],
        'symbol_count':   int(df['symbol'].nunique()) if 'symbol' in df.columns else 0,
        'date_range': {
            'first': str(df['date'].min().date()),
            'last':  str(df['date'].max().date()),
        },
        'window_labels':   sorted(df['window_label'].unique().tolist()) if 'window_label' in df.columns else [],
        'ml_feature_cols': ['rsi','macd_hist','atr','bb_pos','vwap_dev','vol_ratio','valid_count',
                            'atr_pct','rsi_zone','momentum_score','confluence_score','vol_spike'],
        'leakage_excluded':['entry_price','stop_loss','target_price','return_pct',
                            'bars_held','exit_price','tfs_triggered','outcome'],
        'runs_completed':  n_runs,
        'runs_zero_trades':n_zero,
        'runs_planned':    plan_size,
        'providers':       sorted(df['provider'].unique().tolist()) if 'provider' in df.columns else [],
        'approach':        '89 focus symbols in batches of 10 × overlapping 6-month windows',
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='M9 Phase 3: ML dataset generator')
    parser.add_argument('--mode',    choices=['overwrite','append'], default='overwrite')
    parser.add_argument('--quick',   action='store_true', help='2 batches × 2 windows')
    parser.add_argument('--dry-run', action='store_true', help='Print plan, no fetches')
    parser.add_argument('--pause',   type=float, default=1.0,
                        help='Seconds between runs (default: 1.0)')
    args = parser.parse_args()

    ML_DIR.mkdir(parents=True, exist_ok=True)
    out_parquet = ML_DIR / 'training_dataset.parquet'
    out_summary = ML_DIR / 'training_dataset_summary.json'

    plan = build_plan(quick=args.quick)

    print('\n' + '='*70)
    print('ALGO TRADER  —  M9 PHASE 3: ML DATASET GENERATION')
    print('='*70)
    print(f'Mode         : {args.mode}')
    print(f'Runs planned : {len(plan)}  '
          f'({len(_get_symbol_batches()) if not args.quick else 2} batches × '
          f'{len(_build_date_windows()) if not args.quick else 2} windows)')
    print(f'Approach     : all 89 focus symbols in batches × recent overlapping windows')
    print(f'Rationale    : recent windows use 1D+4H+1H (min_valid=2) — '
          f'same regime that produced 216 trades from mixed1y')
    print(f'Output       : {out_parquet}')

    if args.dry_run:
        print('\n=== DRY RUN — plan only ===')
        windows = _build_date_windows() if not args.quick else _build_date_windows()[:2]
        batches = _get_symbol_batches()[:2 if args.quick else 99]
        print(f'\nDate windows ({len(windows)}):')
        for label, start, end in windows:
            print(f'  {label:<35} {start} → {end}')
        print(f'\nSymbol batches ({len(batches)}):')
        for i, b in enumerate(batches):
            print(f'  Batch {i+1:02d}: {", ".join(b)}')
        print(f'\nTotal runs: {len(plan)}')
        return

    # Load existing if appending
    existing_rows   = []
    existing_run_ids= set()
    if args.mode == 'append' and out_parquet.exists():
        try:
            existing_df      = pd.read_parquet(out_parquet)
            existing_rows    = existing_df.to_dict('records')
            existing_run_ids = set(existing_df.get('run_id', pd.Series()).unique())
            log.info('Append: loaded %d existing rows (%d run IDs)',
                     len(existing_rows), len(existing_run_ids))
        except Exception as e:
            log.warning('Could not load existing dataset: %s', e)

    all_rows = list(existing_rows)
    n_runs = n_zero = n_new = 0
    run_log = []
    t_start = time.monotonic()

    for i, (batch_idx, symbols, window_label, start, end) in enumerate(plan, 1):
        run_id = _run_id(symbols, start, end)
        if run_id in existing_run_ids:
            log.info('[SKIP] %s already done', run_id)
            continue

        pct = i / len(plan) * 100
        elapsed_so_far = round(time.monotonic() - t_start)
        print(f'\n[{i}/{len(plan)} {pct:.0f}%  {elapsed_so_far}s]  '
              f'batch_{batch_idx:02d} | {window_label}')

        rows = run_one(symbols, start, end, window_label, batch_idx)
        all_rows.extend(rows)
        n_new  += len(rows)
        n_runs += 1
        if not rows:
            n_zero += 1

        wins = sum(1 for r in rows if r.get('outcome') == 'WIN')
        losses = sum(1 for r in rows if r.get('outcome') == 'LOSS')
        run_log.append({
            'batch': batch_idx, 'window': window_label,
            'start': start, 'end': end,
            'trades': len(rows), 'wins': wins, 'losses': losses,
        })

        # Checkpoint every 3 runs
        if n_runs % 3 == 0 and all_rows:
            df_tmp = assemble_dataset(all_rows)
            df_tmp.to_parquet(out_parquet, index=False)
            log.info('[SAVE] Checkpoint: %d rows total', len(df_tmp))

        if args.pause > 0 and i < len(plan):
            time.sleep(args.pause)

    # Coverage report
    if run_log:
        print(f'\n{"─"*70}')
        print('PER-RUN COVERAGE REPORT')
        print('─'*70)
        print(f'  {"Batch":>6}  {"Window":<35}  {"Trades":>7}  {"W":>4}  {"L":>4}')
        print('  ' + '─'*60)
        for r in run_log:
            flag = '  ← 0 trades' if r['trades'] == 0 else ''
            print(f'  {r["batch"]:>6}  {r["window"]:<35}  '
                  f'{r["trades"]:>7}  {r["wins"]:>4}  {r["losses"]:>4}{flag}')
        print(f'  Zero-trade runs: {n_zero}/{n_runs}  '
              f'({n_zero/max(n_runs,1)*100:.0f}%)')

    # Final dataset
    print(f'\n{"─"*70}')
    df = assemble_dataset(all_rows)
    if df.empty:
        log.error('Dataset empty. Check provider and strategy config.')
        sys.exit(1)

    df.to_parquet(out_parquet, index=False)
    summary = build_summary(df, n_runs, n_zero, len(plan))
    out_summary.write_text(json.dumps(summary, indent=2, default=str))

    elapsed = round(time.monotonic() - t_start)
    print(f'\n{"="*70}')
    print('DATASET COMPLETE')
    print('='*70)
    print(f'  Total rows       : {summary["total_rows"]}')
    print(f'  Trainable (W+L)  : {summary["trainable_rows"]}')
    print(f'  WIN              : {summary["win_count"]}')
    print(f'  LOSS             : {summary["loss_count"]}')
    print(f'  TIMEOUT          : {summary["timeout_count"]}')
    print(f'  Symbols covered  : {summary["symbol_count"]}')
    print(f'  Date range       : {summary["date_range"]["first"]} → {summary["date_range"]["last"]}')
    print(f'  Runs completed   : {n_runs}  (zero-trade: {n_zero})')
    print(f'  New trades added : {n_new}')
    print(f'  Elapsed          : {elapsed}s')
    print(f'  Saved to         : {out_parquet}')
    print(f'\nTo train: python ml_train.py --dataset data/ml/training_dataset.parquet')


if __name__ == '__main__':
    main()
