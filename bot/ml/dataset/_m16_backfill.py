"""bot.ml.dataset._m16_backfill — single source of truth for the
M16 backfill CLI command string used in M18 error messages.

Centralised here so coverage.py and assembler.py cannot drift from
the actual M16 CLI surface. The G4 test
`G4_M16Backfill.test_command_matches_actual_m16_cli` introspects
the real argparse parser and asserts that the subcommand and
argument names emitted here are valid.

Verified against bot/historical/cli.py at M18.A.5 commit time:

    python -m bot.historical.cli backfill [-h] [--symbols SYMBOLS]
                                          [--timeframes TIMEFRAMES]
                                          [--lookback LOOKBACK]

Both --symbols and --timeframes accept comma-separated values
(verified by _parse_csv_list in bot/historical/cli.py).

If the M16 CLI surface changes, update the constants in this module;
the G4 test will catch the drift.
"""
from __future__ import annotations

from typing import Iterable, Union


# Public-API constants. Kept simple so a refactor of M16 only requires
# updating these three lines.
M16_CLI_MODULE         = "bot.historical.cli"
M16_BACKFILL_SUBCOMMAND = "backfill"
M16_BACKFILL_SYMBOLS_FLAG    = "--symbols"
M16_BACKFILL_TIMEFRAMES_FLAG = "--timeframes"


def format_backfill_command(
    symbol: str,
    timeframes: Union[str, Iterable[str]],
    *,
    indent: str = "    ",
) -> str:
    """Return the canonical M16 backfill command line as a string.

    Parameters
    ----------
    symbol : str
        Symbol identifier (e.g. "AAPL"). Single-symbol only — the
        M18.A.5 assembler is single-symbol.
    timeframes : str | Iterable[str]
        Either one TF as a string (e.g. "4H"), or an iterable of
        TF strings (e.g. ("15m", "1H", "4H")) which will be joined
        with commas into a CSV. The M16 CLI parses both forms via
        _parse_csv_list (verified).
    indent : str
        Leading indent for the command line so it renders cleanly
        inside multi-line error messages. Default 4 spaces.

    Returns
    -------
    str
        e.g. "    python -m bot.historical.cli backfill "
             "--symbols AAPL --timeframes 4H,1H"
    """
    if isinstance(timeframes, str):
        tf_csv = timeframes
    else:
        # Preserve caller-supplied order; do NOT sort here so that
        # tests can pass an explicit ordering.
        tf_csv = ",".join(timeframes)
    return (
        f"{indent}python -m {M16_CLI_MODULE} {M16_BACKFILL_SUBCOMMAND} "
        f"{M16_BACKFILL_SYMBOLS_FLAG} {symbol} "
        f"{M16_BACKFILL_TIMEFRAMES_FLAG} {tf_csv}"
    )
