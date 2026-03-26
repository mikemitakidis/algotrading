#!/usr/bin/env python3
"""
backtest_cli_v2.py  —  CLI runner for the v2 backtest engine

Usage:
    python backtest_cli_v2.py --symbols AAPL --start 2025-01-01 --end 2025-12-31
    python backtest_cli_v2.py --preset aapl1y
    python backtest_cli_v2.py --symbols AAPL,MSFT --start 2025-01-01 --end 2025-12-31 --verbose

Output: console summary + files in data/reports/<timestamp>/

Presets: aapl1y  mega1y  mixed1y  90d15m
"""

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

PRESETS = {
    'aapl1y':  {'symbols': ['AAPL'],
                'start': (date.today() - timedelta(days=365)).isoformat(),
                'end':   date.today().isoformat()},
    'mega1y':  {'symbols': ['AAPL', 'MSFT', 'NVDA', 'GOOGL', 'AMZN'],
                'start': (date.today() - timedelta(days=365)).isoformat(),
                'end':   date.today().isoformat()},
    'mixed1y': {'symbols': ['AAPL','MSFT','NVDA','JPM','V','UNH','XOM','JNJ','WMT','NFLX'],
                'start': (date.today() - timedelta(days=365)).isoformat(),
                'end':   date.today().isoformat()},
    '90d15m':  {'symbols': ['AAPL', 'MSFT', 'NVDA'],
                'start': (date.today() - timedelta(days=90)).isoformat(),
                'end':   date.today().isoformat()},
}

SEP  = '─' * 60
SEP2 = '═' * 60


def _col(text, colour):
    codes = {'green': '\033[92m', 'red': '\033[91m',
             'yellow': '\033[93m', 'cyan': '\033[96m',
             'grey': '\033[90m', 'bold': '\033[1m'}
    return f"{codes.get(colour,'')}{text}\033[0m"


def main():
    parser = argparse.ArgumentParser(
        description='Algo Trader backtest CLI v2',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--symbols',  help='Comma-separated tickers e.g. AAPL,MSFT')
    parser.add_argument('--start',    help='Start date YYYY-MM-DD')
    parser.add_argument('--end',      help='End date (default: today)')
    parser.add_argument('--preset',   choices=list(PRESETS.keys()),
                        help='Named preset (overrides --symbols/--start/--end)')
    parser.add_argument('--verbose',  '-v', action='store_true',
                        help='Show detailed fetch and walk logs')
    parser.add_argument('--no-export', action='store_true',
                        help='Skip writing report files')
    args = parser.parse_args()

    # Logging
    level = logging.INFO if args.verbose else logging.WARNING
    logging.basicConfig(level=level, format='%(levelname)s %(name)s %(message)s')

    if args.preset:
        p = PRESETS[args.preset]
        symbols, start_str, end_str = p['symbols'], p['start'], p['end']
        print(f"Preset: {args.preset}")
    elif args.symbols and args.start:
        symbols   = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
        start_str = args.start
        end_str   = args.end or date.today().isoformat()
    else:
        parser.print_help()
        sys.exit(1)

    if len(symbols) > 10:
        print('Error: max 10 symbols'); sys.exit(1)

    print(SEP2)
    print(_col('ALGO TRADER — BACKTEST v2', 'bold'))
    print(SEP2)
    print(f"Symbols  : {', '.join(symbols)}")
    print(f"Period   : {start_str}  →  {end_str}")
    print(f"Exporting: {'no' if args.no_export else 'yes'}")
    print()

    from bot.backtest_v2 import run
    result = run(symbols, start_str, end_str, export=not args.no_export)

    if result.get('status') == 'error':
        print(_col(f"ERROR: {result['error']}", 'red'))
        sys.exit(1)

    s    = result.get('stats', {})
    meta = result.get('meta', {})

    # ── Data loading summary ───────────────────────────────────────────────
    print(SEP)
    print(_col('DATA LOADING', 'cyan'))
    print(SEP)
    for sym, d in result.get('diagnostics', {}).items():
        print(f"\n  {_col(sym, 'bold')}")
        if d.get('error'):
            print(f"    {_col('ERROR: ' + d['error'], 'red')}")
            continue
        fr = d.get('fetch_report', {})
        for lbl in ['1D', '4H', '1H', '15m']:
            if lbl not in fr: continue
            info = fr[lbl]
            ok   = info['bars'] > 0
            col  = 'green' if ok else 'red'
            rng  = f"  {info['first']}→{info['last']}" if info.get('first') else ''
            print(f"    {_col(lbl, col)}: {info['bars']:>5} bars  "
                  f"[{info['status']}]{rng}")
        wlk = d.get('walk', {})
        if wlk:
            print(f"    available TFs  : {wlk.get('tfs_available', [])}")
            print(f"    min_valid      : {wlk.get('min_valid', '?')}")
            print(f"    candidates     : {wlk.get('candidates', 0)}")
            rej = wlk.get('rejected', {})
            if rej:
                for reason, cnt in sorted(rej.items(), key=lambda x: -x[1]):
                    print(f"    rejected ({reason}): {cnt}")
            else:
                print(f"    rejected       : none")

    # ── Results ───────────────────────────────────────────────────────────
    print()
    print(SEP)
    print(_col('RESULTS', 'cyan'))
    print(SEP)

    total = s.get('total', 0)
    if not total:
        print(_col('  No trades generated.', 'yellow'))
        print()
        print('  Possible reasons:')
        print('  1. Strategy thresholds not met during this period')
        print('  2. Data loaded but only 1D available — check TF coverage above')
        print('  3. Confluence too strict (min_valid TFs) for available data')
        print(f"  4. Confluence min = {meta.get('confluence_min', '?')} TFs required")
    else:
        wr  = s.get('win_rate', 0)
        pf  = s.get('profit_factor')
        dd  = s.get('max_drawdown_pct', 0)
        feq = s.get('final_equity', 100)
        ann = s.get('annualised_return_pct', 0)

        wc  = 'green' if wr >= 50 else 'yellow'
        pfc = 'green' if (pf or 0) >= 1 else 'red'
        eqc = 'green' if feq >= 100 else 'red'

        print(f"  Total trades     : {_col(total, 'bold')}")
        print(f"  Win/Loss/Timeout : {s.get('wins',0)} / {s.get('losses',0)} / {s.get('timeouts',0)}")
        print(f"  Win rate         : {_col(str(wr)+'%', wc)}")
        print(f"  Profit factor    : {_col(str(pf) if pf else 'n/a', pfc)}")
        print(f"  Avg return       : {s.get('avg_return_pct',0)}%")
        print(f"  Avg win          : {_col(str(s.get('avg_win_pct',0))+'%', 'green')}")
        print(f"  Avg loss         : {_col(str(s.get('avg_loss_pct',0))+'%', 'red')}")
        print(f"  Max drawdown     : {_col(str(dd)+'%', 'red')}")
        print(f"  Final equity     : {_col(str(feq), eqc)}  (start=100)")
        print(f"  Annualised return: {_col(str(ann)+'%', 'green' if ann >= 0 else 'red')}")
        print(f"  Avg hold (days)  : {s.get('avg_hold_days',0)}")
        print(f"  Max consec W/L   : {s.get('max_consec_wins',0)} / {s.get('max_consec_losses',0)}")

        # By TF
        by_tf = s.get('by_timeframe', {})
        if by_tf:
            print(f"\n  {SEP}")
            print(f"  BY TIMEFRAME")
            for tf in ['1D', '4H', '1H', '15m']:
                if tf not in by_tf: continue
                v  = by_tf[tf]
                wc = 'green' if v['win_rate'] >= 50 else 'yellow'
                print(f"    {tf:<5}: {v['trades']:>3} trades  "
                      f"WR:{_col(str(v['win_rate'])+'%', wc):>12}  "
                      f"AvgRet:{v['avg_ret']:>+7.3f}%")

        # By combo
        by_combo = s.get('by_tf_combo', {})
        if by_combo:
            print(f"\n  BY TF COMBINATION")
            for combo, v in sorted(by_combo.items(), key=lambda x: -x[1]['total']):
                wc = 'green' if v['win_rate'] >= 50 else 'yellow'
                print(f"    {combo:<22}: {v['total']:>3} trades  "
                      f"WR:{_col(str(v['win_rate'])+'%', wc):>12}  "
                      f"AvgRet:{v['avg_ret']:>+7.3f}%")

        # Sample trades
        trades = result.get('trades', [])
        show   = trades[:5]
        if show:
            print(f"\n  FIRST {len(show)} TRADES")
            for t in show:
                tfs = '+'.join(t.get('tfs_triggered', []))
                oc  = 'green' if t['outcome'] == 'WIN' else (
                      'red' if t['outcome'] == 'LOSS' else 'yellow')
                print(f"    {t['date']}  {t['symbol']:<6}  {t['direction']:<6}  "
                      f"{tfs:<14}  entry={t['entry_price']:.2f}  "
                      f"{_col(t['outcome'], oc)}  {t['return_pct']:>+6.3f}%")

    # ── Files ─────────────────────────────────────────────────────────────
    folder = result.get('report_folder')
    print()
    print(SEP)
    if folder:
        print(_col(f"Reports saved to: {folder}/", 'cyan'))
        print(f"  report.txt   — human-readable summary")
        print(f"  trades.csv   — trade list")
        print(f"  results.json — full JSON")
    else:
        print(_col('Export skipped or failed.', 'grey'))

    elapsed = result.get('elapsed_s', 0)
    print()
    print(f"Completed in {elapsed}s")
    print(SEP2)


if __name__ == '__main__':
    main()
