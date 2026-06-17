"""M20.A paper-trading configuration.

Frozen, standard-library dataclass. Risk / slippage / commission fields are
DEFINED here but not actively used until later phases (M20.C clean-room sizing,
M20.D fills). No live/broker semantics. No I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any, Dict

SCHEMA_VERSION = "m20_paper_config_v1"


@dataclass(frozen=True)
class PaperTradingConfig:
    paper_equity: float = 100000.0
    risk_per_trade_pct: float = 1.0
    max_open_paper_positions: int = 10
    max_symbol_paper_exposure: float = 0.20
    slippage_bps: float = 0.0
    commission_bps: float = 0.0
    spread_assumption_bps: float = 0.0
    schema_version: str = SCHEMA_VERSION
    IS_LIVE: bool = field(default=False, init=False)

    def __post_init__(self):
        self.validate()

    def validate(self) -> None:
        if not (isinstance(self.paper_equity, (int, float))
                and self.paper_equity > 0):
            raise ValueError("paper_equity must be a positive number")
        if not (0 < self.risk_per_trade_pct <= 100):
            raise ValueError("risk_per_trade_pct must be in (0, 100]")
        if not (isinstance(self.max_open_paper_positions, int)
                and self.max_open_paper_positions > 0):
            raise ValueError("max_open_paper_positions must be a positive int")
        if not (0 < self.max_symbol_paper_exposure <= 1.0):
            raise ValueError("max_symbol_paper_exposure must be in (0, 1]")
        for name in ("slippage_bps", "commission_bps", "spread_assumption_bps"):
            v = getattr(self, name)
            if not (isinstance(v, (int, float)) and v >= 0):
                raise ValueError(f"{name} must be a non-negative number")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["IS_LIVE"] = False
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PaperTradingConfig":
        allowed = {f for f in cls.__dataclass_fields__ if f != "IS_LIVE"}
        unknown = set(d) - allowed - {"IS_LIVE"}
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        return cls(**{k: v for k, v in d.items() if k in allowed})


def default_paper_config() -> PaperTradingConfig:
    return PaperTradingConfig()
