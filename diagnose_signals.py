#!/usr/bin/env python3
"""
diagnose_signals.py — Signal frequency formal diagnosis.

Answers exactly:
1. Signal frequency over 7/14/30 days
2. Candidate funnel: scanned → partial_confluence → final_signal
3. Which filters kill the most candidates
4. Per-TF pass rates
5. Effect of min_valid logic
6. Effect of route thresholds
7. Cooldown suppression effect
8. Symbol concentration (close-but-never-pass)
9. Regime context
10. Ranked recommendations

Run on the server: python diagnose_signals.py
"""
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
_env = BASE_DIR / '.env'
if _env.exists():
    try:
        from dotenv import load_dotenv; load_dotenv(_env)
    except ImportError:
        pass
sys.path.insert(0, str(BASE_DIR))

DB_PATH = BASE_DIR / 'data' / 'signals.db'


def hr(char='─', w=65):
    print(char * w)

def section(title):
    print()
    hr('═')
    print(f'  {title}')
    hr('═')


def get_conn():
    if not DB_PATH.exists():
        print(f'ERROR: DB not found at {DB_PATH}')
        sys.exit(1)
    return sqlite3.connect(str(DB_PATH))


def run():
    conn = get_conn()
    now  = datetime.now(timezone.utc)
    today = now.date()

    # ── Load strategy config ──────────────────────────────────────────────────
    strategy_path = BASE_DIR / 'data' / 'strategy.json'
    if strategy_path.exists():
        strategy = json.loads(strategy_path.read_text())
    else:
        from bot.strategy import DEFAULTS as strategy
    confluence    = strategy.get('confluence', {})
    cfg_min       = int(confluence.get('min_valid_tfs', 3))
    routing       = strategy.get('routing', {})
    etoro_min     = int(routing.get('etoro_min_tfs', 4))
    ibkr_min      = int(routing.get('ibkr_min_tfs',  2))
    long_cfg      = strategy.get('long', {})
    short_cfg     = strategy.get('short', {})
    cooldown_secs = int(os.getenv('TELEGRAM_COOLDOWN_SECS', 14400))

    print('\n' + '='*65)
    print('  SIGNAL FREQUENCY DIAGNOSIS')
    print(f'  Generated: {now.strftime("%Y-%m-%d %H:%M UTC")}')
    print('='*65)

    # ── Strategy config snapshot ──────────────────────────────────────────────
    section('STRATEGY CONFIG SNAPSHOT')
    print(f'  confluence.min_valid_tfs : {cfg_min}')
    print(f'  routing.etoro_min_tfs    : {etoro_min}  (all TFs must agree)')
    print(f'  routing.ibkr_min_tfs     : {ibkr_min}  (minimum to emit signal)')
    print(f'  telegram_cooldown_secs   : {cooldown_secs} ({cooldown_secs//3600}h)')
    print()
    print('  Long thresholds:')
    for k, v in long_cfg.items():
        print(f'    {k:<25} = {v}')
    print('  Short thresholds:')
    for k, v in short_cfg.items():
        print(f'    {k:<25} = {v}')

    # ── 1. Signal frequency ───────────────────────────────────────────────────
    section('1. SIGNAL FREQUENCY (DB signals table)')
    for label, days in [('Last 7 days', 7), ('Last 14 days', 14), ('Last 30 days', 30), ('All time', 9999)]:
        since = (today - timedelta(days=days)).isoformat() if days < 9999 else '2000-01-01'
        try:
            rows = conn.execute(
                "SELECT COUNT(*), direction FROM signals WHERE timestamp >= ? GROUP BY direction",
                (since,)
            ).fetchall()
            total = sum(r[0] for r in rows)
            breakdown = ', '.join(f'{r[1]}={r[0]}' for r in rows) or 'none'
            print(f'  {label:<20}: {total:4d} signals  ({breakdown})')
        except Exception as e:
            print(f'  {label}: ERROR — {e}')

    # ── 2. Candidate funnel ───────────────────────────────────────────────────
    section('2. CANDIDATE FUNNEL (candidate_snapshots)')
    try:
        stages = conn.execute(
            """SELECT stage, COUNT(*) as cnt
               FROM candidate_snapshots
               WHERE signal_id NOT IN (888888,999999)
               GROUP BY stage ORDER BY cnt DESC"""
        ).fetchall()
        total_snaps = sum(r[1] for r in stages)
        print(f'  Total snapshots (excl test rows): {total_snaps}')
        print()
        for stage, cnt in stages:
            pct = cnt/max(total_snaps,1)*100
            bar = '█' * int(pct/2)
            print(f'  {stage:<22}: {cnt:5d}  ({pct:5.1f}%)  {bar}')

        # Per-cycle average
        cycles = conn.execute(
            "SELECT COUNT(DISTINCT cycle_id) FROM candidate_snapshots "
            "WHERE signal_id NOT IN (888888,999999)"
        ).fetchone()[0]
        if cycles > 0:
            print(f'\n  Cycles in DB: {cycles}')
            for stage, cnt in stages:
                print(f'  {stage:<22}: {cnt/cycles:.1f} per cycle avg')
    except Exception as e:
        print(f'  ERROR: {e}')

    # ── 3. Which filters kill candidates ─────────────────────────────────────
    section('3. REJECTION BREAKDOWN')
    try:
        reasons = conn.execute(
            """SELECT rejection_reason, COUNT(*) as cnt
               FROM candidate_snapshots
               WHERE stage='partial_confluence'
               AND signal_id NOT IN (888888,999999)
               GROUP BY rejection_reason ORDER BY cnt DESC LIMIT 15"""
        ).fetchall()
        print(f'  {"Rejection reason":<35} {"Count":>6}  {"% of partial":>12}')
        hr()
        total_partial = sum(r[1] for r in reasons)
        for reason, cnt in reasons:
            pct = cnt/max(total_partial,1)*100
            print(f'  {(reason or "none"):<35} {cnt:>6}  {pct:>11.1f}%')
    except Exception as e:
        print(f'  ERROR: {e}')

    # ── 4. Per-TF pass rates from candidate snapshots ─────────────────────────
    section('4. PER-TIMEFRAME PASS RATES')
    try:
        # Count how many candidate rows mention each TF in tfs_passing
        tf_counts = Counter()
        tf_totals = Counter()
        rows = conn.execute(
            "SELECT tfs_passing, valid_count, available_tfs FROM candidate_snapshots "
            "WHERE signal_id NOT IN (888888,999999)"
        ).fetchall()
        for tfs_str, valid_count, avail in rows:
            tfs = tfs_str.split('+') if tfs_str else []
            for tf in ('1D', '4H', '1H', '15m'):
                tf_totals[tf] += 1
                if tf in tfs:
                    tf_counts[tf] += 1

        # Also check signals table for TF breakdown
        sig_tfs = {}
        for tf_col, label in [('tf_1d','1D'),('tf_4h','4H'),('tf_1h','1H'),('tf_15m','15m')]:
            try:
                r = conn.execute(f"SELECT SUM({tf_col}), COUNT(*) FROM signals").fetchone()
                sig_tfs[label] = (r[0] or 0, r[1] or 0)
            except Exception:
                pass

        print(f'  From candidate_snapshots (symbols with ≥1 TF score):')
        for tf in ('1D', '4H', '1H', '15m'):
            total = tf_totals[tf]
            passed = tf_counts[tf]
            rate = passed/max(total,1)*100
            bar = '█' * int(rate/5)
            print(f'    {tf:<6}: {passed:4d}/{total:4d} appear in TF scores ({rate:.1f}%)  {bar}')

        if sig_tfs:
            print(f'\n  From signals table (TF contribution to final signals):')
            for label, (hits, total) in sig_tfs.items():
                rate = hits/max(total,1)*100
                print(f'    {label:<6}: present in {hits}/{total} signals ({rate:.0f}%)')
    except Exception as e:
        print(f'  ERROR: {e}')

    # ── 5. min_valid effect ───────────────────────────────────────────────────
    section('5. MIN_VALID THRESHOLD EFFECT')
    try:
        counts = conn.execute(
            """SELECT valid_count, COUNT(*) as cnt
               FROM candidate_snapshots
               WHERE signal_id NOT IN (888888,999999)
               GROUP BY valid_count ORDER BY valid_count"""
        ).fetchall()
        vc_map = {r[0]: r[1] for r in counts}
        total = sum(vc_map.values())

        print(f'  Current cfg_min: {cfg_min}  (with 4 TFs available → min_valid={cfg_min})')
        print(f'  With 3 TFs available → min_valid=max(2,{cfg_min}-1)={max(2,cfg_min-1)}')
        print()
        print(f'  {"valid_count":<14} {"Candidates":>10}  {"% of all":>9}  {"Fires signal?":>14}')
        hr()
        for vc in sorted(vc_map.keys()):
            cnt = vc_map[vc]
            pct = cnt/max(total,1)*100
            fires = '✓ YES' if vc >= cfg_min else f'✗ NO  (need {cfg_min})'
            print(f'  {vc:<14} {cnt:>10}  {pct:>8.1f}%  {fires}')

        # How many more signals if min_valid=2?
        at_1 = vc_map.get(1, 0)
        at_2 = vc_map.get(2, 0)
        at_3 = vc_map.get(3, 0)
        at_4 = vc_map.get(4, 0)
        print()
        print(f'  Signals that would fire at min_valid=2: {at_2+at_3+at_4}')
        print(f'  Signals that would fire at min_valid=3: {at_3+at_4}')
        print(f'  Signals that would fire at min_valid=4: {at_4}')
        print(f'  Currently blocked by min_valid={cfg_min}: {at_1+at_2} candidates')
    except Exception as e:
        print(f'  ERROR: {e}')

    # ── 6. Route threshold effect ─────────────────────────────────────────────
    section('6. ROUTE THRESHOLD EFFECT')
    try:
        sig_routes = conn.execute(
            "SELECT route, COUNT(*) FROM signals GROUP BY route ORDER BY COUNT(*) DESC"
        ).fetchall()
        print(f'  Signals by route:')
        for route, cnt in sig_routes:
            print(f'    {route:<10}: {cnt}')
        print()
        print(f'  Route logic:')
        print(f'    valid_count >= {etoro_min} → ETORO  (requires ALL {etoro_min} TFs — very rare)')
        print(f'    valid_count >= {ibkr_min}  → IBKR   (minimum to emit signal)')
        print(f'    valid_count <  {ibkr_min}  → WATCH  (NOT emitted as signal)')
        print()
        print(f'  Note: WATCH route requires valid_count >= ibkr_min anyway to emit.')
        print(f'  Signals below ibkr_min={ibkr_min} are silently dropped before routing.')
    except Exception as e:
        print(f'  ERROR: {e}')

    # ── 7. Cooldown suppression ───────────────────────────────────────────────
    section('7. COOLDOWN SUPPRESSION EFFECT')
    try:
        # Find signals that fired within cooldown window of a previous same signal
        all_sigs = conn.execute(
            "SELECT symbol, direction, timestamp FROM signals ORDER BY timestamp"
        ).fetchall()
        last_sent = {}
        suppressed = 0
        emitted = 0
        for sym, direction, ts_str in all_sigs:
            try:
                ts = datetime.fromisoformat(ts_str.replace('Z','+00:00'))
            except Exception:
                continue
            key = (sym, direction)
            last = last_sent.get(key)
            if last and (ts - last).total_seconds() < cooldown_secs:
                suppressed += 1
            else:
                emitted += 1
                last_sent[key] = ts

        total_sigs = suppressed + emitted
        print(f'  Cooldown window : {cooldown_secs}s = {cooldown_secs//3600}h')
        print(f'  Total signals in DB: {total_sigs}')
        print(f'  Would have Telegrammed: {emitted}')
        print(f'  Suppressed by cooldown: {suppressed}  ({suppressed/max(total_sigs,1)*100:.1f}%)')
        print()
        print(f'  Note: cooldown only affects Telegram alerts, NOT DB inserts.')
        print(f'  All signals are stored; only alerts are throttled.')
    except Exception as e:
        print(f'  ERROR: {e}')

    # ── 8. Symbol concentration ───────────────────────────────────────────────
    section('8. SYMBOL CONCENTRATION (close-but-never-pass)')
    try:
        # Symbols appearing frequently in partial_confluence but never final_signal
        partial = conn.execute(
            """SELECT symbol, COUNT(*) as cnt
               FROM candidate_snapshots
               WHERE stage='partial_confluence'
               AND signal_id NOT IN (888888,999999)
               GROUP BY symbol ORDER BY cnt DESC LIMIT 20"""
        ).fetchall()
        final = {r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM candidate_snapshots WHERE stage='final_signal' "
            "AND signal_id NOT IN (888888,999999)"
        ).fetchall()}
        signals_syms = {r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM signals"
        ).fetchall()}

        print(f'  Top symbols in partial_confluence, with signal history:')
        print(f'  {"Symbol":<8} {"Partial":>8} {"Final signal?":>14} {"In signals DB?":>15}')
        hr()
        for sym, cnt in partial[:15]:
            has_final  = '✓' if sym in final else '✗'
            has_signal = '✓' if sym in signals_syms else '✗'
            print(f'  {sym:<8} {cnt:>8}  {has_final:>14}  {has_signal:>15}')
    except Exception as e:
        print(f'  ERROR: {e}')

    # ── 9. Regime context ─────────────────────────────────────────────────────
    section('9. REGIME CONTEXT')
    try:
        # Look at RSI distribution of partial_confluence candidates
        rsi_rows = conn.execute(
            "SELECT rsi FROM candidate_snapshots "
            "WHERE rsi IS NOT NULL AND signal_id NOT IN (888888,999999)"
        ).fetchall()
        rsis = [r[0] for r in rsi_rows if r[0] is not None]

        if rsis:
            import statistics
            print(f'  RSI distribution of ALL candidates (n={len(rsis)}):')
            print(f'    Mean  : {statistics.mean(rsis):.1f}')
            print(f'    Median: {statistics.median(rsis):.1f}')
            print(f'    Min   : {min(rsis):.1f}  Max: {max(rsis):.1f}')
            # Bucket
            buckets = Counter()
            for r in rsis:
                if r < 35:   buckets['<35 (oversold)'] += 1
                elif r < 50: buckets['35-50 (bearish neutral)'] += 1
                elif r < 65: buckets['50-65 (bullish neutral)'] += 1
                else:        buckets['>65 (overbought)'] += 1
            print(f'  RSI buckets:')
            for label, cnt in sorted(buckets.items()):
                pct = cnt/len(rsis)*100
                print(f'    {label:<30}: {cnt:4d}  ({pct:.1f}%)')

        # MACD sign distribution
        macd_rows = conn.execute(
            "SELECT macd_hist FROM candidate_snapshots "
            "WHERE macd_hist IS NOT NULL AND signal_id NOT IN (888888,999999)"
        ).fetchall()
        macds = [r[0] for r in macd_rows if r[0] is not None]
        if macds:
            pos = sum(1 for m in macds if m > 0)
            neg = sum(1 for m in macds if m < 0)
            print(f'\n  MACD histogram sign (n={len(macds)}):')
            print(f'    Positive (bullish): {pos}  ({pos/len(macds)*100:.1f}%)')
            print(f'    Negative (bearish): {neg}  ({neg/len(macds)*100:.1f}%)')
            print(f'\n  Regime interpretation:')
            if pos/len(macds) < 0.4:
                print(f'    → BEARISH bias. MACD mostly negative — long signals harder to trigger.')
            elif pos/len(macds) > 0.6:
                print(f'    → BULLISH bias. MACD mostly positive — longs should fire more readily.')
            else:
                print(f'    → MIXED/CHOPPY. Roughly balanced MACD — regime is neutral/ranging.')
    except Exception as e:
        print(f'  ERROR: {e}')

    # ── 10. Recommendations ───────────────────────────────────────────────────
    section('10. RANKED RECOMMENDATIONS')

    # Count key numbers for concrete recommendations
    try:
        total_partial = conn.execute(
            "SELECT COUNT(*) FROM candidate_snapshots WHERE stage='partial_confluence' "
            "AND signal_id NOT IN (888888,999999)"
        ).fetchone()[0]
        total_final = conn.execute(
            "SELECT COUNT(*) FROM candidate_snapshots WHERE stage='final_signal' "
            "AND signal_id NOT IN (888888,999999)"
        ).fetchone()[0]
        total_signals_db = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        at_2 = conn.execute(
            "SELECT COUNT(*) FROM candidate_snapshots WHERE valid_count=2 "
            "AND signal_id NOT IN (888888,999999)"
        ).fetchone()[0]
    except Exception:
        total_partial = total_final = total_signals_db = at_2 = 0

    print(f'  Key numbers:')
    print(f'    partial_confluence candidates : {total_partial}')
    print(f'    final_signal candidates       : {total_final}')
    print(f'    signals in DB                 : {total_signals_db}')
    print(f'    candidates at valid_count=2   : {at_2}  '
          f'(would become signals if min_valid=2)')
    print()
    print('  ── SAFEST (no strategy change) ───────────────────────────')
    print('  1. Let flywheel accumulate more data (already running)')
    print('     Evidence: partial_confluence rows growing each cycle')
    print('     Risk: zero — passive data collection only')
    print()
    print('  2. Reduce Telegram cooldown: 4h → 1h')
    print('     Effect: more Telegram alerts for real signals')
    print('     Clarification: does NOT change signal frequency — only alert frequency')
    print('     Config: TELEGRAM_COOLDOWN_SECS=3600 in .env')
    print('     Risk: more Telegram noise, no strategy change')
    print()
    print('  ── MODERATE (small threshold change) ────────────────────')
    print('  3. Reduce min_valid_tfs: 3 → 2')
    f'     Effect: valid_count=2 candidates ({at_2}) become signals'
    print(f'     Effect: valid_count=2 candidates ({at_2} in DB) become signals')
    print('     Current: only fires when 3+ TFs agree — very strict for trending markets')
    print('     Change: dashboard → Strategy → confluence.min_valid_tfs = 2')
    print('     Risk: lower-confidence signals, more false positives')
    print('     Recommendation: run in paper mode first, observe quality for 2 weeks')
    print()
    print('  4. Widen RSI window: long rsi_max 75 → 80, short rsi_min 50 → 45')
    print('     Effect: allows signals in stronger trending conditions')
    print('     Risk: slightly more signals in overbought/oversold regions')
    print()
    print('  ── AGGRESSIVE (structural changes) ──────────────────────')
    print('  5. Disable 15m timeframe entirely')
    print('     Effect: 3-TF regime always active → min_valid=3 applies across all windows')
    print('     Reduces noise from 15m false positives')
    print('     Risk: loses intraday precision')
    print()
    print('  6. Separate long/short confluence thresholds')
    print('     Long: min_valid=2 (easier in bull regimes)')
    print('     Short: min_valid=3 (stricter — shorting requires stronger confirmation)')
    print('     Risk: requires code change, not a config change')
    print()
    print('  ── DO NOT CHANGE YET ────────────────────────────────────')
    print('  - Do not change EMA tolerance (0.005 is well-calibrated)')
    print('  - Do not change VWAP deviation thresholds')
    print('  - Do not change ATR stop/target multipliers')
    print('  - Do not touch ML, backtesting, or broker path')
    print()
    print('  ── SINGLE CHANGE TO TEST FIRST ──────────────────────────')
    print('  → Reduce TELEGRAM_COOLDOWN_SECS to 3600 (1h) in .env')
    print('    This is zero-risk and answers: are signals firing but being suppressed?')
    print('    If Telegram stays quiet for days after this change, the problem is')
    print('    genuinely signal generation, not alert suppression.')
    print('    If alerts increase significantly, cooldown was the main bottleneck.')

    hr('═')
    print('  END OF DIAGNOSIS')
    hr('═')
    conn.close()


if __name__ == '__main__':
    run()
