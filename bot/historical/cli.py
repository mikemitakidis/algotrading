"""bot/data/cli.py — M16 operator entry points.

Invoked as: python -m bot.historical.cli <subcommand> [args]

Subcommands:
  backfill --symbols S1,S2,... --timeframes 1D,1H,15m
  incremental [--symbols ...] [--timeframes ...]
  repair [--symbols ...] [--timeframes ...]
  force-rebuild --symbol S --timeframe TF
  status

The provider is yfinance for V1. The DB lives at data/historical.db
relative to the repo root; Parquet at data/historical/.

This module imports NOTHING broker-related. AST-asserted.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from bot.historical import refresh as _refresh
from bot.historical import schema as _schema
from bot.historical import store as _store
from bot.historical.providers_yfinance import YFinanceProvider


log = logging.getLogger(__name__)


ALL_TIMEFRAMES = ("1D", "1H", "15m", "4H")
DEFAULT_TIMEFRAMES = "1D,1H,15m,4H"


def _load_symbols_from_csv(repo_root: Path) -> List[str]:
    """Load the symbol universe from data/symbol_universe.csv.

    The CSV expects either a header 'symbol' column, or symbols
    one-per-line. Symbols are upper-cased and deduplicated, order
    preserved.
    """
    csv = repo_root / "data" / "symbol_universe.csv"
    if not csv.exists():
        return []
    symbols: List[str] = []
    seen = set()
    with open(csv, "r", encoding="utf-8") as f:
        for raw in f:
            s = raw.strip().split(",")[0].strip().upper()
            if not s or s.lower() in ("symbol", "ticker", "#"):
                continue
            if s in seen:
                continue
            seen.add(s)
            symbols.append(s)
    return symbols


def _parse_csv_list(arg: str) -> List[str]:
    return [x.strip() for x in arg.split(",") if x.strip()]


def cmd_backfill(args, repo_root: Path) -> int:
    symbols = _resolve_symbols(args, repo_root)
    if not symbols:
        print("no symbols specified; pass --symbols or populate "
                "data/symbol_universe.csv", file=sys.stderr)
        return 2
    tfs = _resolve_timeframes(args)
    provider = YFinanceProvider()
    print(f"[backfill] symbols={len(symbols)} timeframes={tfs} "
            f"provider={provider.capability.name}")
    res = _refresh.run(
        mode="backfill",
        symbols=symbols, timeframes=tfs, provider=provider,
        backfill_max_lookback=args.lookback)
    _print_result(res)
    return 0 if res.status == "ok" else 1


def cmd_incremental(args, repo_root: Path) -> int:
    symbols = _resolve_symbols(args, repo_root)
    if not symbols:
        # Default to whatever already exists in coverage.
        symbols = _store.list_symbols()
    tfs = _resolve_timeframes(args)
    provider = YFinanceProvider()
    print(f"[incremental] symbols={len(symbols)} timeframes={tfs}")
    res = _refresh.run(
        mode="incremental",
        symbols=symbols, timeframes=tfs, provider=provider)
    _print_result(res)
    return 0 if res.status in ("ok", "partial") else 1


def cmd_repair(args, repo_root: Path) -> int:
    symbols = _resolve_symbols(args, repo_root)
    if not symbols:
        symbols = _store.list_symbols()
    tfs = _resolve_timeframes(args)
    provider = YFinanceProvider()
    print(f"[repair] symbols={len(symbols)} timeframes={tfs}")
    res = _refresh.run(
        mode="repair",
        symbols=symbols, timeframes=tfs, provider=provider)
    _print_result(res)
    return 0 if res.status in ("ok", "partial") else 1


def cmd_force_rebuild(args, repo_root: Path) -> int:
    if not args.symbol or not args.timeframe:
        print("force-rebuild requires --symbol and --timeframe",
                file=sys.stderr)
        return 2
    provider = YFinanceProvider()
    print(f"[force-rebuild] symbol={args.symbol} timeframe={args.timeframe}")
    res = _refresh.run(
        mode="force_rebuild",
        symbols=[args.symbol], timeframes=[args.timeframe],
        provider=provider, backfill_max_lookback=args.lookback)
    _print_result(res)
    return 0 if res.status == "ok" else 1


def cmd_status(args, repo_root: Path) -> int:
    """Human-readable status check. Reads DB only."""
    db = _schema.default_db_path(repo_root)
    if not db.exists():
        print(f"no historical DB at {db}; nothing to report")
        return 0
    conn = _schema.open_db(db)
    try:
        v = _schema.get_schema_version(conn)
        n_symbols = conn.execute(
            "SELECT COUNT(*) FROM historical_symbols").fetchone()[0]
        n_coverage = conn.execute(
            "SELECT COUNT(*) FROM historical_coverage").fetchone()[0]
        n_runs = conn.execute(
            "SELECT COUNT(*) FROM historical_refresh_runs").fetchone()[0]
        n_events = conn.execute(
            "SELECT COUNT(*) FROM historical_quality_events").fetchone()[0]
        print(f"M16 historical store status")
        print(f"  schema_version:       {v}")
        print(f"  symbols:              {n_symbols}")
        print(f"  coverage rows:        {n_coverage}")
        print(f"  refresh_runs:         {n_runs}")
        print(f"  quality_events:       {n_events}")

        last = conn.execute(
            "SELECT run_id, started_at_utc, finished_at_utc, mode, "
            "       status, symbols_ok, symbols_no_data, symbols_failed, "
            "       symbols_rate_limited, bars_written, "
            "       rate_limit_count, duration_sec "
            "FROM historical_refresh_runs "
            "ORDER BY run_id DESC LIMIT 1").fetchone()
        if last is not None:
            print(f"  last refresh:")
            keys = ("run_id", "started", "finished", "mode", "status",
                      "ok", "no_data", "failed", "rate_limited",
                      "bars_written", "rate_limit_count", "duration_s")
            for k, val in zip(keys, last):
                print(f"    {k:14s} {val}")

        recent_errors = conn.execute(
            "SELECT COUNT(*) FROM historical_quality_events "
            "WHERE severity='error' AND created_at_utc >= ?",
            ((datetime.now(timezone.utc).replace(microsecond=0).isoformat()
              [:10] + "T00:00:00+00:00"),)).fetchone()[0]
        print(f"  errors today (UTC):   {recent_errors}")
    finally:
        conn.close()
    return 0


def _resolve_symbols(args, repo_root: Path) -> List[str]:
    if getattr(args, "symbols", None):
        return _parse_csv_list(args.symbols)
    return _load_symbols_from_csv(repo_root)


def _resolve_timeframes(args) -> List[str]:
    return _parse_csv_list(getattr(args, "timeframes", None) or
                             DEFAULT_TIMEFRAMES)


def _print_result(res):
    print(f"  status={res.status}")
    print(f"  symbols_attempted={res.symbols_attempted}")
    print(f"  symbols_ok={res.symbols_ok} no_data={res.symbols_no_data} "
            f"failed={res.symbols_failed} "
            f"rate_limited={res.symbols_rate_limited}")
    print(f"  bars_fetched={res.bars_fetched} bars_written={res.bars_written} "
            f"bars_updated={res.bars_updated}")
    print(f"  duration_sec={res.duration_sec:.2f}")
    if res.errors_count:
        print(f"  errors_count={res.errors_count}")
    if res.rate_limit_count:
        print(f"  rate_limit_count={res.rate_limit_count}  "
                f"(retry attempts hit by rate-limit responses)")
    # Honest banner: when nothing was stored, say so loudly.
    if res.symbols_rate_limited > 0 and res.symbols_ok == 0:
        print("")
        print("  PROVIDER RATE-LIMITED — no bars were written.")
        print("  See 'rate_limited' quality events for details:")
        print("    python -m bot.historical.cli status")
        print("  Operator response options:")
        print("    * wait 5-15 minutes and retry")
        print("    * retry with fewer symbols (e.g. --symbols AAPL)")
        print("    * retry with one timeframe only (e.g. --timeframes 1D)")
        print("    * see docs/M16_historical_data.md §P for guidance")
    elif res.symbols_failed > 0 and res.symbols_ok == 0:
        print("")
        print("  ALL SYMBOLS FAILED — no bars were written.")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot.historical.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_backfill = sub.add_parser("backfill", help="full backfill")
    p_backfill.add_argument("--symbols", help="CSV list; default: read CSV")
    p_backfill.add_argument("--timeframes", default=DEFAULT_TIMEFRAMES)
    p_backfill.add_argument("--lookback", default=None,
                              help="e.g. 730d; default: provider max")
    p_backfill.set_defaults(fn=cmd_backfill)

    p_inc = sub.add_parser("incremental", help="incremental refresh")
    p_inc.add_argument("--symbols", default=None)
    p_inc.add_argument("--timeframes", default=DEFAULT_TIMEFRAMES)
    p_inc.set_defaults(fn=cmd_incremental)

    p_rep = sub.add_parser("repair", help="repair gaps")
    p_rep.add_argument("--symbols", default=None)
    p_rep.add_argument("--timeframes", default=DEFAULT_TIMEFRAMES)
    p_rep.set_defaults(fn=cmd_repair)

    p_fr = sub.add_parser("force-rebuild", help="delete + rebuild one (sym,tf)")
    p_fr.add_argument("--symbol", required=True)
    p_fr.add_argument("--timeframe", required=True)
    p_fr.add_argument("--lookback", default=None)
    p_fr.set_defaults(fn=cmd_force_rebuild)

    p_st = sub.add_parser("status", help="show status of historical store")
    p_st.set_defaults(fn=cmd_status)

    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent.parent

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    return args.fn(args, repo_root)


if __name__ == "__main__":
    sys.exit(main())
