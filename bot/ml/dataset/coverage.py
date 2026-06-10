"""bot.ml.dataset.coverage — Q19 intraday-coverage assessment.

Q19 (locked):

  1. The default dataset builder MAY degrade to whatever timeframes
     are available, but ONLY for diagnostic/infrastructure datasets.
     Production scanner_replica meta-label datasets require full
     coverage.
  2. Degradation MUST NEVER be silent — the manifest carries an
     explicit `degradation_warning` and `coverage_degraded=True` flag,
     and `promotion_eligible=False` is enforced downstream.
  3. `require_intraday=True` MUST fail with
     `InsufficientIntradayCoverageError` whose message includes:
         * the exact missing TFs
         * the symbol
         * the suggested M16 backfill command(s) to fix it
  4. A degraded dataset/model MUST NOT be treated as a full
     scanner_replica meta-label model. Downstream promotion logic
     (M18.A.8) reads `promotion_eligible` from the manifest and
     refuses to register a non-eligible dataset as a full model.

This module is a pure assessor — it returns a `CoverageReport`. The
assembler decides whether to raise vs degrade based on its
`require_intraday` config.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Tuple

import pandas as pd


# Required intraday timeframes for a FULL scanner_replica dataset.
# 1D is required at minimum for trend features (SMA200 etc.). 1H +
# 4H + 15m form the multi-TF confluence picture.
REQUIRED_INTRADAY_TFS: FrozenSet[str] = frozenset({"15m", "1H", "4H"})
REQUIRED_DAILY_TF: str = "1D"

# Minimum bars-per-TF for "full coverage". Calibrated to:
#   * 200 bars covers EMA(200) warmup with a buffer
#   * 60 bars covers ATR-percentile-60 + rolling regimes
#   * 252 bars covers liquidity_bucket
# 200 is the binding constraint for most safe features.
DEFAULT_MIN_BARS_PER_TF: int = 200


@dataclass(frozen=True)
class CoverageReport:
    """Outcome of `assess_intraday_coverage()`.

    Q19-aligned field naming:
      coverage_degraded     True iff NOT full coverage (semantic
                              inverse of the older is_full_coverage_*)
      degradation_warning   Human-readable explanation of what's wrong,
                              or None when coverage_degraded=False.

    Categorisation of TFs:
      available_tfs   ALL TFs that were SUPPLIED with at least one
                        bar (regardless of bar count); used by the
                        manifest's `available_timeframes` field.
      present_tfs     TFs supplied with >= min_bars_per_tf — these
                        contribute to a "full" assessment.
      degraded_tfs    TFs supplied with < min_bars_per_tf.
      missing_tfs     Required TFs not supplied at all (or supplied
                        empty).
    """
    coverage_degraded:    bool
    degradation_warning:  Optional[str]
    available_tfs:        Tuple[str, ...]
    present_tfs:          Tuple[str, ...]
    degraded_tfs:         Tuple[str, ...]
    missing_tfs:          Tuple[str, ...]
    bar_counts:           Dict[str, int]

    def assert_promotable_or_raise(self, *, symbol: str) -> None:
        """Raise InsufficientIntradayCoverageError if the report is
        degraded. The error message contains the symbol, the missing
        TF list, and the exact M16 backfill command(s) to run.

        Used by the assembler when require_intraday=True. Callers
        building diagnostic datasets pass require_intraday=False and
        skip this check — see assembler.py.
        """
        if not self.coverage_degraded:
            return
        from bot.ml.errors import InsufficientIntradayCoverageError
        from bot.ml.dataset._m16_backfill import format_backfill_command
        # One single combined M16 backfill command that covers every
        # degraded or missing TF as a CSV. _parse_csv_list in the M16
        # CLI accepts CSV for both --symbols and --timeframes (verified
        # against bot/historical/cli.py).
        bad_tfs = sorted(set(self.missing_tfs) | set(self.degraded_tfs))
        if bad_tfs:
            cmd_block = format_backfill_command(symbol, bad_tfs)
        else:
            cmd_block = "    (no specific missing TFs identified)"
        raise InsufficientIntradayCoverageError(
            f"Intraday coverage insufficient for a full scanner_replica "
            f"dataset on symbol={symbol!r}:\n"
            f"  warning:     {self.degradation_warning}\n"
            f"  present:     {sorted(self.present_tfs)}\n"
            f"  degraded:    {sorted(self.degraded_tfs)}\n"
            f"  missing:     {sorted(self.missing_tfs)}\n"
            f"  bar_counts:  {self.bar_counts}\n"
            f"To backfill via M16, run:\n"
            f"{cmd_block}\n"
            f"Or pass require_intraday=False to build a diagnostic-only "
            f"dataset; the manifest will record coverage_degraded=True "
            f"and promotion_eligible=False, which downstream M18.A.8 "
            f"promotion logic will refuse to register as a full "
            f"scanner_replica meta-label model."
        )


def assess_intraday_coverage(
    per_tf_bars: Dict[str, Optional[pd.DataFrame]],
    *,
    min_bars_per_tf: int = DEFAULT_MIN_BARS_PER_TF,
    required_intraday: FrozenSet[str] = REQUIRED_INTRADAY_TFS,
    required_daily: str = REQUIRED_DAILY_TF,
) -> CoverageReport:
    """Assess whether the supplied bars meet 'full coverage'
    criteria for a promotable scanner_replica meta-label dataset.

    Parameters
    ----------
    per_tf_bars
        Dict TF_label -> bars DataFrame. None / empty entries count
        as 'missing'.
    min_bars_per_tf
        Minimum bars per TF for "full" coverage (default 200).
    required_intraday
        Intraday TFs that must each be present with min_bars_per_tf
        (default {15m, 1H, 4H}).
    required_daily
        Daily TF label that must also be present (default "1D").

    Returns
    -------
    CoverageReport
    """
    if min_bars_per_tf < 1:
        raise ValueError(
            f"min_bars_per_tf must be >= 1, got {min_bars_per_tf}")

    bar_counts: Dict[str, int] = {}
    present_tfs:   List[str] = []
    degraded_tfs:  List[str] = []
    missing_tfs:   List[str] = []
    available_tfs: List[str] = []     # any TF with >= 1 bar

    full_required: FrozenSet[str] = required_intraday | {required_daily}

    for tf in sorted(full_required):
        df = per_tf_bars.get(tf)
        if df is None or len(df) == 0:
            missing_tfs.append(tf)
            bar_counts[tf] = 0
            continue
        n = int(len(df))
        bar_counts[tf] = n
        available_tfs.append(tf)
        if n < min_bars_per_tf:
            degraded_tfs.append(tf)
        else:
            present_tfs.append(tf)

    is_full = (len(missing_tfs) == 0 and len(degraded_tfs) == 0)

    degradation_warning: Optional[str]
    if is_full:
        degradation_warning = None
    elif missing_tfs:
        degradation_warning = (
            f"missing required TFs {sorted(missing_tfs)}"
            + (f"; below-min TFs {sorted(degraded_tfs)} "
                f"(min={min_bars_per_tf})" if degraded_tfs else ""))
    else:  # only degraded, none missing
        degradation_warning = (
            f"TFs below min_bars_per_tf={min_bars_per_tf}: "
            f"{sorted(degraded_tfs)} "
            f"(counts: {{{', '.join(f'{t}={bar_counts[t]}' for t in sorted(degraded_tfs))}}})")

    return CoverageReport(
        coverage_degraded=(not is_full),
        degradation_warning=degradation_warning,
        available_tfs=tuple(sorted(available_tfs)),
        present_tfs=tuple(sorted(present_tfs)),
        degraded_tfs=tuple(sorted(degraded_tfs)),
        missing_tfs=tuple(sorted(missing_tfs)),
        bar_counts=dict(bar_counts),
    )
