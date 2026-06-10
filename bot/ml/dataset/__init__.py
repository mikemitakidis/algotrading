"""bot.ml.dataset — dataset assembly subpackage.

Houses:
  m16_loader.py           sole bot.historical importer in production
                            bot/ml/* (SR-7 — enforced by G10 AST guard)
  flywheel_reader.py      read-only sqlite3 access to the live
                            signal_outcomes table (M18.A.3)
  anchors.py              Model A / Model B anchor enumeration (Q18)
  coverage.py             intraday-coverage assessment (Q19)
  manifest.py             dataset hash + manifest schema
  walk_forward.py         purged train/val/test split with embargo
  adversarial_validation.py
                          sklearn-based LR + CV AUC + 0.55 gate
  assembler.py            end-to-end orchestrator
"""
from __future__ import annotations

from bot.ml.dataset.m16_loader import (
    load_bars,
    validate_lookback_coverage,
)

__all__ = ["load_bars", "validate_lookback_coverage"]
