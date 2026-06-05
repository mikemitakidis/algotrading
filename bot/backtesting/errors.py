"""bot.backtesting.errors — typed error classes for the M17 engine.

The error message is the operator-facing contract. Every error must
either (a) tell the operator exactly what command to run to fix it,
or (b) be a precise statement of a configuration mistake. No bare
`Exception` raises anywhere in the package.
"""
from __future__ import annotations


class BacktestError(Exception):
    """Base for all M17 backtesting errors. Never raised directly."""


class ConfigError(BacktestError):
    """Raised when a BacktestRequest / StrategyConfig / ExecutionConfig
    fails validation, when a JSON config file is malformed, or when an
    unknown strategy name is requested.
    """


class MissingDataError(BacktestError):
    """Raised by `bot.backtesting.data_loader` when the M16 store does
    not have sufficient data for the requested backtest. Message MUST
    include the exact `python -m bot.historical.cli backfill` command
    the operator needs to run.

    Example:
        raise MissingDataError(
            "No bars in M16 store for AAPL 1D in 2024-01-01..2024-12-31.\\n"
            "Run this first:\\n"
            "  python -m bot.historical.cli backfill "
            "--symbols AAPL --timeframes 1D --start 2024-01-01 --end 2024-12-31"
        )
    """


class StrategyError(BacktestError):
    """Raised when a strategy implementation produces malformed output
    (missing required columns, wrong index, etc.) or when a strategy
    contract is violated at runtime.
    """
