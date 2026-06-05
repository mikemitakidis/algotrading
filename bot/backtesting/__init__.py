"""bot.backtesting — M17 local-first backtesting engine.

Reads ONLY from the M16 historical store (`bot.historical`).
NEVER calls a data provider directly. NEVER imports broker / scanner /
order-path modules. Pure consumer of M16 parquet data; pure producer of
file-based result artifacts.

Public API:
    bot.backtesting.run(config) -> BacktestResult

Hard invariants (AST-asserted by test_m17_backtesting.py G10):
    * No imports of yfinance, bot.data, bot.providers.*, bot.scanner,
      bot.backtest, bot.backtest_v2, bot.brokers.*, bot.etoro.*, ibapi,
      ib_insync, requests, urllib.
    * No string literals referencing order methods.
    * Network sockets never opened during a run.

Layout (read by tests; written only via the public API):
    __init__.py        this file
    errors.py          MissingDataError, ConfigError, StrategyError
    models.py          Trade, Position, EquityPoint, BacktestWarning,
                         BacktestResult, Bar
    config.py          BacktestRequest, StrategyConfig, ExecutionConfig,
                         parse_config_file, validate_config, config_hash
    data_loader.py     load_backtest_bars, validate_coverage
                         (ONLY module that touches bot.historical)
    indicators.py      vectorized SMA, EMA, RSI, MACD, ATR, volume_avg,
                         bollinger — pure pd.Series -> pd.Series
    strategy.py        Strategy base + SmaCrossoverStrategy
    portfolio.py       cash, position, sizing, max-position cap
    execution.py       bar loop: entry/exit/SL/TP/EOD, fees, slippage
    ledger.py          Ledger + equity-curve recorder
    metrics.py         pure (ledger, equity, bars) -> dict
    output.py          manifest/report/csv/jsonl artifacts
    runner.py          run(config) -> BacktestResult — only public entry
    cli.py             `python -m bot.backtesting.cli run [...]`
"""
from __future__ import annotations

# Version constant for manifest reproducibility metadata.
# Bump on any change to engine semantics (signal timing, SL/TP rules,
# sizing, fees, slippage, output format).
ENGINE_VERSION = "M17.A.1"

__all__ = ["ENGINE_VERSION"]
