#!/usr/bin/env python3
"""
backtest_cli.py — Command-line backtest runner for Algo Trader.

Usage:
    python backtest_cli.py --symbols AAPL,MSFT --start 2025-01-01 --end 2025-12-31
    python backtest_cli.py --preset aapl1y
    python backtest_cli.py --symbols SPY --start 2024-01-01 --end 2024-12-31 --no-benchmark

Output:
    - Console summary printed immediately
    - Full report saved to data/reports/<timestamp>/report.txt
    - Trade CSV saved to data/reports/<timestamp>/trades.csv
    - Full JSON saved to data/reports/<timestamp>/results.json

Examples:
    # AAPL 1 year (standard)
    python backtest_cli.py --symbols AAPL --start 2025-03-20 --end 2026-03-20

    # 5 mega-caps 6 months
    python backtest_cli.py --symbols AAPL,MSFT,NVDA,GOOGL,AMZN --start 2025-09-01 --end 2026-03-01

    # Last 90 days (15m data available)
    python backtest_cli.py --symbols AAPL,MSFT --start 2025-12-20 --end 2026-03-20

    # Use a named preset
    python backtest_cli.py --preset aapl1y
    python backtest_cli.py --preset mega1y
    python backtest_cli.py --preset mixed1y
    python backtest_cli.py --preset 90d15m
"""

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Minimal logging — errors only unless --verbose
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s %(levelname)s %(message)s'
)

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


def _print_section(title: str):
    print(f'\n── {title} ' + '─' * max(0, 55 - len(title)))


def run(symbols, start_str, end_str, verbose=False, no_benchmark=False):
    if verbose:
        logging.getLogger().setLevel(logging.INFO)

    sys.path.insert(0, str(BASE_DIR))
    from bot.backtest import run_backtest, read_results
    from bot.strategy import load as load_strategy

    strategy = load_strategy()

    print('=' * 60)
    print('ALGO TRADER — BACKTEST CLI')
    print('=' * 60)
    print(f'Symbols  : {", ".join(symbols)}')
    print(f'Period   : {start_str} → {end_str}')
    print(f'Strategy : v{strategy.get("version", 1)}'
          f'  |  Confluence ≥{strategy.get("confluence",{}).get("min_valid_tfs",3)}/4 TFs')
    print()
    print('Running… (this may take 30-120s for cold cache)')

    # Run synchronously in this thread (not in background thread)
    from bot.backtest import _new_token, _CANCEL_EVENT, _RUN_TOKEN, _RUN_LOCK
    with _RUN_LOCK:
        _CANCEL_EVENT.clear()
        token = _new_token()
        _RUN_TOKEN['value'] = token

    run_backtest(symbols, start_str, end_str, strategy, token,
                 skip_benchmark=no_benchmark)
    result = read_results()

    status = result.get('status', 'error')
    s = result.get('stats', {})
    m = result.get('meta', {})

    print()
    print(f'Status   : {status.upper()}')
    if status in ('partial', 'cancelled', 'timeout'):
        comp = m.get('symbols_completed', 0)
        tot  = m.get('symbols_count', 0)
        print(f'⚠  PARTIAL — {comp}/{tot} symbols completed')
        if result.get('stop_reason'):
            print(f'   Reason: {result["stop_reason"]}')

    if not s.get('total'):
        print('\nNo trades generated.')
    else:
        _print_section('PERFORMANCE')
        print(f'  Total trades     : {s.get("total",0)}')
        print(f'  Win / Loss / TO  : {s.get("wins",0)} / {s.get("losses",0)} / {s.get("timeouts",0)}')
        print(f'  Win rate         : {s.get("win_rate",0)}%')
        print(f'  Profit factor    : {s.get("profit_factor","n/a")}')
        print(f'  Avg return       : {s.get("avg_return_pct",0)}%')
        print(f'  Max drawdown     : {s.get("max_drawdown_pct",0)}%')
        print(f'  Final equity     : {s.get("final_equity",100)} (start=100)')
        print(f'  Annualised ret   : {s.get("annualised_return_pct",0)}%')
        print(f'  Avg hold (days)  : {s.get("avg_hold_days",0)}')
        print(f'  Max consec W/L   : {s.get("max_consec_wins",0)} / {s.get("max_consec_losses",0)}')

        by_tf = s.get('by_timeframe', {})
        if by_tf:
            _print_section('BY TIMEFRAME')
            for tf in ['1D', '4H', '1H', '15m']:
                if tf not in by_tf: continue
                v = by_tf[tf]
                print(f'  {tf:<5}: {v["trades"]:>3} trades  '
                      f'WR:{v["win_rate"]:>5.1f}%  AvgRet:{v["avg_ret"]:>+7.3f}%')

        bm = result.get('benchmark', {})
        if bm.get('symbol') and not no_benchmark:
            _print_section(f'vs {bm["symbol"]} (buy-and-hold)')
            op   = result.get('outperformance_pct', 0)
            sign = '+' if op >= 0 else ''
            print(f'  Strategy ann.ret : {s.get("annualised_return_pct",0)}%')
            print(f'  Benchmark ann.ret: {bm.get("annualised_pct",0)}%')
            verdict = '▲ OUTPERFORMS' if op >= 0 else '▼ UNDERPERFORMS'
            print(f'  {verdict} by {sign}{op}% annualised')

    # Report path
    report_folder = None
    from bot.backtest import REPORTS_DIR
    folders = sorted(REPORTS_DIR.glob('*/report.txt')) if REPORTS_DIR.exists() else []
    if folders:
        report_folder = folders[-1].parent

    print()
    if report_folder:
        print(f'Reports saved to: {report_folder}/')
        print(f'  report.txt   — human-readable summary')
        print(f'  trades.csv   — trade list')
        print(f'  results.json — full JSON results')
    print()


def main():
    parser = argparse.ArgumentParser(
        description='Algo Trader backtest CLI',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--symbols',      help='Comma-separated tickers, e.g. AAPL,MSFT')
    parser.add_argument('--start',        help='Start date YYYY-MM-DD')
    parser.add_argument('--end',          help='End date YYYY-MM-DD (default: today)')
    parser.add_argument('--preset',       choices=list(PRESETS.keys()),
                        help='Use a named preset (overrides --symbols/--start/--end)')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Show detailed fetch logs')
    parser.add_argument('--no-benchmark', action='store_true',
                        help='Skip benchmark fetch entirely (saves ~10s, no SPY/symbol comparison)')
    args = parser.parse_args()

    if args.preset:
        p = PRESETS[args.preset]
        symbols   = p['symbols']
        start_str = p['start']
        end_str   = p['end']
        print(f'Using preset: {args.preset}')
    elif args.symbols:
        symbols   = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
        start_str = args.start
        end_str   = args.end or date.today().isoformat()
        if not start_str:
            parser.error('--start is required when not using --preset')
    else:
        parser.print_help()
        sys.exit(1)

    if len(symbols) > 10:
        print('Error: max 10 symbols per run')
        sys.exit(1)

    run(symbols, start_str, end_str, verbose=args.verbose,
        no_benchmark=args.no_benchmark)


if __name__ == '__main__':
    main()
