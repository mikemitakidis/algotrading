"""bot.backtesting.config — typed configuration for a backtest run.

A backtest is described by three logically-separate concerns:

  * BacktestRequest    — what symbol, what timeframe, what date range
  * StrategyConfig     — strategy name + strategy-specific parameters
  * ExecutionConfig    — fees, slippage, sizing, SL/TP, initial equity

These are combined into a single BacktestConfig envelope which is the
sole input to runner.run().

JSON format (see configs/backtests/example_sma_aapl.json):

    {
      "request": {
        "symbol":    "AAPL",
        "timeframe": "1D",
        "start":     "2024-01-01",
        "end":       "2024-12-31"
      },
      "data": {
        "adjusted": true,
        "provider": "yfinance"
      },
      "strategy": {
        "name":   "sma_crossover",
        "params": {
          "fast_window": 20,
          "slow_window": 50
        }
      },
      "execution": {
        "initial_equity":      10000.0,
        "fee_bps":             5,
        "slippage_bps":        5,
        "stop_loss_pct":       0.03,
        "take_profit_pct":     0.06,
        "risk_per_trade_pct":  0.01,
        "max_position_pct":    0.25,
        "allow_short":         false
      }
    }

Hard rules:
  * `allow_short=true` is REJECTED in M17.A (deferred to M17.C).
  * Unknown strategy names are rejected loudly (registered names only).
  * Date parsing accepts ISO date strings ('2024-01-01'); CLI dates are
    operator-natural INCLUSIVE on both ends. The data_loader converts
    to M16's `start_utc` inclusive / `end_utc` exclusive convention.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.backtesting.errors import ConfigError


# --- M16-compatible timeframe set (mirrors bot.historical.schema) -----
# Listed here as a literal (not imported) to keep config.py from
# touching bot.historical at all. The runtime path that DOES touch
# bot.historical is data_loader.py.
_ALLOWED_TIMEFRAMES = ("1D", "4H", "1H", "15m")

# --- Registered strategy names (M17.A) --------------------------------
# scanner_replica (and friends) land in M17.B; tests assert this is the
# exact set of names accepted in M17.A.
_M17A_STRATEGIES = frozenset({"sma_crossover"})


# ─────────────────────────────────────────────────────────────────────
# Sub-configs
# ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BacktestRequest:
    """What to backtest. Date semantics: BOTH inclusive at the CLI/config
    boundary. data_loader converts to M16's inclusive/exclusive convention."""
    symbol:    str
    timeframe: str
    start:     date          # INCLUSIVE
    end:       date          # INCLUSIVE


@dataclass(frozen=True)
class DataConfig:
    """How to read M16 data."""
    adjusted: bool = True
    provider: str = "yfinance"


@dataclass(frozen=True)
class StrategyConfig:
    """Strategy name + opaque-to-the-engine parameter dict.

    The strategy implementation is responsible for validating its own
    `params` content. The engine only validates that `name` is a known
    strategy and that `params` is a dict.
    """
    name:   str
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionConfig:
    """Money mechanics: fees, slippage, sizing, SL/TP, initial equity.

    SL/TP are percentage-based in M17.A (ATR-based ships with
    scanner_replica in M17.B). `None` means the channel is disabled.
    """
    initial_equity:     float = 10_000.0
    fee_bps:            float = 5.0      # per side
    slippage_bps:       float = 5.0      # per side
    stop_loss_pct:      Optional[float] = None
    take_profit_pct:    Optional[float] = None
    risk_per_trade_pct: float = 0.01     # fraction of equity risked per trade
    max_position_pct:   float = 0.25     # cap on notional / equity
    allow_short:        bool = False     # M17.A: must be False


@dataclass(frozen=True)
class BacktestConfig:
    """The single input envelope to runner.run()."""
    request:   BacktestRequest
    data:      DataConfig
    strategy:  StrategyConfig
    execution: ExecutionConfig


# ─────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────

def parse_config_file(path: str | Path) -> BacktestConfig:
    """Load and validate a JSON config file. Returns a validated
    BacktestConfig or raises ConfigError with a precise message."""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    try:
        raw = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise ConfigError(f"config file {p} is not valid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(
            f"config file {p} top-level must be a JSON object, "
            f"got {type(raw).__name__}")
    return parse_config_dict(raw)


def parse_config_dict(raw: Dict[str, Any]) -> BacktestConfig:
    """Validate + structure-check a config dict. Single source of truth
    for what is and isn't a legal config."""

    if not isinstance(raw, dict):
        raise ConfigError(
            f"config must be a dict, got {type(raw).__name__}")

    # ---- request -----------------------------------------------------
    req_raw = raw.get("request")
    if not isinstance(req_raw, dict):
        raise ConfigError("config.request must be a dict")
    for k in ("symbol", "timeframe", "start", "end"):
        if k not in req_raw:
            raise ConfigError(f"config.request.{k} is required")
    symbol = req_raw["symbol"]
    if not isinstance(symbol, str) or not symbol.strip():
        raise ConfigError("config.request.symbol must be a non-empty string")
    symbol = symbol.strip().upper()
    timeframe = req_raw["timeframe"]
    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise ConfigError(
            f"config.request.timeframe must be one of {_ALLOWED_TIMEFRAMES}, "
            f"got {timeframe!r}")
    start = _parse_iso_date(req_raw["start"], "config.request.start")
    end   = _parse_iso_date(req_raw["end"],   "config.request.end")
    if start > end:
        raise ConfigError(
            f"config.request.start ({start}) must be <= "
            f"config.request.end ({end})")
    request = BacktestRequest(
        symbol=symbol, timeframe=timeframe, start=start, end=end)

    # ---- data --------------------------------------------------------
    data_raw = raw.get("data", {}) or {}
    if not isinstance(data_raw, dict):
        raise ConfigError("config.data must be a dict")
    adjusted = data_raw.get("adjusted", True)
    if not isinstance(adjusted, bool):
        raise ConfigError("config.data.adjusted must be a boolean")
    provider = data_raw.get("provider", "yfinance")
    if not isinstance(provider, str) or not provider:
        raise ConfigError("config.data.provider must be a non-empty string")
    data = DataConfig(adjusted=adjusted, provider=provider)

    # ---- strategy ----------------------------------------------------
    strat_raw = raw.get("strategy")
    if not isinstance(strat_raw, dict):
        raise ConfigError("config.strategy must be a dict")
    name = strat_raw.get("name")
    if not isinstance(name, str):
        raise ConfigError("config.strategy.name must be a string")
    if name not in _M17A_STRATEGIES:
        raise ConfigError(
            f"unknown strategy {name!r}. M17.A registered strategies: "
            f"{sorted(_M17A_STRATEGIES)}. scanner_replica and friends "
            f"land in M17.B.")
    params = strat_raw.get("params", {}) or {}
    if not isinstance(params, dict):
        raise ConfigError("config.strategy.params must be a dict")
    strategy = StrategyConfig(name=name, params=dict(params))

    # ---- execution ---------------------------------------------------
    exec_raw = raw.get("execution", {}) or {}
    if not isinstance(exec_raw, dict):
        raise ConfigError("config.execution must be a dict")
    execution = _parse_execution(exec_raw)

    return BacktestConfig(
        request=request, data=data, strategy=strategy, execution=execution)


def _parse_execution(raw: Dict[str, Any]) -> ExecutionConfig:
    """Validate the execution sub-config. All numeric fields are
    range-checked."""
    def _num(key: str, default: float, *,
              min_v: Optional[float] = None,
              max_v: Optional[float] = None,
              allow_none: bool = False) -> Optional[float]:
        v = raw.get(key, default)
        if v is None:
            if allow_none:
                return None
            raise ConfigError(f"config.execution.{key} must not be null")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ConfigError(
                f"config.execution.{key} must be a number, got "
                f"{type(v).__name__}")
        fv = float(v)
        if min_v is not None and fv < min_v:
            raise ConfigError(
                f"config.execution.{key}={fv} must be >= {min_v}")
        if max_v is not None and fv > max_v:
            raise ConfigError(
                f"config.execution.{key}={fv} must be <= {max_v}")
        return fv

    initial_equity = _num("initial_equity", 10_000.0, min_v=0.01)
    fee_bps        = _num("fee_bps",        5.0,     min_v=0.0,  max_v=10_000.0)
    slippage_bps   = _num("slippage_bps",   5.0,     min_v=0.0,  max_v=10_000.0)
    stop_loss_pct  = _num("stop_loss_pct",
                            raw.get("stop_loss_pct"),
                            min_v=0.0, max_v=1.0,
                            allow_none=True)
    take_profit_pct = _num("take_profit_pct",
                              raw.get("take_profit_pct"),
                              min_v=0.0, max_v=10.0,
                              allow_none=True)
    risk_per_trade_pct = _num("risk_per_trade_pct", 0.01,
                                  min_v=1e-6, max_v=1.0)
    max_position_pct = _num("max_position_pct", 0.25,
                                min_v=1e-6, max_v=1.0)

    allow_short = raw.get("allow_short", False)
    if not isinstance(allow_short, bool):
        raise ConfigError("config.execution.allow_short must be a boolean")
    if allow_short:
        raise ConfigError(
            "config.execution.allow_short=True is rejected in M17.A. "
            "Short selling lands in a later sub-milestone.")

    # If stop_loss_pct is None, risk-per-trade sizing falls back to
    # cap-only sizing — but that's a strategy concern, not invalid here.
    return ExecutionConfig(
        initial_equity=initial_equity,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        risk_per_trade_pct=risk_per_trade_pct,
        max_position_pct=max_position_pct,
        allow_short=allow_short,
    )


def _parse_iso_date(v: Any, field_name: str) -> date:
    """Accept 'YYYY-MM-DD' string or datetime.date. Reject everything else."""
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str):
        try:
            return date.fromisoformat(v)
        except ValueError as e:
            raise ConfigError(
                f"{field_name}={v!r} is not a valid ISO date "
                f"(expected YYYY-MM-DD): {e}") from e
    raise ConfigError(
        f"{field_name} must be a date or 'YYYY-MM-DD' string, "
        f"got {type(v).__name__}")


# ─────────────────────────────────────────────────────────────────────
# Hashing (for reproducibility)
# ─────────────────────────────────────────────────────────────────────

def config_hash(cfg: BacktestConfig, *, length: int = 12) -> str:
    """Deterministic short hash of a BacktestConfig. Used in run_id
    and as a reproducibility key in manifest.json.

    Same config object -> same hash, always. Date objects are
    serialised as ISO strings so two date(2024,1,1) values hash the same
    regardless of source.
    """
    canonical = json.dumps(
        config_to_dict(cfg),
        sort_keys=True,
        separators=(",", ":"),
        default=_canonical_default,
    )
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return h[:length]


def config_to_dict(cfg: BacktestConfig) -> Dict[str, Any]:
    """Serialise a BacktestConfig to a plain dict suitable for JSON.
    Dates -> ISO strings. Used by config_hash and by output.write_results."""
    return {
        "request": {
            "symbol":    cfg.request.symbol,
            "timeframe": cfg.request.timeframe,
            "start":     cfg.request.start.isoformat(),
            "end":       cfg.request.end.isoformat(),
        },
        "data": asdict(cfg.data),
        "strategy": {
            "name":   cfg.strategy.name,
            "params": dict(cfg.strategy.params),
        },
        "execution": asdict(cfg.execution),
    }


def _canonical_default(o: Any) -> Any:
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(f"not JSON-serialisable: {type(o).__name__}")


# ─────────────────────────────────────────────────────────────────────
# Public surface
# ─────────────────────────────────────────────────────────────────────

__all__ = [
    "BacktestRequest", "DataConfig", "StrategyConfig",
    "ExecutionConfig", "BacktestConfig",
    "parse_config_file", "parse_config_dict",
    "config_hash", "config_to_dict",
]
