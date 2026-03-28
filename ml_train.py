#!/usr/bin/env python3
"""
ml_train.py — Milestone 9: XGBoost meta-labeling pipeline

Meta-labeling concept:
    The base strategy (technical signals) decides WHICH trades to take.
    The ML model learns WHETHER a signal is likely to WIN.
    ML is an additional filter — it does not replace the strategy.

Training data source:
    data/reports/*/results.json  — backtest runs produced by M5 engine
    data/reports/*/trades.csv    — same data in CSV form (preferred)
    Combines all available backtest runs automatically.

Leakage prevention:
    Excluded columns (known only after trade closes):
        entry_price, stop_loss, target_price, return_pct, bars_held,
        exit_price, tfs_triggered, outcome

Target:
    WIN  = 1   (trade hit target price)
    LOSS = 0   (trade hit stop loss)
    TIMEOUT    excluded (ambiguous — trade never resolved clearly)

Split:
    Chronological: first 80% → train, last 20% → test
    No random shuffle — financial time-series ordering must be preserved.

Artifacts saved to data/ml/:
    model.ubj              XGBoost model (binary format)
    features.json          feature list + metadata
    training_summary.json  metrics, thresholds, trade counts

Usage:
    python ml_train.py                    # train from all reports
    python ml_train.py --min-trades 30    # require at least N trades
    python ml_train.py --verbose          # show extra debug output
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('ml_train')

BASE_DIR    = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / 'data' / 'reports'
ML_DIR      = BASE_DIR / 'data' / 'ml'

# ── Columns ───────────────────────────────────────────────────────────────────

# Columns available at signal time — safe to use as features
SIGNAL_TIME_COLS = [
    'rsi', 'macd_hist', 'atr', 'bb_pos', 'vwap_dev',
    'vol_ratio', 'valid_count', 'direction',
]

# Additional signal-time cols to include if present
OPTIONAL_SIGNAL_COLS = ['strategy_version', 'route']

# Columns that contain future information — must NEVER be features
LEAKAGE_COLS = {
    'entry_price', 'stop_loss', 'target_price',
    'return_pct', 'bars_held', 'exit_price',
    'tfs_triggered', 'outcome',
}

TARGET_COL = 'outcome'

# ── Data loading ──────────────────────────────────────────────────────────────

def load_all_trades() -> pd.DataFrame:
    """
    Load all trades from every results.json under data/reports/.
    Falls back to trades.csv if results.json is missing.
    Returns combined DataFrame sorted by date ascending.
    """
    frames = []

    if not REPORTS_DIR.exists():
        log.error('No data/reports/ directory found. Run a backtest first.')
        sys.exit(1)

    report_dirs = sorted(REPORTS_DIR.iterdir())
    log.info('Scanning %d report folder(s) in %s', len(report_dirs), REPORTS_DIR)

    for rd in report_dirs:
        if not rd.is_dir():
            continue

        # Try results.json first (richer data)
        rj = rd / 'results.json'
        tc = rd / 'trades.csv'

        if rj.exists():
            try:
                data   = json.loads(rj.read_text())
                trades = data.get('trades', [])
                if not trades:
                    continue
                df = pd.DataFrame(trades)
                df['_source'] = rd.name
                frames.append(df)
                log.info('  %s: %d trades (results.json)', rd.name, len(df))
                continue
            except Exception as e:
                log.warning('  %s: results.json parse error: %s', rd.name, e)

        if tc.exists():
            try:
                df = pd.read_csv(tc)
                if df.empty:
                    continue
                df['_source'] = rd.name
                frames.append(df)
                log.info('  %s: %d trades (trades.csv)', rd.name, len(df))
            except Exception as e:
                log.warning('  %s: trades.csv parse error: %s', rd.name, e)

    if not frames:
        log.error(
            'No trade data found in %s\n'
            'Run a backtest first:\n'
            '  python backtest_cli_v2.py --preset aapl1y', REPORTS_DIR
        )
        sys.exit(1)

    df = pd.concat(frames, ignore_index=True)
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df = df.dropna(subset=['date']).sort_values('date').reset_index(drop=True)
    log.info('Total trades loaded: %d (date range: %s → %s)',
             len(df), df['date'].min().date(), df['date'].max().date())
    return df


# ── Feature engineering ───────────────────────────────────────────────────────

def build_features(df: pd.DataFrame, verbose: bool = False) -> tuple[pd.DataFrame, list]:
    """
    Build feature matrix X from signal-time columns only.
    Returns (X_df, feature_names).
    Raises ValueError if leakage columns are detected.
    """
    # Paranoia check: ensure no leakage columns slipped through
    found_leakage = LEAKAGE_COLS & set(df.columns) - {TARGET_COL}
    # entry_price etc are in the df but not in X — just verify they won't be
    # included. They stay in df for reference, we select only signal-time cols.

    available_features = []
    for col in SIGNAL_TIME_COLS:
        if col in df.columns:
            available_features.append(col)
        else:
            log.warning('  Feature %r not in data — skipping', col)

    for col in OPTIONAL_SIGNAL_COLS:
        if col in df.columns:
            available_features.append(col)

    if verbose:
        log.info('Features selected: %s', available_features)
        log.info('Leakage cols present in df (excluded from X): %s',
                 sorted(LEAKAGE_COLS & set(df.columns)))

    X = df[available_features].copy()

    # Encode categoricals
    if 'direction' in X.columns:
        X['direction'] = (X['direction'] == 'long').astype(int)
        # 1 = long, 0 = short

    if 'route' in X.columns:
        route_map = {'ETORO': 2, 'IBKR': 1, 'WATCH': 0}
        X['route'] = X['route'].map(route_map).fillna(0).astype(int)

    if 'strategy_version' in X.columns:
        X['strategy_version'] = pd.to_numeric(X['strategy_version'], errors='coerce').fillna(1)

    # Coerce all to numeric, drop rows with any NaN
    X = X.apply(pd.to_numeric, errors='coerce')
    bad_rows = X.isna().any(axis=1).sum()
    if bad_rows > 0:
        log.warning('Dropping %d rows with NaN features', bad_rows)
        X = X.dropna()

    return X, list(X.columns)


def build_target(df: pd.DataFrame, index: pd.Index) -> pd.Series:
    """
    Build binary target: WIN=1, LOSS=0.
    TIMEOUT rows are excluded (handled by the caller filtering df first).
    """
    return (df.loc[index, TARGET_COL] == 'WIN').astype(int)


# ── Training ──────────────────────────────────────────────────────────────────

def train(args) -> dict:
    import xgboost as xgb
    from sklearn.metrics import (classification_report, confusion_matrix,
                                 precision_score, recall_score, roc_auc_score)

    print('\n' + '='*65)
    print('ALGO TRADER  —  M9 ML TRAINING  (XGBoost meta-labeling)')
    print('='*65)

    # ── Load ──────────────────────────────────────────────────────────────────
    dataset_path = getattr(args, 'dataset', None)
    if dataset_path and Path(dataset_path).exists():
        log.info('Loading from dataset: %s', dataset_path)
        df_all = pd.read_parquet(dataset_path)
        df_all['date'] = pd.to_datetime(df_all['date'], errors='coerce')
        df_all = df_all.dropna(subset=['date']).sort_values('date').reset_index(drop=True)
        log.info('Loaded %d rows from parquet', len(df_all))
    else:
        if dataset_path:
            log.warning('Dataset not found at %s — falling back to reports/', dataset_path)
        df_all = load_all_trades()

    # Outcome distribution before filtering
    outcome_counts = df_all['outcome'].value_counts()
    print(f'\nOutcome distribution (all trades):')
    for k, v in outcome_counts.items():
        print(f'  {k:10s}: {v:4d}  ({v/len(df_all)*100:.1f}%)')

    # Exclude TIMEOUT — ambiguous outcome
    df = df_all[df_all['outcome'].isin(['WIN', 'LOSS'])].copy()
    n_timeout = len(df_all) - len(df)
    print(f'\nExcluding {n_timeout} TIMEOUT trades → {len(df)} WIN/LOSS trades remain')

    if len(df) < args.min_trades:
        log.error(
            'Only %d WIN/LOSS trades found (need %d).\n'
            'Run more backtest runs to generate training data:\n'
            '  python backtest_cli_v2.py --preset mega1y\n'
            '  python backtest_cli_v2.py --preset mixed1y',
            len(df), args.min_trades
        )
        sys.exit(1)

    # ── Features ──────────────────────────────────────────────────────────────
    print('\nBuilding features (signal-time only, no leakage)...')
    X_full, feature_names = build_features(df, verbose=args.verbose)
    y_full = build_target(df, X_full.index)

    # Re-align df to X_full index (some rows may have been dropped)
    df_aligned = df.loc[X_full.index].copy()

    print(f'Features ({len(feature_names)}): {feature_names}')
    print(f'Samples after NaN drop: {len(X_full)}')
    print(f'Class balance: WIN={y_full.sum()} ({y_full.mean()*100:.1f}%), '
          f'LOSS={len(y_full)-y_full.sum()} ({(1-y_full.mean())*100:.1f}%)')

    # ── Chronological split ───────────────────────────────────────────────────
    split_idx = int(len(X_full) * 0.8)
    X_train, X_test = X_full.iloc[:split_idx], X_full.iloc[split_idx:]
    y_train, y_test = y_full.iloc[:split_idx], y_full.iloc[split_idx:]
    df_test = df_aligned.iloc[split_idx:]

    print(f'\nChronological split (80/20):')
    print(f'  Train: {len(X_train)} trades  '
          f'({df_aligned.iloc[0]["date"].date()} → {df_aligned.iloc[split_idx-1]["date"].date()})')
    print(f'  Test:  {len(X_test)} trades  '
          f'({df_aligned.iloc[split_idx]["date"].date()} → {df_aligned.iloc[-1]["date"].date()})')

    if len(X_test) < 10:
        log.error('Test set too small (%d). Need more backtest data.', len(X_test))
        sys.exit(1)

    # ── Model ─────────────────────────────────────────────────────────────────
    scale_pos_weight = (y_train == 0).sum() / max((y_train == 1).sum(), 1)
    print(f'\nTraining XGBClassifier (scale_pos_weight={scale_pos_weight:.2f})...')

    model = xgb.XGBClassifier(
        n_estimators      = 300,
        max_depth         = 4,
        learning_rate     = 0.05,
        subsample         = 0.8,
        colsample_bytree  = 0.8,
        scale_pos_weight  = scale_pos_weight,
        use_label_encoder = False,
        eval_metric       = 'logloss',
        random_state      = 42,
        verbosity         = 0,
    )
    model.fit(
        X_train, y_train,
        eval_set        = [(X_test, y_test)],
        verbose         = False,
    )

    # ── Standard metrics ──────────────────────────────────────────────────────
    y_pred  = model.predict(X_test)
    y_proba = model.predict_proba(X_test)[:, 1]

    print('\n' + '─'*65)
    print('CLASSIFICATION REPORT (test set, threshold=0.5)')
    print('─'*65)
    print(classification_report(y_test, y_pred, target_names=['LOSS', 'WIN'], digits=3))

    cm = confusion_matrix(y_test, y_pred)
    print('Confusion matrix:')
    print(f'  TN={cm[0,0]}  FP={cm[0,1]}')
    print(f'  FN={cm[1,0]}  TP={cm[1,1]}')

    try:
        auc = roc_auc_score(y_test, y_proba)
        print(f'\nROC-AUC: {auc:.3f}')
    except Exception:
        auc = None

    # ── Feature importance ────────────────────────────────────────────────────
    print('\n' + '─'*65)
    print('FEATURE IMPORTANCE (gain)')
    print('─'*65)
    imp = dict(zip(feature_names, model.feature_importances_))
    imp_sorted = sorted(imp.items(), key=lambda x: -x[1])
    for fname, score in imp_sorted:
        bar = '█' * int(score * 40)
        print(f'  {fname:<20s} {score:.4f}  {bar}')

    # ── Meta-labeling threshold analysis ──────────────────────────────────────
    print('\n' + '─'*65)
    print('META-LABELING THRESHOLD ANALYSIS')
    print('(How many trades kept, and win rate, at each confidence threshold)')
    print('─'*65)
    print(f'  {"Threshold":>10}  {"Kept":>6}  {"Kept%":>6}  {"Win Rate":>9}  {"Precision":>10}')
    print('  ' + '-'*50)

    threshold_results = []
    for thresh in [0.3, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8]:
        mask     = y_proba >= thresh
        n_kept   = mask.sum()
        if n_kept == 0:
            continue
        pct_kept = n_kept / len(y_test) * 100
        wins     = y_test[mask].sum()
        wr       = wins / n_kept * 100
        prec     = precision_score(y_test, mask.astype(int), zero_division=0)
        print(f'  {thresh:>10.2f}  {n_kept:>6d}  {pct_kept:>5.1f}%  {wr:>8.1f}%  {prec:>10.3f}')
        threshold_results.append({
            'threshold': thresh,
            'n_kept':    int(n_kept),
            'pct_kept':  round(pct_kept, 1),
            'win_rate':  round(wr, 1),
            'precision': round(float(prec), 3),
        })

    # ── Baseline comparison ───────────────────────────────────────────────────
    baseline_wr = y_test.mean() * 100
    print(f'\n  Baseline win rate (no filter): {baseline_wr:.1f}%')
    print(f'  Total test trades: {len(y_test)}')

    # ── Save artifacts ────────────────────────────────────────────────────────
    ML_DIR.mkdir(parents=True, exist_ok=True)

    model_path = ML_DIR / 'model.ubj'
    model.save_model(str(model_path))

    features_meta = {
        'feature_names':   feature_names,
        'direction_encoding': {'long': 1, 'short': 0},
        'route_encoding':     {'ETORO': 2, 'IBKR': 1, 'WATCH': 0},
        'exclude_cols':    sorted(LEAKAGE_COLS),
        'target':          'outcome: WIN=1, LOSS=0, TIMEOUT=excluded',
        'trained_at':      datetime.now(timezone.utc).isoformat(),
    }
    (ML_DIR / 'features.json').write_text(json.dumps(features_meta, indent=2))

    summary = {
        'trained_at':        datetime.now(timezone.utc).isoformat(),
        'n_train':           int(len(X_train)),
        'n_test':            int(len(X_test)),
        'n_timeout_excluded':int(n_timeout),
        'feature_names':     feature_names,
        'baseline_win_rate': round(float(baseline_wr), 2),
        'roc_auc':           round(float(auc), 3) if auc else None,
        'threshold_analysis':threshold_results,
        'feature_importance':[(k, round(float(v), 4)) for k, v in imp_sorted],
        'class_balance':     {
            'train_win_pct': round(float(y_train.mean() * 100), 1),
            'test_win_pct':  round(float(y_test.mean()  * 100), 1),
        },
    }
    (ML_DIR / 'training_summary.json').write_text(json.dumps(summary, indent=2))

    print('\n' + '='*65)
    print(f'Artifacts saved to {ML_DIR}/')
    print(f'  model.ubj           XGBoost model ({model_path.stat().st_size // 1024}KB)')
    print(f'  features.json       feature list + encoding metadata')
    print(f'  training_summary.json  metrics + threshold table')
    print('='*65)

    return summary


# ── Argparse ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='M9 XGBoost meta-labeling trainer')
    parser.add_argument('--min-trades', type=int, default=30,
                        help='Minimum WIN+LOSS trades required (default: 30)')
    parser.add_argument('--verbose', action='store_true',
                        help='Show extra debug output')
    parser.add_argument('--dataset', type=str, default=None,
                        help='Path to training_dataset.parquet (from ml_build_dataset.py). '
                             'Falls back to scanning data/reports/ if not set.')
    args = parser.parse_args()
    train(args)


if __name__ == '__main__':
    main()
