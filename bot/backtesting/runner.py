"""bot.backtesting.runner — the single public entry point.

    run(config)             -> BacktestResult
    run_and_write(config,   -> Path
                     output_dir=...)

Orchestration only — every step lives in its own module:

    config       -> validated BacktestConfig
    data_loader  -> bars + coverage + load-time warnings
    strategy     -> signals (Strategy.run(bars))
    execution    -> ledger filled by simulate()
    metrics      -> dict computed from ledger + bars
    BacktestResult assembled and returned
    output       -> filesystem artifacts (run_and_write only)

Hard rules:
  * Synchronous.
  * Pure with respect to the M16 store (read-only).
  * No network calls.
  * No retries or self-healing — failures bubble up as BacktestError
    subclasses with operator-actionable messages.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Union

from bot.backtesting.config import (
    BacktestConfig, config_hash, config_to_dict,
)
from bot.backtesting.data_loader import load_backtest_bars
from bot.backtesting.errors import BacktestError
from bot.backtesting.execution import simulate
from bot.backtesting.ledger import Ledger
from bot.backtesting.metrics import compute_metrics
from bot.backtesting.models import BacktestResult
from bot.backtesting.output import build_run_id, write_results
from bot.backtesting.strategy import get_strategy


def run(cfg: BacktestConfig) -> BacktestResult:
    """Execute a backtest end-to-end and return a BacktestResult.
    Doesn't write to disk — use run_and_write for that."""
    if not isinstance(cfg, BacktestConfig):
        raise BacktestError(
            f"runner.run expects BacktestConfig, got {type(cfg).__name__}")

    # 1. Load bars + coverage from M16.
    bars, coverage, load_warnings = load_backtest_bars(cfg)

    # 2. Generate signals.
    strategy = get_strategy(cfg.strategy.name, cfg.strategy.params)
    signals = strategy.run(bars)

    # 3. Simulate.
    ledger = Ledger()
    ledger.extend_warnings(load_warnings)
    simulate(bars=bars, signals=signals, cfg=cfg, ledger=ledger)

    # 4. Metrics.
    metrics = compute_metrics(
        ledger=ledger, bars=bars, exec_cfg=cfg.execution)

    # 5. Assemble.
    cfg_hash = config_hash(cfg)
    created_at = datetime.now(timezone.utc)
    return BacktestResult(
        run_id=build_run_id(cfg, created_at_utc=created_at,
                                cfg_hash=cfg_hash),
        created_at_utc=created_at,
        config=config_to_dict(cfg),
        config_hash=cfg_hash,
        coverage_metadata=coverage,
        trades=ledger.trades,
        equity_curve=ledger.equity_curve,
        warnings=ledger.warnings,
        metrics=metrics,
        bars_processed=len(bars),
    )


def run_and_write(
    cfg: BacktestConfig,
    output_dir: Union[Path, str] = "data/backtests",
) -> Path:
    """Convenience: run() + write_results(). Returns the run dir path."""
    result = run(cfg)
    return write_results(
        result, cfg, output_dir,
        run_id=result.run_id,
        created_at_utc=result.created_at_utc,
    )


__all__ = ["run", "run_and_write"]
