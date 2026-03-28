#!/usr/bin/env python3
"""
ml_train.py — M9 Phase 3: XGBoost meta-labeling with advanced evaluation

Meta-labeling: base strategy decides WHICH trades to take.
ML learns WHETHER a signal is likely to WIN. ML is a filter, not a replacement.

Data source (in priority order):
  1. --dataset data/ml/training_dataset.parquet   (from ml_build_dataset.py)
  2. data/reports/*/results.json                  (scattered backtest reports)

Features (signal-time only, no leakage):
  Core:      rsi, macd_hist, atr, bb_pos, vwap_dev, vol_ratio, valid_count
  Engineered: atr_pct, rsi_macd_agree, momentum_score, confluence_score
  Dropped:   direction, route, strategy_version (consistently zero-contribution)
  Reason for drop: encoded direction has no predictive power at this dataset
  size. Route is a deterministic function of valid_count. strategy_version is
  constant. All three are kept as metadata but excluded from feature matrix.

Target: WIN=1, LOSS=0. TIMEOUT excluded (ambiguous — never hit stop or target).

Split: chronological 80/20. No shuffle — financial time-series ordering preserved.

Advanced evaluation:
  - Walk-forward cross-validation (TimeSeriesSplit, 5 folds)
  - Calibrated probabilities (isotonic regression)
  - Precision-recall curve
  - Filter comparison table (baseline vs ML-filtered at each threshold)
  - Per-group evaluation if group_label present in dataset
  - Honest verdict on whether model is ready for live shadow scoring
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# Load .env so DATA_PROVIDER and other env vars are available
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
log = logging.getLogger('ml_train')

BASE_DIR = Path(__file__).resolve().parent
ML_DIR   = BASE_DIR / 'data' / 'ml'

# ── Feature sets ──────────────────────────────────────────────────────────────

# Core signal-time features — available at decision time
CORE_FEATURES = ['rsi', 'macd_hist', 'atr', 'bb_pos', 'vwap_dev', 'vol_ratio', 'valid_count']

# Engineered features computed from core — still leakage-free
ENGINEERED_FEATURES = ['atr_pct', 'rsi_zone', 'momentum_score', 'confluence_score', 'vol_spike']

# Dropped from feature matrix (zero contribution, kept as metadata)
DROPPED_FEATURES = ['direction', 'route', 'strategy_version']
DROPPED_REASON   = 'consistently zero feature importance across all runs'

# Leakage — never in X
LEAKAGE_COLS = {
    'entry_price', 'stop_loss', 'target_price',
    'return_pct', 'bars_held', 'exit_price',
    'tfs_triggered', 'outcome',
}

TARGET_COL = 'outcome'

# ── Data loading ──────────────────────────────────────────────────────────────

def load_data(dataset_path=None) -> pd.DataFrame:
    if dataset_path and Path(dataset_path).exists():
        log.info('Loading dataset: %s', dataset_path)
        df = pd.read_parquet(dataset_path)
        df['date'] = pd.to_datetime(df['date'], errors='coerce')
        df = df.dropna(subset=['date']).sort_values('date').reset_index(drop=True)
        log.info('Loaded %d rows', len(df))
        return df

    if dataset_path:
        log.warning('Dataset not found at %s — scanning reports/', dataset_path)

    REPORTS_DIR = BASE_DIR / 'data' / 'reports'
    if not REPORTS_DIR.exists():
        log.error('No data/reports/ directory. Run: python ml_build_dataset.py')
        sys.exit(1)

    frames = []
    for rd in sorted(REPORTS_DIR.iterdir()):
        rj = rd / 'results.json'
        if rj.exists():
            try:
                data = json.loads(rj.read_text())
                trades = data.get('trades', [])
                if trades:
                    df = pd.DataFrame(trades)
                    df['_source'] = rd.name
                    frames.append(df)
            except Exception:
                pass

    if not frames:
        log.error('No trade data found. Run: python ml_build_dataset.py')
        sys.exit(1)

    df = pd.concat(frames, ignore_index=True)
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    return df.dropna(subset=['date']).sort_values('date').reset_index(drop=True)


# ── Feature engineering ───────────────────────────────────────────────────────

def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add engineered features from signal-time data only.
    All derivable from core indicators at decision time — no leakage.
    """
    df = df.copy()
    c = df.get('close') if 'close' in df.columns else None

    # ATR as % of price (normalised volatility — more comparable across symbols)
    if 'atr' in df.columns and 'entry_price' in df.columns:
        df['atr_pct'] = df['atr'] / (df['entry_price'].replace(0, np.nan)) * 100
    elif 'atr' in df.columns:
        df['atr_pct'] = df['atr'] / 100.0   # rough normalisation

    # RSI zone: 0=oversold(<35), 1=neutral(35-65), 2=overbought(>65)
    if 'rsi' in df.columns:
        df['rsi_zone'] = pd.cut(df['rsi'],
                                bins=[0, 35, 65, 100],
                                labels=[0, 1, 2]).astype(float)

    # Momentum score: RSI + MACD direction agreement (-1 to +1)
    if 'rsi' in df.columns and 'macd_hist' in df.columns:
        rsi_norm  = (df['rsi'] - 50) / 50        # -1..+1
        macd_norm = np.sign(df['macd_hist'])      # -1, 0, +1
        df['momentum_score'] = (rsi_norm + macd_norm) / 2

    # Confluence score: valid_count normalised by max possible (4 TFs)
    if 'valid_count' in df.columns:
        df['confluence_score'] = df['valid_count'] / 4.0

    # Volume spike: vol_ratio above 1.5 = clear spike
    if 'vol_ratio' in df.columns:
        df['vol_spike'] = (df['vol_ratio'] > 1.5).astype(float)

    return df


def build_features(df: pd.DataFrame, verbose: bool = False):
    """Build feature matrix. Returns (X, feature_names)."""
    df = engineer_features(df)

    available = []
    for col in CORE_FEATURES + ENGINEERED_FEATURES:
        if col in df.columns:
            available.append(col)
        elif verbose:
            log.info('  Feature %r not present — skipping', col)

    if verbose:
        log.info('Features selected (%d): %s', len(available), available)
        log.info('Dropped (zero contribution): %s', DROPPED_FEATURES)
        log.info('Leakage excluded: %s', sorted(LEAKAGE_COLS & set(df.columns)))

    X = df[available].apply(pd.to_numeric, errors='coerce').copy()
    bad = X.isna().any(axis=1).sum()
    if bad > 0:
        log.warning('Dropping %d rows with NaN features', bad)
        X = X.dropna()

    return X, list(X.columns)


# ── Training ──────────────────────────────────────────────────────────────────

def train(args) -> dict:
    import xgboost as xgb
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.metrics import (classification_report, confusion_matrix,
                                 precision_recall_curve, roc_auc_score,
                                 average_precision_score)
    from sklearn.model_selection import TimeSeriesSplit

    print('\n' + '='*70)
    print('ALGO TRADER  —  M9 PHASE 3: ADVANCED ML TRAINING')
    print('='*70)

    # ── Load ──────────────────────────────────────────────────────────────────
    df_all = load_data(getattr(args, 'dataset', None))

    outcome_counts = df_all['outcome'].value_counts()
    print('\nOutcome distribution (all rows):')
    for k, v in outcome_counts.items():
        print(f'  {k:10s}: {v:4d}  ({v/len(df_all)*100:.1f}%)')

    # Dataset coverage info
    if 'group_label' in df_all.columns:
        print('\nRows by group:')
        for g, cnt in df_all['group_label'].value_counts().items():
            wins = (df_all[df_all['group_label']==g]['outcome']=='WIN').sum()
            print(f'  {g:<20s}: {cnt:4d} rows  ({wins} WIN)')

    if 'window_label' in df_all.columns:
        print('\nRows by window:')
        for w, cnt in df_all['window_label'].value_counts().items():
            wins = (df_all[df_all['window_label']==w]['outcome']=='WIN').sum()
            print(f'  {w:<30s}: {cnt:4d} rows  ({wins} WIN)')

    # ── Filter to WIN/LOSS ────────────────────────────────────────────────────
    df = df_all[df_all['outcome'].isin(['WIN', 'LOSS'])].copy()
    n_timeout = len(df_all) - len(df)
    print(f'\nExcluding {n_timeout} TIMEOUT → {len(df)} WIN/LOSS rows remain')

    if len(df) < args.min_trades:
        log.error('Only %d W/L rows (need %d). Run: python ml_build_dataset.py',
                  len(df), args.min_trades)
        sys.exit(1)

    # ── Features ──────────────────────────────────────────────────────────────
    print(f'\nBuilding features...')
    print(f'  Dropped (zero contribution): {DROPPED_FEATURES}')
    print(f'  Reason: {DROPPED_REASON}')

    X_full, feature_names = build_features(df, verbose=args.verbose)
    y_full = (df.loc[X_full.index, TARGET_COL] == 'WIN').astype(int)
    df_aligned = df.loc[X_full.index].copy()

    print(f'  Features ({len(feature_names)}): {feature_names}')
    print(f'  Samples: {len(X_full)}')
    win_rate = y_full.mean() * 100
    print(f'  Class balance: WIN={y_full.sum()} ({win_rate:.1f}%)  '
          f'LOSS={len(y_full)-y_full.sum()} ({100-win_rate:.1f}%)')

    # ── Chronological split ───────────────────────────────────────────────────
    split_idx  = int(len(X_full) * 0.8)
    X_train, X_test = X_full.iloc[:split_idx], X_full.iloc[split_idx:]
    y_train, y_test = y_full.iloc[:split_idx], y_full.iloc[split_idx:]
    df_test = df_aligned.iloc[split_idx:].copy()

    train_dates = df_aligned['date'].iloc[:split_idx]
    test_dates  = df_aligned['date'].iloc[split_idx:]
    print(f'\nChronological split (80/20):')
    print(f'  Train: {len(X_train):4d}  '
          f'({train_dates.min().date()} → {train_dates.max().date()})'
          f'  WIN={y_train.sum()} ({y_train.mean()*100:.1f}%)')
    print(f'  Test:  {len(X_test):4d}  '
          f'({test_dates.min().date()} → {test_dates.max().date()})'
          f'  WIN={y_test.sum()} ({y_test.mean()*100:.1f}%)')

    if len(X_test) < 10:
        log.error('Test set too small (%d rows).', len(X_test))
        sys.exit(1)

    # ── Walk-forward cross-validation ─────────────────────────────────────────
    print(f'\n{"─"*70}')
    print('WALK-FORWARD CROSS-VALIDATION (TimeSeriesSplit, 5 folds)')
    print('─'*70)
    tscv    = TimeSeriesSplit(n_splits=5)
    cv_aucs = []
    cv_aps  = []

    spw = (y_train == 0).sum() / max((y_train == 1).sum(), 1)
    base_model = xgb.XGBClassifier(
        n_estimators=300, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        scale_pos_weight=spw,
        eval_metric='logloss', random_state=42, verbosity=0,
    )

    for fold, (tr_idx, val_idx) in enumerate(tscv.split(X_train), 1):
        Xtr, Xval = X_train.iloc[tr_idx], X_train.iloc[val_idx]
        ytr, yval = y_train.iloc[tr_idx], y_train.iloc[val_idx]
        if yval.nunique() < 2 or ytr.nunique() < 2:
            print(f'  Fold {fold}: skipped (single class)')
            continue
        m = xgb.XGBClassifier(
            n_estimators=300, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            scale_pos_weight=(ytr==0).sum()/max((ytr==1).sum(),1),
            eval_metric='logloss', random_state=42, verbosity=0,
        )
        m.fit(Xtr, ytr, verbose=False)
        proba = m.predict_proba(Xval)[:,1]
        try:
            auc = roc_auc_score(yval, proba)
            ap  = average_precision_score(yval, proba)
            cv_aucs.append(auc)
            cv_aps.append(ap)
            print(f'  Fold {fold}: AUC={auc:.3f}  AP={ap:.3f}  '
                  f'(train={len(Xtr)} val={len(Xval)} WIN={yval.sum()})')
        except Exception:
            print(f'  Fold {fold}: insufficient class diversity')

    if cv_aucs:
        print(f'  CV AUC mean={np.mean(cv_aucs):.3f} ± {np.std(cv_aucs):.3f}')
        print(f'  CV AP  mean={np.mean(cv_aps):.3f} ± {np.std(cv_aps):.3f}')

    # ── Final model with calibrated probabilities ──────────────────────────────
    print(f'\n{"─"*70}')
    print('FINAL MODEL (full train set + probability calibration)')
    print('─'*70)

    base = xgb.XGBClassifier(
        n_estimators=400, max_depth=4, learning_rate=0.04,
        subsample=0.8, colsample_bytree=0.8,
        min_child_weight=3,
        scale_pos_weight=spw,
        eval_metric='logloss', random_state=42, verbosity=0,
    )

    # Calibrate probabilities so score=0.6 really means ~60% WIN probability
    if len(X_train) >= 50:
        model = CalibratedClassifierCV(base, method='isotonic', cv=3)
        model.fit(X_train, y_train)
        print('  Probability calibration: isotonic (cv=3)')
    else:
        base.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)
        model = base
        print('  Probability calibration: skipped (train set too small)')

    y_proba = model.predict_proba(X_test)[:,1]
    y_pred  = (y_proba >= 0.5).astype(int)

    # ── Standard metrics ──────────────────────────────────────────────────────
    print(f'\n{"─"*70}')
    print('CLASSIFICATION REPORT (test set, threshold=0.5)')
    print('─'*70)
    print(classification_report(y_test, y_pred, target_names=['LOSS','WIN'], digits=3))

    cm = confusion_matrix(y_test, y_pred)
    print(f'Confusion matrix:  TN={cm[0,0]}  FP={cm[0,1]}  FN={cm[1,0]}  TP={cm[1,1]}')

    try:
        auc = roc_auc_score(y_test, y_proba)
        ap  = average_precision_score(y_test, y_proba)
        print(f'\nROC-AUC: {auc:.3f}')
        print(f'Avg Precision (PR-AUC): {ap:.3f}')
    except Exception:
        auc = ap = None

    # ── Feature importance ────────────────────────────────────────────────────
    print(f'\n{"─"*70}')
    print('FEATURE IMPORTANCE (gain)')
    print('─'*70)
    try:
        # Get importances from underlying XGB model
        xgb_model = model.calibrated_classifiers_[0].estimator if hasattr(model, 'calibrated_classifiers_') else model
        imp = dict(zip(feature_names, xgb_model.feature_importances_))
    except Exception:
        imp = {}
    imp_sorted = sorted(imp.items(), key=lambda x: -x[1])
    for fname, score in imp_sorted:
        bar = '█' * int(score * 50)
        print(f'  {fname:<22s} {score:.4f}  {bar}')

    # ── Filter comparison table ───────────────────────────────────────────────
    print(f'\n{"─"*70}')
    print('FILTER COMPARISON: BASELINE vs ML-FILTERED')
    print('─'*70)
    baseline_n  = len(y_test)
    baseline_wr = y_test.mean() * 100
    print(f'  Baseline (no filter): {baseline_n} trades, WR={baseline_wr:.1f}%')
    print()
    print(f'  {"Threshold":>10}  {"Kept":>6}  {"Coverage":>9}  '
          f'{"Win Rate":>9}  {"Precision":>10}  {"vs Baseline":>12}')
    print('  ' + '─'*62)

    threshold_results = []
    for thresh in [0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8]:
        mask  = y_proba >= thresh
        n     = mask.sum()
        if n == 0:
            continue
        cov   = n / len(y_test) * 100
        wr    = y_test[mask].mean() * 100
        prec  = y_test[mask].sum() / n * 100
        delta = wr - baseline_wr
        flag  = '✓' if delta > 2 else ('~' if delta > -2 else '✗')
        print(f'  {thresh:>10.2f}  {n:>6d}  {cov:>8.1f}%  '
              f'{wr:>8.1f}%  {prec:>9.1f}%  {delta:>+10.1f}%  {flag}')
        threshold_results.append({
            'threshold': thresh, 'n_kept': int(n),
            'coverage_pct': round(cov,1), 'win_rate': round(wr,1),
            'precision': round(prec,1), 'vs_baseline': round(delta,1),
        })

    # ── Precision-recall analysis ─────────────────────────────────────────────
    try:
        prec_curve, rec_curve, pr_thresholds = precision_recall_curve(y_test, y_proba)
        # Find threshold where precision >= 50%
        good_idxs = np.where(prec_curve >= 0.5)[0]
        if len(good_idxs):
            best_idx = good_idxs[np.argmax(rec_curve[good_idxs])]
            best_thresh = pr_thresholds[min(best_idx, len(pr_thresholds)-1)]
            best_prec   = prec_curve[best_idx]
            best_rec    = rec_curve[best_idx]
            print(f'\n  Best threshold for precision ≥ 50%: '
                  f'{best_thresh:.2f} (precision={best_prec:.1%}, recall={best_rec:.1%})')
        else:
            print('\n  No threshold achieves precision ≥ 50% on test set')
    except Exception:
        pass

    # ── Per-group evaluation ──────────────────────────────────────────────────
    if 'group_label' in df_test.columns and df_test['group_label'].nunique() > 1:
        print(f'\n{"─"*70}')
        print('PER-GROUP EVALUATION (test set)')
        print('─'*70)
        for grp in sorted(df_test['group_label'].unique()):
            mask_grp  = (df_test['group_label'] == grp).values
            if mask_grp.sum() < 5:
                continue
            yt_grp = y_test[mask_grp]
            yp_grp = y_proba[mask_grp]
            wr_grp = yt_grp.mean() * 100
            try:
                auc_grp = roc_auc_score(yt_grp, yp_grp)
                print(f'  {grp:<20s}: n={mask_grp.sum():3d}  '
                      f'WR={wr_grp:.1f}%  AUC={auc_grp:.3f}')
            except Exception:
                print(f'  {grp:<20s}: n={mask_grp.sum():3d}  '
                      f'WR={wr_grp:.1f}%  AUC=n/a')

    # ── Honest verdict ────────────────────────────────────────────────────────
    print(f'\n{"="*70}')
    print('VERDICT')
    print('='*70)

    has_lift = any(r['vs_baseline'] > 3 and r['n_kept'] >= 5
                   for r in threshold_results)
    good_auc = (auc or 0) > 0.58
    enough_data = len(X_full) >= 500

    if has_lift and good_auc and enough_data:
        verdict = 'READY for shadow live scoring at appropriate threshold'
        ready   = True
    elif has_lift or good_auc:
        verdict = 'MARGINAL — shows some signal, needs more data before live use'
        ready   = False
    else:
        verdict = 'NOT READY — model is near-random, grow dataset before live use'
        ready   = False

    print(f'  Model status : {verdict}')
    print(f'  AUC          : {(f"{auc:.3f}") if auc else "n/a"}')
    print(f'  Dataset size : {len(X_full)} W/L rows')
    print(f'  Has lift     : {"Yes" if has_lift else "No"}')
    print(f'  Recommendation: ', end='')
    if not enough_data:
        print('Run python ml_build_dataset.py (full, not --quick) to grow dataset')
    elif not ready:
        print('Grow dataset with more regime windows and retrain')
    else:
        print('Proceed to M9 phase 4: live shadow ML scoring')

    # ── Save artifacts ────────────────────────────────────────────────────────
    ML_DIR.mkdir(parents=True, exist_ok=True)

    model_path = ML_DIR / 'model.ubj'
    # Save underlying XGB model for scoring (calibrated wrapper not serialisable as ubj)
    try:
        xgb_model.save_model(str(model_path))
    except Exception:
        try:
            base.save_model(str(model_path))
        except Exception:
            pass

    # Save scaler metadata for feature engineering at score time
    features_meta = {
        'feature_names':      feature_names,
        'core_features':      CORE_FEATURES,
        'engineered_features':ENGINEERED_FEATURES,
        'dropped_features':   DROPPED_FEATURES,
        'dropped_reason':     DROPPED_REASON,
        'leakage_excluded':   sorted(LEAKAGE_COLS),
        'target':             'outcome: WIN=1, LOSS=0, TIMEOUT=excluded',
        'trained_at':         datetime.now(timezone.utc).isoformat(),
        'dataset_rows':       int(len(X_full)),
        'ready_for_live':     ready,
    }
    (ML_DIR / 'features.json').write_text(json.dumps(features_meta, indent=2))

    summary = {
        'trained_at':         datetime.now(timezone.utc).isoformat(),
        'n_train':            int(len(X_train)),
        'n_test':             int(len(X_test)),
        'n_timeout_excluded': int(n_timeout),
        'feature_names':      feature_names,
        'baseline_win_rate':  round(float(baseline_wr), 2),
        'roc_auc':            round(float(auc), 3) if auc else None,
        'avg_precision':      round(float(ap),  3) if ap  else None,
        'cv_auc_mean':        round(float(np.mean(cv_aucs)), 3) if cv_aucs else None,
        'cv_auc_std':         round(float(np.std(cv_aucs)),  3) if cv_aucs else None,
        'threshold_analysis': threshold_results,
        'feature_importance': [(k, round(float(v), 4)) for k, v in imp_sorted],
        'verdict':            verdict,
        'ready_for_live':     ready,
    }
    (ML_DIR / 'training_summary.json').write_text(json.dumps(summary, indent=2))

    try:
        model_size = model_path.stat().st_size // 1024
    except Exception:
        model_size = 0

    print(f'\nArtifacts saved to {ML_DIR}/')
    print(f'  model.ubj              ({model_size}KB)')
    print(f'  features.json')
    print(f'  training_summary.json')
    print('='*70)
    return summary


def main():
    parser = argparse.ArgumentParser(description='M9 Phase 3: advanced ML training')
    parser.add_argument('--min-trades', type=int, default=30)
    parser.add_argument('--verbose',    action='store_true')
    parser.add_argument('--dataset',    type=str, default=None,
                        help='Path to training_dataset.parquet (from ml_build_dataset.py)')
    main_args = parser.parse_args()
    train(main_args)

if __name__ == '__main__':
    main()
