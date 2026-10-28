"""research.optimiser.canary — the integrated canary battery for the WP13 driver.

The canary runs the EXACT walk-forward decision (``walkforward.run_template``,
same config and code) on synthetic markets with a known truth:

* nulls      no planted effect; a PASS is a false pass;
* positives  a planted causal effect that the template's own rule can capture
             (``EFFECT_FOR``), at pre-declared strengths.

``research.integrity.controls.run_battery`` turns the counts into a verdict.
- CALIBRATED needs the Clopper-Pearson bound on false passes at or below the
  target; at a 5 % target with alpha 0.05 that means at least 59 nulls with
  zero passes.
- It also needs enough power at every required effect.
- A smaller battery is reported honestly as NOT_CALIBRATED (or NULL_ONLY /
  UNDERPOWERED); it is never upgraded.

The full battery is a long offline job (``main --canary``). The unit tests run
a deliberately small battery and assert that it is labelled as such.
Effect strengths, market sizes and seeds are declared in the plan before the
run and are never retuned to obtain a result.

Experiment identity (PR #24 review). ``experiment_identity`` is the canonical
record of everything that can change the decision or its calibration: the
code fingerprint (decision code and the trial code), the complete WFConfig
(folds, embargo, holdout, metric, thresholds, size multipliers, the economics
cost/liquidity model, the engine), the template and its parameter search
space, the data-generation protocol (generator, every MarketSpec parameter,
the planted effect and horizon, universe size and session count), the null
and positive designs, the false-pass and power thresholds and the seed
schedule. It is bound into the plan (and so its digest), recorded with its
own digest, written to the report, and the report card accepts the battery
only for an evaluation that requires exactly this identity.

The v2 battery (plan ``16aaccc1…``, run at 609aa85: 0/59 null passes, 20/20
detections at 0.5σ, 30 symbols × 750 sessions) predates this binding. It
stays as historical evidence for that exact run and is not re-labelled; its
plan carries no experiment identity and its ledger was not exported, so the
report card cannot accept it. ``PROTOCOL_V3`` preregisters the fresh,
identity-bound calibration with the same design and new seeds.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Dict, Optional, Tuple

from research.common.hashing import digest, to_plain
from research.common.synthetic import MarketSpec, PlantedEffect, generate
from research.genome import search as SR
from research.integrity.controls import BatteryPlan, BatteryResult, PipelineDecision, pipeline_identity, run_battery
from research.integrity.ledger import Ledger
from research.integrity.provenance import code_identity
from research.optimiser import walkforward as W

EFFECT_FOR = {"mean_reversion": "rsi_oversold", "breakout_momentum": "breakout", "pullback_in_trend": "pullback"}
CODE_PATHS = ("research/optimiser", "research/genome", "research/integrity", "research/economics",
              "research/common", "bot/backtesting")
IDENTITY_SCHEMA = "m21-wp13-canary-experiment/1"
SEED_SCHEME = "research.common.rng.child_seed(base_seed, plan.name, 'null'|'pos', [repr(effect),] index)"


@dataclass(frozen=True)
class CanaryProtocol:
    """A pre-declared canary design. Never edited after a run: a new design is a new protocol."""
    name: str
    n_nulls: int
    effect_sizes: Tuple[float, ...] = ()
    n_per_effect: int = 0
    n_symbols: int = 8
    n_sessions: int = 600
    horizon: int = 5
    base_seed: int = 5000
    false_pass_target: float = 0.05
    alpha: float = 0.05
    min_power: float = 0.8
    required_effects: Tuple[float, ...] = ()


# The design of the historical v2 battery (plan 16aaccc1…, run at 609aa85). Kept for the record only.
PROTOCOL_V2_HISTORICAL = CanaryProtocol("wp13-canary-v2", 59, (0.5,), 20, n_symbols=30, n_sessions=750)
# Preregistered fresh calibration bound to the experiment identity: same design, new name (new seeds).
PROTOCOL_V3 = CanaryProtocol("wp13-canary-v3", 59, (0.5,), 20, n_symbols=30, n_sessions=750)


def experiment_identity(template: str, cfg: W.WFConfig, protocol: CanaryProtocol) -> Dict[str, Any]:
    """The canonical identity of one canary experiment (see the module notes)."""
    if template not in EFFECT_FOR and protocol.effect_sizes:
        raise ValueError("no planted effect is defined for %r" % template)
    one = replace(cfg, templates=(template,))
    spec = asdict(MarketSpec(n_symbols=protocol.n_symbols, n_sessions=protocol.n_sessions, timeframe="1D"))
    spec.pop("seed")
    spec.pop("effects")
    plan = BatteryPlan(protocol.name, "pipeline:unbound", n_nulls=protocol.n_nulls,
                       false_pass_target=protocol.false_pass_target, alpha=protocol.alpha,
                       effect_sizes=tuple(protocol.effect_sizes), n_per_effect=protocol.n_per_effect,
                       min_power=protocol.min_power, base_seed=protocol.base_seed,
                       required_effects=tuple(protocol.required_effects))      # validates the design
    return to_plain({
        "schema": IDENTITY_SCHEMA,
        "template": template,
        "pipeline": {
            "family_id": "wf:%s" % template,
            "decision": "research.optimiser.walkforward.run_template on the research period "
                        "(timeline cut at int(len * (1 - holdout_frac))); PASS = verdict PASS",
            "code_paths": list(CODE_PATHS), "code_digest": code_identity(CODE_PATHS),
            "trial_code_paths": list(SR.CODE_PATHS), "trial_code_digest": code_identity(SR.CODE_PATHS),
            "wf_config": replace(one, axes=None),
            "search_space": {k: list(v) for k, v in one.axes_for(template).items()},
        },
        "data_protocol": {
            "generator": "research.common.synthetic.generate",
            "market_spec": spec,                                 # every generator parameter except the seed
            "planted_effect": {"name": "canary", "kind": EFFECT_FOR.get(template), "horizon": protocol.horizon},
            "universe": {"n_symbols": protocol.n_symbols, "n_sessions": protocol.n_sessions, "timeframe": "1D"},
        },
        "design": {"name": plan.name, "n_nulls": plan.n_nulls, "effect_sizes": list(plan.effect_sizes),
                   "n_per_effect": plan.n_per_effect, "required_effects": list(plan.required),
                   "false_pass_target": plan.false_pass_target, "alpha": plan.alpha, "min_power": plan.min_power,
                   "base_seed": plan.base_seed, "power_rule": "cp_lower_bound_at_every_required_effect"},
        "seed_schedule": {"scheme": SEED_SCHEME, "base_seed": plan.base_seed, "name": plan.name},
        "holdout_policy": {"holdout_frac": one.holdout_frac, "sealed_holdout_used": False},
        "costs_and_liquidity": one.economics,
    })


def experiment_digest(template: str, cfg: W.WFConfig, protocol: CanaryProtocol) -> str:
    """What an evaluation passes to the report card as ``canary_experiment``."""
    return digest(experiment_identity(template, cfg, protocol))


def market(seed: int, *, n_symbols: int, n_sessions: int, template: Optional[str] = None,
           strength: float = 0.0, horizon: int = 5):
    effects = ()
    if strength:
        effects = (PlantedEffect("canary", EFFECT_FOR[template], strength, horizon=horizon),)
    return seed, generate(MarketSpec(n_symbols=n_symbols, n_sessions=n_sessions, timeframe="1D", seed=seed,
                                     effects=effects)).bars


def wf_decision(template: str, cfg: W.WFConfig) -> Callable[[Tuple[int, Dict]], PipelineDecision]:
    def pipeline(seed_and_bars) -> PipelineDecision:
        seed, bars = seed_and_bars
        led = Ledger()                                       # each canary run is its own family history
        tl = W.timeline_of(bars)
        cut = tl[int(len(tl) * (1.0 - cfg.holdout_frac))]   # the same research period the driver would see
        research = W.SealedHistory(led, "canary", bars, cut).research_bars
        r = W.run_template(template, research, cfg, ledger=led, data_seed=int(seed), sealed=None)
        return PipelineDecision(r["verdict"] == W.PASS, float(r["summary"]["oos_sharpe_ann"]),
                                {"verdict": r["verdict"], "reasons": r["reasons"]})
    return pipeline


def canary_plan(template: str, cfg: W.WFConfig, protocol: CanaryProtocol) -> BatteryPlan:
    """The battery plan, bound to the experiment identity: ``pipeline_id`` names the family and the digest of
    the identity's pipeline section; the whole identity is part of the plan (and of its digest)."""
    exp = experiment_identity(template, cfg, protocol)
    return BatteryPlan(name=protocol.name, pipeline_id=pipeline_identity(exp["pipeline"]["family_id"],
                                                                         digest(exp["pipeline"])),
                       n_nulls=protocol.n_nulls, false_pass_target=protocol.false_pass_target, alpha=protocol.alpha,
                       effect_sizes=tuple(protocol.effect_sizes), n_per_effect=protocol.n_per_effect,
                       min_power=protocol.min_power, base_seed=protocol.base_seed,
                       required_effects=tuple(protocol.required_effects), experiment=exp)


def run_protocol(template: str, cfg: W.WFConfig, protocol: CanaryProtocol, *, ledger: Ledger) -> BatteryResult:
    plan = canary_plan(template, cfg, protocol)
    one = replace(cfg, templates=(template,))
    p = protocol
    return run_battery(plan, wf_decision(template, one),
                       lambda s: market(s, n_symbols=p.n_symbols, n_sessions=p.n_sessions),
                       (lambda eff, s: market(s, n_symbols=p.n_symbols, n_sessions=p.n_sessions, template=template,
                                              strength=eff, horizon=p.horizon)) if p.effect_sizes else None,
                       ledger=ledger)


def run_canary(template: str, cfg: W.WFConfig, *, ledger: Ledger, name: str, n_nulls: int,
               effect_sizes: Tuple[float, ...] = (), n_per_effect: int = 0, n_symbols: int = 8,
               n_sessions: int = 600, base_seed: int = 5000, horizon: int = 5) -> BatteryResult:
    if template not in EFFECT_FOR and effect_sizes:
        raise ValueError("no planted effect is defined for %r" % template)
    return run_protocol(template, cfg, CanaryProtocol(name, n_nulls, tuple(effect_sizes), n_per_effect,
                                                      n_symbols=n_symbols, n_sessions=n_sessions, horizon=horizon,
                                                      base_seed=base_seed), ledger=ledger)


__all__ = ["EFFECT_FOR", "CODE_PATHS", "IDENTITY_SCHEMA", "CanaryProtocol", "PROTOCOL_V2_HISTORICAL", "PROTOCOL_V3",
           "experiment_identity", "experiment_digest", "canary_plan", "market", "wf_decision", "run_canary",
           "run_protocol"]


def main(argv=None) -> int:
    """Offline canary battery for one template (long job; writes a JSON report to --out)."""
    import argparse
    import json
    from dataclasses import asdict
    from pathlib import Path
    ap = argparse.ArgumentParser(description="WP13 integrated canary battery (synthetic, offline)")
    ap.add_argument("--template", default="mean_reversion", choices=sorted(EFFECT_FOR))
    ap.add_argument("--nulls", type=int, default=59)
    ap.add_argument("--positives", type=int, default=20)
    ap.add_argument("--effect", type=float, default=0.5)
    ap.add_argument("--symbols", type=int, default=30)
    ap.add_argument("--sessions", type=int, default=750)
    ap.add_argument("--name", default=None, help="default: the preregistered PROTOCOL_V3 (all its fields)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.name is None:
        protocol = PROTOCOL_V3
    else:
        protocol = CanaryProtocol(a.name, a.nulls, (a.effect,) if a.positives else (), a.positives,
                                  n_symbols=a.symbols, n_sessions=a.sessions)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    led = Ledger(str(out / ("canary_%s.ledger.db" % a.template)))        # the ledger is part of the evidence
    cfg = W.WFConfig()
    plan = canary_plan(a.template, cfg, protocol)
    res = run_protocol(a.template, cfg, protocol, ledger=led)
    print(res.verdict, "null_passes=%d/%d" % (res.null_passes, res.n_nulls),
          " ".join("power@%g=%d/%d" % (p.effect, p.detections, p.n) for p in res.power),
          "experiment=%s" % plan.experiment_digest[:12], flush=True)
    led.export_jsonl(out / ("canary_%s.ledger.jsonl" % a.template))
    target = out / ("canary_%s.json" % a.template)
    target.write_text(json.dumps({"origin": "synthetic", "battery": asdict(res), "plan": plan.record(),
                                  "experiment_digest": plan.experiment_digest, "protocol": asdict(protocol),
                                  "args": vars(a), "ledger_tip": led.tip_hash, "ledger_ok": led.verify().ok},
                                 indent=2, default=str))
    return 0

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
