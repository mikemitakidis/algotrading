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
from bot.backtesting.data_loader import (
    load_backtest_bars,
    load_multi_tf_bars,
)
from bot.backtesting.errors import BacktestError, StrategyError
from bot.backtesting.execution import simulate
from bot.backtesting.ledger import Ledger
from bot.backtesting.metrics import compute_metrics
from bot.backtesting.models import BacktestResult
from bot.backtesting.mtf_context import MultiTimeframeContext
from bot.backtesting.output import build_run_id, write_results
from bot.backtesting.strategy import (
    MultiTimeframeStrategy,
    get_strategy,
)


# M21 E3: the conversion method whose evidence injected multi-TF bars must
# carry (research.dataqa.converted.METHOD; production never injects bars).
CONVERTED_BARS_METHOD = "m21-e3-close-stamped/1"


def _accept_converted(mtf, cfg: BacktestConfig, timeframes) -> None:
    """Injected multi-TF bars (the research converted path) must be the
    requested symbol's, cover every strategy timeframe, carry conversion
    evidence for each, and still pass the start-stamp guard."""
    from bot.backtesting.data_loader import MultiTfBars
    from bot.backtesting.mtf_context import refuse_start_stamped
    if not isinstance(mtf, MultiTfBars):
        raise BacktestError("injected multi-TF bars must be a MultiTfBars")
    if mtf.symbol != cfg.request.symbol:
        raise BacktestError(f"injected bars are for {mtf.symbol!r}, not {cfg.request.symbol!r}")
    missing = [tf for tf in timeframes if mtf.per_tf_bars.get(tf) is None]
    if missing:
        raise BacktestError(f"injected bars lack timeframes {missing}")
    ev = mtf.conversion or {}
    per_tf = ev.get("per_tf") or {}
    if ev.get("method") != CONVERTED_BARS_METHOD or any(tf not in per_tf for tf in timeframes):
        raise BacktestError("injected multi-TF bars carry no conversion evidence for every "
                            "timeframe: only the converted path (research.dataqa.converted) may inject")
    refuse_start_stamped(mtf.per_tf_bars, f"converted multi-TF bars ({cfg.request.symbol})")


def run(cfg: BacktestConfig, *, _multi_tf_bars=None) -> BacktestResult:
    """Execute a backtest end-to-end and return a BacktestResult.
    Doesn't write to disk — use run_and_write for that.

    ``_multi_tf_bars`` is INTERNAL (like M18's ``_bars_provider``): the
    research converted-data path passes close-stamped bars WITH their
    conversion evidence. Production never passes it (pinned by test), so
    production always loads through the guarded ``load_multi_tf_bars``."""
    if not isinstance(cfg, BacktestConfig):
        raise BacktestError(
            f"runner.run expects BacktestConfig, got {type(cfg).__name__}")

    # 1. Instantiate strategy first so we know whether to load single
    #    or multi-TF bars (M17.B.4 — scanner_replica needs multi-TF;
    #    M17.A SmaCrossoverStrategy stays on the single-TF path).
    strategy = get_strategy(cfg.strategy.name, cfg.strategy.params)

    # 2. Load bars + coverage from M16, single or multi-TF as required.
    if isinstance(strategy, MultiTimeframeStrategy):
        # Multi-TF path: strategy.params['timeframes'] declares which
        # TFs to load. cfg.request.timeframe MUST be the anchor TF
        # (== strategy.params['anchor_tf']) and MUST be in the list.
        timeframes = strategy.params["timeframes"]
        anchor_tf  = strategy.params["anchor_tf"]
        if cfg.request.timeframe != anchor_tf:
            raise StrategyError(
                f"{strategy.name}: cfg.request.timeframe="
                f"{cfg.request.timeframe!r} must equal "
                f"strategy.params.anchor_tf={anchor_tf!r}; the request "
                f"timeframe identifies the cycle anchor for the "
                f"multi-TF run")
        if _multi_tf_bars is None:
            mtf = load_multi_tf_bars(cfg, timeframes,
                                        allow_partial_tfs=False)  # strict
        else:
            _accept_converted(_multi_tf_bars, cfg, timeframes)
            mtf = _multi_tf_bars
        bars     = mtf.per_tf_bars[anchor_tf]
        coverage = mtf.per_tf_coverage[anchor_tf]
        load_warnings = mtf.warnings
        context = MultiTimeframeContext(
            mtf.per_tf_bars, anchor_tf=anchor_tf)
        strategy.attach_context(context)
    else:
        if _multi_tf_bars is not None:
            raise StrategyError(
                f"{strategy.name} is single-timeframe: converted multi-TF bars "
                f"are not accepted")
        # M17.A single-TF path — byte-identical to before M17.B.4.
        bars, coverage, load_warnings = load_backtest_bars(cfg)

    # 3. Generate signals.
    signals = strategy.run(bars)

    # 4. Simulate.
    ledger = Ledger()
    ledger.extend_warnings(load_warnings)
    simulate(bars=bars, signals=signals, cfg=cfg, ledger=ledger)

    # 5. Metrics.
    metrics = compute_metrics(
        ledger=ledger, bars=bars, exec_cfg=cfg.execution)

    # 6. Assemble.
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
