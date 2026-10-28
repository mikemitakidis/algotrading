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
"""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Dict, Optional, Tuple

from research.common.synthetic import MarketSpec, PlantedEffect, generate
from research.integrity.controls import BatteryPlan, BatteryResult, PipelineDecision, pipeline_identity, run_battery
from research.integrity.ledger import Ledger
from research.integrity.provenance import code_identity
from research.optimiser import walkforward as W

EFFECT_FOR = {"mean_reversion": "rsi_oversold", "breakout_momentum": "breakout", "pullback_in_trend": "pullback"}
CODE_PATHS = ("research/optimiser", "research/genome", "research/integrity", "research/economics",
              "research/common", "bot/backtesting")


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


def run_canary(template: str, cfg: W.WFConfig, *, ledger: Ledger, name: str, n_nulls: int,
               effect_sizes: Tuple[float, ...] = (), n_per_effect: int = 0, n_symbols: int = 8,
               n_sessions: int = 600, base_seed: int = 5000) -> BatteryResult:
    if template not in EFFECT_FOR and effect_sizes:
        raise ValueError("no planted effect is defined for %r" % template)
    plan = BatteryPlan(name=name, pipeline_id=pipeline_identity("wf:%s" % template, code_identity(CODE_PATHS)),
                       n_nulls=n_nulls, effect_sizes=tuple(effect_sizes), n_per_effect=n_per_effect,
                       base_seed=base_seed)
    one = replace(cfg, templates=(template,))
    return run_battery(plan, wf_decision(template, one),
                       lambda s: market(s, n_symbols=n_symbols, n_sessions=n_sessions),
                       (lambda eff, s: market(s, n_symbols=n_symbols, n_sessions=n_sessions, template=template,
                                              strength=eff)) if effect_sizes else None,
                       ledger=ledger)


__all__ = ["EFFECT_FOR", "CODE_PATHS", "market", "wf_decision", "run_canary"]


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
    ap.add_argument("--name", default="wp13-canary-v1")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    led = Ledger()
    res = run_canary(a.template, W.WFConfig(), ledger=led, name=a.name, n_nulls=a.nulls,
                     effect_sizes=(a.effect,) if a.positives else (), n_per_effect=a.positives,
                     n_symbols=a.symbols, n_sessions=a.sessions)
    print(res.verdict, "null_passes=%d/%d" % (res.null_passes, res.n_nulls),
          " ".join("power@%g=%d/%d" % (p.effect, p.detections, p.n) for p in res.power), flush=True)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    target = out / ("canary_%s.json" % a.template)
    target.write_text(json.dumps({"origin": "synthetic", "battery": asdict(res), "args": vars(a),
                                  "ledger_tip": led.tip_hash, "ledger_ok": led.verify().ok}, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
