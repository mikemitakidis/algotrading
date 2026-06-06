"""bot.backtesting.cli — `python -m bot.backtesting.cli run [...]`.

Two ways to invoke:

  1. From a JSON config file:
       python -m bot.backtesting.cli run \\
         --config configs/backtests/example_sma_aapl.json

  2. From inline arguments:
       python -m bot.backtesting.cli run \\
         --symbol AAPL --timeframe 1D \\
         --from 2024-01-01 --to 2024-12-31 \\
         --strategy sma_crossover --fast 20 --slow 50

Inline arguments override config-file fields when both are supplied.

Exit codes:
  0   success — run completed and artifacts written
  2   MissingDataError — operator must run a refresh
  3   ConfigError — bad config / bad CLI args
  1   any other unexpected error
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Dict

from bot.backtesting.config import parse_config_dict, parse_config_file
from bot.backtesting.errors import (
    BacktestError, ConfigError, MissingDataError,
)
from bot.backtesting.runner import run_and_write


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m bot.backtesting.cli",
        description="M17 backtesting engine.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # run subcommand
    pr = sub.add_parser("run", help="Run a backtest.")
    pr.add_argument("--config", type=str, default=None,
                      help="Path to JSON config file.")
    pr.add_argument("--output-dir", type=str, default="data/backtests",
                      help="Where to write the run directory "
                           "(default: data/backtests).")
    # Inline-config overrides (all optional)
    pr.add_argument("--symbol",      type=str)
    pr.add_argument("--timeframe",   type=str)
    pr.add_argument("--from",        type=str, dest="start_date")
    pr.add_argument("--to",          type=str, dest="end_date")
    pr.add_argument("--strategy",    type=str)
    pr.add_argument("--fast",        type=int, dest="fast_window")
    pr.add_argument("--slow",        type=int, dest="slow_window")
    pr.add_argument("--initial-equity",     type=float)
    pr.add_argument("--fee-bps",            type=float)
    pr.add_argument("--slippage-bps",       type=float)
    pr.add_argument("--stop-loss-pct",      type=float)
    pr.add_argument("--take-profit-pct",    type=float)
    pr.add_argument("--risk-per-trade-pct", type=float)
    pr.add_argument("--max-position-pct",   type=float)
    return p


def _build_config_dict(args: argparse.Namespace) -> Dict[str, Any]:
    """Build a config dict from a --config file (if given), then
    apply inline arg overrides."""
    if args.config is not None:
        raw = json.loads(Path(args.config).read_text())
    else:
        raw = {"request": {}, "data": {}, "strategy": {}, "execution": {}}

    # Inline overrides
    if args.symbol:    raw.setdefault("request", {})["symbol"]    = args.symbol
    if args.timeframe: raw.setdefault("request", {})["timeframe"] = args.timeframe
    if args.start_date:raw.setdefault("request", {})["start"]     = args.start_date
    if args.end_date:  raw.setdefault("request", {})["end"]       = args.end_date

    if args.strategy:
        raw.setdefault("strategy", {})["name"] = args.strategy
    if args.fast_window is not None:
        raw.setdefault("strategy", {}).setdefault(
            "params", {})["fast_window"] = args.fast_window
    if args.slow_window is not None:
        raw.setdefault("strategy", {}).setdefault(
            "params", {})["slow_window"] = args.slow_window

    for cli_name, cfg_name in (
        ("initial_equity",     "initial_equity"),
        ("fee_bps",            "fee_bps"),
        ("slippage_bps",       "slippage_bps"),
        ("stop_loss_pct",      "stop_loss_pct"),
        ("take_profit_pct",    "take_profit_pct"),
        ("risk_per_trade_pct", "risk_per_trade_pct"),
        ("max_position_pct",   "max_position_pct"),
    ):
        v = getattr(args, cli_name, None)
        if v is not None:
            raw.setdefault("execution", {})[cfg_name] = v

    return raw


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        raw = _build_config_dict(args)
        cfg = parse_config_dict(raw)
    except ConfigError as e:
        print(f"ERROR: ConfigError: {e}", file=sys.stderr)
        return 3
    except json.JSONDecodeError as e:
        print(f"ERROR: config file is not valid JSON: {e}", file=sys.stderr)
        return 3

    try:
        run_dir = run_and_write(cfg, output_dir=args.output_dir)
    except MissingDataError as e:
        print(f"ERROR: MissingDataError: {e}", file=sys.stderr)
        return 2
    except ConfigError as e:
        print(f"ERROR: ConfigError: {e}", file=sys.stderr)
        return 3
    except BacktestError as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        traceback.print_exc()
        print(f"ERROR: unexpected {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    # Print a one-line summary to stdout for the operator.
    report = json.loads((run_dir / "report.json").read_text())
    metrics = report["metrics"]
    print(f"OK: run written to {run_dir}")
    print(f"  trades:            {metrics['n_trades']}")
    print(f"  win_rate:          {metrics['win_rate']:.2%}")
    print(f"  total_return_pct:  {metrics['total_return_pct']:+.4f}")
    print(f"  max_drawdown_pct:  {metrics['max_drawdown_pct']:.4f}")
    print(f"  exposure_time_pct: {metrics['exposure_time_pct']:.4f}")
    return 0


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _cmd_run(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
