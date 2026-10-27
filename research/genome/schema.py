"""research.genome.schema — Strategy Genome v0 (canonical B09).

A genome is a strategy written as DATA: a template id, typed parameters with
declared bounds, explicit exits (a stop is mandatory), a risk budget and a
timeframe. It has a semantic id (the digest of everything that changes
behaviour) and lineage (the parent it was mutated from, and why).

The validator is fail-closed. It rejects:
- unknown templates, parameters or keys;
- out-of-range or wrongly typed values;
- a missing stop;
- shorts (M17 execution is long-only);
- risk above the hard caps.

All the reasons are reported together. Genomes are research objects only:
nothing here can place an order, and no genome reaches a broker path.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np

from research.common.hashing import digest

SCHEMA_VERSION = 0
TIMEFRAMES = ("1D", "15m")
MAX_RISK_PER_TRADE = 0.02
MAX_POSITION_PCT = 0.25


class GenomeError(ValueError):
    def __init__(self, reasons: List[str]):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True)
class ParamSpec:
    kind: str                     # "int" | "float" | "choice"
    default: Any
    lo: Optional[float] = None
    hi: Optional[float] = None
    choices: Tuple[Any, ...] = ()
    step: Optional[float] = None  # grid step used by the optimiser

    def check(self, name: str, v: Any) -> Optional[str]:
        if self.kind == "int":
            if isinstance(v, bool) or not isinstance(v, (int, np.integer)):
                return "%s must be an int, got %r" % (name, v)
        elif self.kind == "float":
            if isinstance(v, bool) or not isinstance(v, (int, float, np.integer, np.floating)) or not np.isfinite(v):
                return "%s must be a finite number, got %r" % (name, v)
        elif self.kind == "choice":
            return None if v in self.choices else "%s must be one of %s, got %r" % (name, self.choices, v)
        if self.lo is not None and v < self.lo or self.hi is not None and v > self.hi:
            return "%s=%r outside [%s, %s]" % (name, v, self.lo, self.hi)
        return None


@dataclass(frozen=True)
class TemplateSpec:
    template_id: str
    description: str
    params: Mapping[str, ParamSpec]
    constraints: Tuple[Tuple[str, str], ...] = ()      # (a, b): params[a] < params[b]


@dataclass(frozen=True)
class ExitSpec:
    stop_atr: float = 2.0                  # mandatory protective stop, ATR multiples below entry
    target_atr: Optional[float] = 3.0      # None: no fixed target
    time_stop_bars: int = 20               # exit at the open after this many bars held
    trail_atr: Optional[float] = None      # trailing stop below the highest close since entry
    signal_exit: bool = True               # honour the template's own exit signal


@dataclass(frozen=True)
class RiskSpec:
    risk_per_trade: float = 0.01
    max_position_pct: float = 0.20


@dataclass(frozen=True)
class Genome:
    template: str
    params: Tuple[Tuple[str, Any], ...]
    exits: ExitSpec = ExitSpec()
    risk: RiskSpec = RiskSpec()
    timeframe: str = "1D"
    direction: str = "long"
    parent_id: str = ""
    origin: str = "seed"                   # "seed" | "mutation" | "grid" | "manual"
    schema_version: int = SCHEMA_VERSION

    @property
    def p(self) -> Dict[str, Any]:
        return dict(self.params)

    def behaviour(self) -> Dict[str, Any]:
        """Everything that changes trading behaviour (lineage excluded)."""
        return {"template": self.template, "params": dict(self.params), "exits": asdict(self.exits),
                "risk": asdict(self.risk), "timeframe": self.timeframe, "direction": self.direction,
                "schema_version": self.schema_version}

    @property
    def genome_id(self) -> str:
        return "%s-%s" % (self.template, digest(self.behaviour())[:12])

    def to_dict(self) -> Dict[str, Any]:
        d = self.behaviour()
        d.update({"parent_id": self.parent_id, "origin": self.origin, "genome_id": self.genome_id})
        return d


_TOP_KEYS = {"template", "params", "exits", "risk", "timeframe", "direction", "parent_id", "origin",
             "schema_version", "genome_id"}


def from_dict(d: Mapping[str, Any]) -> "Genome":
    """Parse and validate. Unknown keys are rejected and a stated genome_id must match."""
    reasons = ["unknown key %r" % k for k in d if k not in _TOP_KEYS]
    for sub, cls in (("exits", ExitSpec), ("risk", RiskSpec)):
        known = set(cls.__dataclass_fields__)
        reasons += ["unknown %s key %r" % (sub, k) for k in d.get(sub, {}) if k not in known]
    if reasons:
        raise GenomeError(reasons)
    g = Genome(template=d.get("template", ""), params=tuple(sorted(dict(d.get("params", {})).items())),
               exits=ExitSpec(**d.get("exits", {})), risk=RiskSpec(**d.get("risk", {})),
               timeframe=d.get("timeframe", "1D"), direction=d.get("direction", "long"),
               parent_id=d.get("parent_id", ""), origin=d.get("origin", "manual"),
               schema_version=d.get("schema_version", SCHEMA_VERSION))
    validate(g)
    if "genome_id" in d and d["genome_id"] != g.genome_id:
        raise GenomeError(["stated genome_id %s does not match its content %s" % (d["genome_id"], g.genome_id)])
    return g


def validate(g: Genome, registry: Optional[Mapping[str, TemplateSpec]] = None) -> Genome:
    from research.genome.templates import TEMPLATES
    reg = TEMPLATES if registry is None else registry
    r: List[str] = []
    if g.schema_version != SCHEMA_VERSION:
        r.append("schema_version %r is not supported (expected %d)" % (g.schema_version, SCHEMA_VERSION))
    if g.direction != "long":
        r.append("direction %r refused: research execution is long-only (M17)" % g.direction)
    if g.timeframe not in TIMEFRAMES:
        r.append("timeframe %r not in %s" % (g.timeframe, TIMEFRAMES))
    spec = reg.get(g.template)
    if spec is None:
        r.append("unknown template %r" % g.template)
    else:
        p = dict(g.params)
        if len(p) != len(g.params):
            r.append("duplicate parameter names")
        r += ["unknown parameter %r for %s" % (k, g.template) for k in p if k not in spec.params]
        r += ["missing parameter %r for %s" % (k, g.template) for k in spec.params if k not in p]
        for k, ps in spec.params.items():
            if k in p:
                why = ps.check(k, p[k])
                if why:
                    r.append(why)
        for a, b in spec.constraints:
            if a in p and b in p and not p[a] < p[b]:
                r.append("constraint %s < %s violated (%r >= %r)" % (a, b, p[a], p[b]))
    e = g.exits
    if not (isinstance(e.stop_atr, (int, float)) and np.isfinite(e.stop_atr) and 0.25 <= e.stop_atr <= 10):
        r.append("stop_atr is mandatory and must be in [0.25, 10] ATR, got %r" % (e.stop_atr,))
    if e.target_atr is not None and not (np.isfinite(e.target_atr) and 0.25 <= e.target_atr <= 20):
        r.append("target_atr must be None or in [0.25, 20], got %r" % (e.target_atr,))
    if e.trail_atr is not None and not (np.isfinite(e.trail_atr) and 0.25 <= e.trail_atr <= 10):
        r.append("trail_atr must be None or in [0.25, 10], got %r" % (e.trail_atr,))
    if isinstance(e.time_stop_bars, bool) or not isinstance(e.time_stop_bars, int) or not 1 <= e.time_stop_bars <= 500:
        r.append("time_stop_bars must be an int in [1, 500], got %r" % (e.time_stop_bars,))
    k = g.risk
    if not (0 < k.risk_per_trade <= MAX_RISK_PER_TRADE):
        r.append("risk_per_trade must be in (0, %.2f], got %r" % (MAX_RISK_PER_TRADE, k.risk_per_trade))
    if not (0 < k.max_position_pct <= MAX_POSITION_PCT):
        r.append("max_position_pct must be in (0, %.2f], got %r" % (MAX_POSITION_PCT, k.max_position_pct))
    if r:
        raise GenomeError(r)
    return g


def seed(template: str, **overrides) -> Genome:
    """The template's default genome, with optional parameter overrides."""
    from research.genome.templates import TEMPLATES
    spec = TEMPLATES[template]
    p = {k: v.default for k, v in spec.params.items()}
    p.update(overrides)
    return validate(Genome(template, tuple(sorted(p.items()))))


def with_params(g: Genome, origin: str = "grid", **updates) -> Genome:
    p = g.p
    p.update(updates)
    return validate(replace(g, params=tuple(sorted(p.items())), parent_id=g.genome_id, origin=origin))


def mutate(g: Genome, rng: np.random.Generator, n_changes: int = 1) -> Genome:
    """Move `n_changes` random parameters one grid step (bounded); lineage recorded."""
    from research.genome.templates import TEMPLATES
    spec = TEMPLATES[g.template]
    p = g.p
    names = sorted(spec.params)
    for name in rng.choice(names, size=min(n_changes, len(names)), replace=False):
        ps = spec.params[name]
        if ps.kind == "choice":
            p[name] = ps.choices[int(rng.integers(len(ps.choices)))]
            continue
        step = ps.step or (1 if ps.kind == "int" else (ps.hi - ps.lo) / 10)
        v = p[name] + step * (1 if rng.uniform() < 0.5 else -1)
        v = min(max(v, ps.lo), ps.hi)
        p[name] = int(round(v)) if ps.kind == "int" else float(round(v, 10))
    out = replace(g, params=tuple(sorted(p.items())), parent_id=g.genome_id, origin="mutation")
    try:
        return validate(out)
    except GenomeError:
        return g                                   # constraint violated: keep the parent (fail-closed)


__all__ = ["SCHEMA_VERSION", "TIMEFRAMES", "GenomeError", "ParamSpec", "TemplateSpec", "ExitSpec", "RiskSpec",
           "Genome", "from_dict", "validate", "seed", "with_params", "mutate"]
