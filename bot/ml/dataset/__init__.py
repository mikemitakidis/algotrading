"""bot.ml.dataset — dataset assembly subpackage.

Houses:
  m16_loader.py   sole bot.historical importer in production bot/ml/*
                  (SR-7 — enforced by G10 AST guard in test_m18_ml.py).
                  Reads OHLCV bars from M16's Parquet store. NO writes,
                  NO provider calls, NO yfinance.

Future modules (lands in later M18 phases):
  assembler.py    feature/label join + manifest emission (M18.A.5)
"""
from __future__ import annotations

from bot.ml.dataset.m16_loader import (
    load_bars,
    validate_lookback_coverage,
)

__all__ = ["load_bars", "validate_lookback_coverage"]
