"""research.mllab.benchmarks — known-truth scenarios for the model laboratory.

Each scenario is a synthetic market whose planted structure is known, so the
lab can be checked for the RIGHT answer, including "no":

  null         no planted effect. Correct answer: no model improves on the
               prior or the baseline; decisions do not beat standing aside.
  linear       drop reversal whose strength grows with the drop
               (monotone). Correct answer: models beat the prior, but trees
               need not beat logistic regression.
  interaction  after a large drop: quiet volume -> nothing, normal-to-high
               volume -> continuation down, capitulation volume (>= 2x) ->
               reversal up. Non-monotone in volume and gated by drop size, so
               a linear model cannot represent it. Correct answer: trees
               beat logistic regression.
  regime       oversold reversal only in the volatile market regime.
               Correct answer: models with regime context beat the
               regime-blind linear baseline.

The primary rule (the candidate sampler) is "one-bar drop below -1 x
realized vol"; every model acts as a META-LABELER on those candidates
(take / skip), so "take every candidate" is the primary-rule benchmark.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, Optional, Sequence, Tuple

from research.common.synthetic import MarketSpec, PlantedEffect, generate
from research.integrity.controls import BatteryResult
from research.integrity.ledger import Ledger
from research.mllab import models as MD
from research.mllab.datasets import build_dataset, drop_rule, oversold_rule
from research.mllab.evaluate import LabConfig, LabResult, run
from research.mllab.labels import BarrierSpec

DAY_NS = int(86400e9)


@dataclass(frozen=True)
class Scenario:
    name: str
    effects: Tuple[PlantedEffect, ...]
    sampler: str = "drop"            # "drop" (drop below -1 x realized vol) or "oversold" (RSI < 40)
    expected: str = ""
    baseline: str = "logistic"
    models: Tuple[str, ...] = ()
    pairs: Tuple[Tuple[str, str], ...] = ()


SCENARIOS: Dict[str, Scenario] = {
    "null": Scenario("null", (), expected="no improvement anywhere"),
    "linear": Scenario("linear", (PlantedEffect("rev2", "drop_reversal", 0.30, horizon=5, k=2.0),
                                  PlantedEffect("rev3", "drop_reversal", 0.30, horizon=5, k=3.0)),
                       expected="models beat prior; trees ~ logistic"),
    "interaction": Scenario("interaction", (
        PlantedEffect("follow", "loud_drop_continuation", -0.5, horizon=5, k=1.5, vol_ratio=1.0),
        PlantedEffect("capitulation", "loud_drop_continuation", 1.0, horizon=5, k=1.5, vol_ratio=2.0)),
        expected="trees beat logistic (non-monotone in volume)"),
    "regime": Scenario("regime", (PlantedEffect("rsi_vol", "rsi_oversold", 1.2, horizon=5, level=35.0,
                                                regime="volatile"),),
                       sampler="oversold", baseline="logistic_noctx",
                       models=("prior", "logistic_noctx", "logistic", "hgb_noctx", "hgb", "xgb", "regime_hgb"),
                       pairs=(("logistic", "logistic_noctx"), ("hgb", "hgb_noctx")),
                       expected="models WITH market-regime context beat the regime-blind baseline"),
}

DEFAULT_MODELS = ("prior", "logistic", "hgb", "xgb", "lgbm", "rf", "mlp", "mean_ens", "stack", "regime_hgb",
                  "bag_hgb")
EMBARGO_NS = 6 * DAY_NS
BARRIER = BarrierSpec(tp_mult=1.0, sl_mult=1.0, horizon=5)


def scaled(name: str, factor: float, new_name: Optional[str] = None) -> Scenario:
    """The scenario with every planted strength multiplied by `factor` (0 = null)."""
    scn = SCENARIOS[name]
    return replace(scn, name=new_name or "%s_x%g" % (name, factor),
                   effects=tuple(replace(e, strength=e.strength * factor) for e in scn.effects))


def market(scn: Scenario, *, n_symbols: int, n_sessions: int, seed: int):
    return generate(MarketSpec(n_symbols=n_symbols, n_sessions=n_sessions, seed=seed, effects=scn.effects))


def prepare(scn: Scenario, *, n_symbols: int = 40, n_sessions: int = 1500, seed: int = 7,
            models: Optional[Sequence[str]] = None):
    """(market, dataset, factories) for a scenario: no fitting yet."""
    m = market(scn, n_symbols=n_symbols, n_sessions=n_sessions, seed=seed)
    sampler = drop_rule(1.0) if scn.sampler == "drop" else oversold_rule(40.0)
    ds = build_dataset(m.bars, spec=BARRIER, sampler=sampler, truth=m.truth,
                       extra_meta={"scenario": scn.name, "market_digest": m.spec_digest})
    ctx_cols = [i for i, f in enumerate(ds.feature_names) if f.startswith("ctx.")]
    use = tuple(models) if models is not None else (scn.models or DEFAULT_MODELS)
    fac = MD.factories(use, seed=seed, regime_col=ds.feature_names.index("ctx.mkt_vol_pct"),
                       context_cols=ctx_cols, n_features=len(ds.feature_names))
    return m, ds, fac


def lab_config(scn: Scenario, *, seed: int, n_folds: int = 5) -> LabConfig:
    return LabConfig(n_folds=n_folds, embargo_ns=EMBARGO_NS, seed=seed, baseline=scn.baseline)


def run_scenario(name, *, n_symbols: int = 40, n_sessions: int = 1500, seed: int = 7,
                 models: Optional[Sequence[str]] = None, ledger: Optional[Ledger] = None,
                 n_folds: int = 5, canary: Optional[BatteryResult] = None, return_inputs: bool = False):
    """Run a named (or given) scenario. With return_inputs=True returns
    (result, market, dataset) so audits can reuse the exact inputs."""
    scn = name if isinstance(name, Scenario) else SCENARIOS[name]
    m, ds, fac = prepare(scn, n_symbols=n_symbols, n_sessions=n_sessions, seed=seed, models=models)
    res = run(ds, fac, lab_config(scn, seed=seed, n_folds=n_folds), scenario=scn.name, ledger=ledger,
              extra_pairs=scn.pairs, canary=canary)
    res.meta.update({"expected": scn.expected, "n_symbols": n_symbols, "n_sessions": n_sessions,
                     "market_digest": m.spec_digest, "n_features": len(ds.feature_names), "seed": seed})
    return (res, m, ds) if return_inputs else res


__all__ = ["Scenario", "SCENARIOS", "DEFAULT_MODELS", "EMBARGO_NS", "BARRIER", "scaled", "market", "prepare",
           "lab_config", "run_scenario"]
