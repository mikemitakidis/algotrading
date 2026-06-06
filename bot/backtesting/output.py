"""bot.backtesting.output — write backtest results to disk.

Single public function `write_results(result, output_dir)` produces a
new immutable run directory:

    <output_dir>/<timestamp>_<strategy>_<config_hash>/
      manifest.json
      report.json
      trades.csv
      trades.jsonl
      equity_curve.csv
      warnings.json

Reproducibility guarantee:
  Same BacktestConfig + same M16 bars + same engine version  ->
  identical content in every artifact EXCEPT manifest.json's
  `run_id` and `created_at_utc` fields. Test asserts this in G9.

Manifest schema (top-level):
  run_id, created_at_utc                    # unique per run
  engine_version                            # bot.backtesting.ENGINE_VERSION
  config                                    # full echoed config dict
  config_hash                               # 12-char sha256[:12]
  coverage_metadata                         # M16 coverage row at load
  strategy_module_sha256                    # SHA256 of strategy.py
  git_head_sha                              # str | 'unknown'
  python_version, pandas_version, numpy_version
  bars_processed, trade_count, warning_count
"""
from __future__ import annotations

import csv
import hashlib
import json
import platform
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pandas as pd

from bot.backtesting import ENGINE_VERSION
from bot.backtesting.config import BacktestConfig, config_hash, config_to_dict
from bot.backtesting.models import BacktestResult


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────

def write_results(
    result: BacktestResult,
    cfg: BacktestConfig,
    output_dir: Path | str,
    *,
    run_id: str | None = None,
    created_at_utc: datetime | None = None,
) -> Path:
    """Write a complete backtest run directory. Returns the new dir path.

    The directory name is `<timestamp>_<strategy>_<config_hash>/`,
    placed under `output_dir`. If `run_id` / `created_at_utc` are
    omitted they're generated fresh (current UTC time + cfg-derived
    run_id).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cfg_hash = config_hash(cfg)
    if created_at_utc is None:
        created_at_utc = datetime.now(timezone.utc)
    if run_id is None:
        run_id = build_run_id(cfg, created_at_utc=created_at_utc,
                                  cfg_hash=cfg_hash)

    run_dir = output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    # ---- manifest.json ----------------------------------------------
    manifest = _build_manifest(
        result=result, cfg=cfg, cfg_hash=cfg_hash,
        run_id=run_id, created_at_utc=created_at_utc)
    _write_json(run_dir / "manifest.json", manifest)

    # ---- report.json ------------------------------------------------
    report = {
        "run_id":         run_id,
        "engine_version": ENGINE_VERSION,
        "config_hash":    cfg_hash,
        "metrics":        result.metrics,
        "trade_count":    result.trade_count,
        "warning_count":  result.warning_count,
        "bars_processed": result.bars_processed,
    }
    _write_json(run_dir / "report.json", report)

    # ---- trades.csv + trades.jsonl ----------------------------------
    _write_trades_csv(run_dir / "trades.csv", result.trades)
    _write_trades_jsonl(run_dir / "trades.jsonl", result.trades)

    # ---- equity_curve.csv -------------------------------------------
    _write_equity_csv(run_dir / "equity_curve.csv", result.equity_curve)

    # ---- warnings.json ----------------------------------------------
    _write_warnings_json(run_dir / "warnings.json", result.warnings)

    return run_dir


def build_run_id(cfg: BacktestConfig, *, created_at_utc: datetime,
                    cfg_hash: str) -> str:
    """Build the canonical run-id string:
        <YYYYMMDDTHHMMSSZ>_<strategy>_<config_hash>
    """
    ts = created_at_utc.strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}_{cfg.strategy.name}_{cfg_hash}"


# ─────────────────────────────────────────────────────────────────────
# Builders
# ─────────────────────────────────────────────────────────────────────

def _build_manifest(*, result: BacktestResult, cfg: BacktestConfig,
                       cfg_hash: str, run_id: str,
                       created_at_utc: datetime) -> Dict[str, Any]:
    return {
        "run_id":                run_id,
        "created_at_utc":        _iso_z(created_at_utc),
        "engine_version":        ENGINE_VERSION,
        "config":                config_to_dict(cfg),
        "config_hash":           cfg_hash,
        "coverage_metadata":     _serialise_coverage(result.coverage_metadata),
        "strategy_module_sha256": _module_sha256("bot/backtesting/strategy.py"),
        "git_head_sha":          _git_head_sha(),
        "python_version":        platform.python_version(),
        "pandas_version":        pd.__version__,
        "numpy_version":         np.__version__,
        "bars_processed":        int(result.bars_processed),
        "trade_count":           int(result.trade_count),
        "warning_count":         int(result.warning_count),
    }


def _serialise_coverage(cov: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce the M16 coverage row to JSON-safe types. Timestamps ->
    ISO strings; numpy ints/floats -> Python ints/floats."""
    out: Dict[str, Any] = {}
    for k, v in (cov or {}).items():
        if isinstance(v, (pd.Timestamp, datetime)):
            out[k] = _iso_z(v)
        elif isinstance(v, (np.integer,)):
            out[k] = int(v)
        elif isinstance(v, (np.floating,)):
            out[k] = float(v)
        else:
            out[k] = v
    return out


# ─────────────────────────────────────────────────────────────────────
# File writers
# ─────────────────────────────────────────────────────────────────────

_TRADE_CSV_FIELDS = (
    "symbol", "direction", "qty",
    "entry_ts_utc", "entry_price",
    "exit_ts_utc",  "exit_price",
    "exit_reason", "fees_paid", "slippage_paid",
    "pnl_absolute", "pnl_pct", "bars_held",
)

_EQUITY_CSV_FIELDS = (
    "ts_utc", "equity", "cash",
    "position_qty", "position_market_value",
)


def _write_trades_csv(path: Path, trades) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(_TRADE_CSV_FIELDS)
        for t in trades:
            w.writerow([
                t.symbol, t.direction, t.qty,
                _iso_z(t.entry_ts_utc), t.entry_price,
                _iso_z(t.exit_ts_utc),  t.exit_price,
                t.exit_reason, t.fees_paid, t.slippage_paid,
                t.pnl_absolute, t.pnl_pct, t.bars_held,
            ])


def _write_trades_jsonl(path: Path, trades) -> None:
    with open(path, "w") as f:
        for t in trades:
            row = {
                "symbol":        t.symbol,
                "direction":     t.direction,
                "qty":           t.qty,
                "entry_ts_utc":  _iso_z(t.entry_ts_utc),
                "entry_price":   t.entry_price,
                "exit_ts_utc":   _iso_z(t.exit_ts_utc),
                "exit_price":    t.exit_price,
                "exit_reason":   t.exit_reason,
                "fees_paid":     t.fees_paid,
                "slippage_paid": t.slippage_paid,
                "pnl_absolute":  t.pnl_absolute,
                "pnl_pct":       t.pnl_pct,
                "bars_held":     t.bars_held,
            }
            f.write(json.dumps(row, sort_keys=True) + "\n")


def _write_equity_csv(path: Path, equity_points) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(_EQUITY_CSV_FIELDS)
        for e in equity_points:
            w.writerow([
                _iso_z(e.ts_utc), e.equity, e.cash,
                e.position_qty, e.position_market_value,
            ])


def _write_warnings_json(path: Path, warnings) -> None:
    rows = []
    for w in warnings:
        rows.append({
            "code":    w.code,
            "message": w.message,
            "ts_utc":  _iso_z(w.ts_utc) if w.ts_utc is not None else None,
            "extras":  dict(w.extras),
        })
    _write_json(path, rows)


def _write_json(path: Path, data: Any) -> None:
    """Deterministic JSON: sorted keys, 2-space indent, trailing newline."""
    with open(path, "w") as f:
        json.dump(data, f, sort_keys=True, indent=2, default=_json_default)
        f.write("\n")


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _iso_z(v) -> str:
    """ISO 8601 with explicit Z suffix for UTC."""
    if v is None:
        return None
    if isinstance(v, pd.Timestamp):
        v = v.to_pydatetime()
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        else:
            v = v.astimezone(timezone.utc)
        # Use ISO format with Z suffix; strip microseconds for stable hashes.
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(v)


def _json_default(o):
    if isinstance(o, (datetime, pd.Timestamp)):
        return _iso_z(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    raise TypeError(f"not JSON-serialisable: {type(o).__name__}")


def _module_sha256(rel_path: str) -> str:
    """SHA256 of a source file's bytes. Used in manifest for the
    strategy module so a future run can detect a code change."""
    p = Path(rel_path)
    if not p.exists():
        return "unknown"
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def _git_head_sha() -> str:
    """Best-effort HEAD short SHA; 'unknown' if git isn't available
    or this isn't a repo. Manifest reproducibility doesn't depend on
    this — it's informational."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2)
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


__all__ = ["write_results", "build_run_id"]
