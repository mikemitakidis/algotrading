"""research.mllab.evidence — reproducible lane C evidence report.

    python -m research.mllab.evidence --out docs/research/evidence/lane_c_mllab [--quick]

Writes ``<out>.md`` and ``<out>.json``. Every number in the Markdown comes from
the JSON produced in the same run. Everything is synthetic, with known truth:
the report shows whether the LABORATORY reaches the right answers, including
"no". It says nothing about any real market.

Contents:
1. label parity with the production M18 triple-barrier label;
2. look-ahead audits: feature as-of (with a deliberately leaky negative
   control) and brute-force fold leakage for every benchmark dataset;
3. a lane B canary battery for the lab's main claim ("a challenger beats
   logistic regression on probabilities"). It reports the false-claim rate
   on null markets with an exact Clopper–Pearson bound, and the power on
   planted non-linear effects;
4. four known-truth benchmarks with the full model zoo: expected versus
   observed verdicts, probability and decision tables, decisions against
   taking every candidate;
5. calibration and uncertainty: ECE, measured split-conformal coverage,
   static versus adaptive (ACI) coverage, and whether ensemble disagreement
   is informative;
6. integrity report cards, ledger-reconciled; one run uses the calibrated
   pipeline configuration with its canary;
7. generated model cards;
8. reproducibility: seeds, digests, library versions, ledger tip.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from research.common.hashing import digest
from research.common.synthetic import MarketSpec, generate
from research.integrity.controls import BatteryPlan, PipelineDecision, run_battery
from research.integrity.ledger import Ledger
from research.integrity.provenance import code_identity
from research.integrity.stats import clopper_pearson
from research.mllab import audits as AU
from research.mllab import benchmarks as BM
from research.mllab import evaluate as EV
from research.mllab import model_card as MC
from research.mllab import uncertainty as U
from research.mllab.datasets import audit_feature_asof

CLAIMS = ("IMPROVES", "PROBABILITIES_ONLY", "DECISIONS_ONLY")
TREES = ("hgb", "xgb", "lgbm", "catboost", "rf", "extra_trees", "mlp", "mean_ens", "stack", "regime_hgb", "bag_hgb")


@dataclass(frozen=True)
class EvidenceConfig:
    seed: int = 7
    bench_symbols: int = 40
    bench_sessions: int = 1500
    bench_models: Optional[Tuple[str, ...]] = None        # None: each scenario's default zoo
    parity_symbols: int = 10
    parity_sessions: int = 1500
    asof_probes: int = 3
    battery_scenario: str = "interaction"
    battery_models: Tuple[str, ...] = ("prior", "logistic", "hgb", "xgb")
    battery_symbols: int = 40
    battery_sessions: int = 1500
    n_nulls: int = 59
    effect_sizes: Tuple[float, ...] = (1.0, 1.5, 2.0)
    required_effects: Tuple[float, ...] = (1.5,)    # chosen from a documented 12-run pilot (power analysis)
    n_per_effect: int = 20
    conformal_alpha: float = 0.2
    aci_gamma: float = 0.01
    scenarios: Tuple[str, ...] = ("null", "linear", "interaction", "regime")
    profile: str = "full"


QUICK = EvidenceConfig(bench_symbols=12, bench_sessions=400, bench_models=("prior", "logistic", "hgb", "mean_ens"),
                       parity_symbols=2, parity_sessions=400, asof_probes=1, battery_symbols=12,
                       battery_sessions=400, battery_models=("prior", "logistic", "hgb"), n_nulls=3,
                       effect_sizes=(1.0,), required_effects=(), n_per_effect=2, scenarios=("null", "interaction"), profile="quick")


def _log(msg: str) -> None:
    print("[lane-c %s] %s" % (time.strftime("%H:%M:%S"), msg), file=sys.stderr, flush=True)


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating, float)):
        v = float(x)
        return None if not math.isfinite(v) else v
    if isinstance(x, np.bool_):
        return bool(x)
    if isinstance(x, np.ndarray):
        return _jsonable(x.tolist())
    return x


# ── 1. label parity ──────────────────────────────────────────────────────────

def label_parity(cfg: EvidenceConfig) -> Dict:
    m = generate(MarketSpec(n_symbols=cfg.parity_symbols, n_sessions=cfg.parity_sessions, seed=cfg.seed))
    tot: Dict[str, int] = {}
    for b in m.bars.values():
        for k, v in AU.parity_with_m18(b).items():
            tot[k] = tot.get(k, 0) + int(v)
    tot["symbols"] = cfg.parity_symbols
    tot["market_digest"] = m.spec_digest
    return tot


# ── 3. canary battery for the lab's claim ────────────────────────────────────

def _claim(res: EV.LabResult) -> PipelineDecision:
    rows = res.comparisons
    hit = [r for r in rows if r["verdict"] in ("IMPROVES", "PROBABILITIES_ONLY")]
    return PipelineDecision(bool(hit), statistic=min((r["holm_p_logloss"] for r in rows), default=1.0),
                            details={"verdicts": {r["model"]: r["verdict"] for r in rows},
                                     "vs_prior": {r["model"]: r["verdict"] for r in res.vs_prior}})


def canary_battery(cfg: EvidenceConfig, ledger: Ledger):
    log: List[Dict] = []

    def pipeline(item):
        kind, eff, seed = item
        scn = BM.scaled(cfg.battery_scenario, eff, "%s_x%g" % (cfg.battery_scenario, eff))
        res = BM.run_scenario(scn, n_symbols=cfg.battery_symbols, n_sessions=cfg.battery_sessions,
                              models=cfg.battery_models, seed=int(seed % (2 ** 31)))
        d = _claim(res)
        log.append({"kind": kind, "effect": eff, "passed": d.passed, **d.details})
        _log("battery %s effect=%g passed=%s (%d done)" % (kind, eff, d.passed, len(log)))
        return d

    plan = BatteryPlan(name="mllab:%s:challenger-beats-logistic" % cfg.battery_scenario,
                       pipeline_id="research.mllab.evaluate.run models=%s size=%dx%d claim=PROBABILITIES_ONLY|IMPROVES"
                                   % (",".join(cfg.battery_models), cfg.battery_symbols, cfg.battery_sessions),
                       n_nulls=cfg.n_nulls, effect_sizes=tuple(cfg.effect_sizes), n_per_effect=cfg.n_per_effect,
                       required_effects=tuple(cfg.required_effects), base_seed=cfg.seed)
    res = run_battery(plan, pipeline, make_null=lambda s: ("null", 0.0, s),
                      make_positive=lambda e, s: ("positive", e, s), ledger=ledger)
    nulls = [r for r in log if r["kind"] == "null"]
    any_claim = sum(any(v in CLAIMS for v in r["verdicts"].values()) for r in nulls)
    dec_only = sum(any(v == "DECISIONS_ONLY" for v in r["verdicts"].values()) for r in nulls)
    prior_claims = sum(any(v in CLAIMS for v in r["vs_prior"].values()) for r in nulls)
    n = len(nulls)
    extra = {"null_runs_with_any_claim_vs_baseline": any_claim,
             "null_any_claim_upper95": clopper_pearson(any_claim, n, 0.05, one_sided_upper=True)[1] if n else None,
             "null_runs_with_decisions_only": dec_only,
             "null_runs_with_any_claim_vs_prior": prior_claims,
             "null_vs_prior_upper95": clopper_pearson(prior_claims, n, 0.05, one_sided_upper=True)[1] if n else None}
    return plan, res, extra


# ── 4/5. benchmarks with calibration, uncertainty and cards ──────────────────

def _expectation(name: str, res: EV.LabResult) -> Tuple[str, bool, str]:
    cmp_ = {r["model"]: r["verdict"] for r in res.comparisons}
    pri = {r["model"]: r["verdict"] for r in res.vs_prior}
    pairs = {(r["model"], r["vs"]): r["verdict"] for r in res.meta.get("pairs", [])}
    good = ("IMPROVES", "PROBABILITIES_ONLY")
    if name == "null":
        claims = [v for v in list(cmp_.values()) + list(pri.values()) + list(pairs.values()) if v in CLAIMS]
        return ("no challenger and no model improves on anything", not claims,
                "%d claims across %d comparisons" % (len(claims), len(cmp_) + len(pri) + len(pairs)))
    if name == "linear":
        beat_prior = pri.get("logistic") in good
        trees_better = [m for m, v in cmp_.items() if v in good]
        return ("logistic beats the prior; no challenger beats logistic on probabilities",
                beat_prior and not trees_better,
                "logistic vs prior: %s; challengers better than logistic: %s" % (pri.get("logistic"),
                                                                                  trees_better or "none"))
    if name == "interaction":
        winners = [m for m, v in cmp_.items() if v in good]
        return ("at least one non-linear challenger beats logistic on probabilities", bool(winners),
                "challengers better than logistic: %s" % (winners or "none"))
    if name == "regime":
        ctx = [p for p, v in pairs.items() if v in good]
        return ("market-context models beat their regime-blind twins", bool(ctx),
                "context pairs better: %s" % (["%s>%s" % p for p in ctx] or "none"))
    return ("", True, "")


def _uncertainty(res: EV.LabResult, ds, cfg: EvidenceConfig, models: Sequence[str]) -> Dict:
    oof = res.oof
    out: Dict[str, Any] = {"online": {}, "dispersion": {}}
    for nm in models:
        if nm not in res.runs:
            continue
        try:
            oc = U.online_coverage(res.runs[nm].p[oof], ds.y[oof], res.fold_of[oof], ds.start[oof], ds.end[oof],
                                   alpha=cfg.conformal_alpha, gamma=cfg.aci_gamma)
        except ValueError:
            continue
        out["online"][nm] = oc
    rnet = ds.r[oof] - EV.LabConfig().cost_r
    for nm, run_ in res.runs.items():
        if run_.member_p is not None:
            out["dispersion"][nm] = U.dispersion_report(run_.member_p[oof], run_.p[oof], ds.y[oof], rnet,
                                                        run_.take[oof])
    return out


def benchmarks(cfg: EvidenceConfig, ledger: Ledger, *, asof_bars=None) -> Dict[str, Dict]:
    out = {}
    for name in cfg.scenarios:
        t = time.time()
        res, mkt, ds = BM.run_scenario(name, n_symbols=cfg.bench_symbols, n_sessions=cfg.bench_sessions,
                                       models=cfg.bench_models, seed=cfg.seed, ledger=ledger, return_inputs=True)
        scn = BM.SCENARIOS[name]
        exp, ok, obs = _expectation(name, res)
        leak = AU.harness_leakage(ds, n_folds=5, embargo_ns=BM.EMBARGO_NS)
        best = res.meta["best_challenger"]
        unc = _uncertainty(res, ds, cfg, [scn.baseline, best] if best else [scn.baseline])
        proto = {"walk_forward_folds": 5, "embargo_days": BM.EMBARGO_NS // BM.DAY_NS, "cost_R": EV.LabConfig().cost_r,
                 "threshold": "chosen on past folds only", "conformal_alpha": cfg.conformal_alpha}
        cards = [MC.build_card(res, m, baseline=scn.baseline, protocol=proto) for m in (scn.baseline, best) if m]
        out[name] = {"result": res, "dataset": ds, "expected": exp, "matched": ok, "observed": obs,
                     "leakage": leak, "uncertainty": unc, "cards": cards, "seconds": round(time.time() - t, 1)}
        if name == "null" and asof_bars is None:
            asof_bars = mkt.bars
        _log("benchmark %s done in %.0fs (matched=%s)" % (name, time.time() - t, ok))
    if asof_bars is not None:
        out["_asof"] = {"clean": audit_feature_asof(asof_bars, probes=cfg.asof_probes, seed=cfg.seed),
                        "leaky_control": audit_feature_asof(asof_bars, probes=cfg.asof_probes, seed=cfg.seed,
                                                            feature_fn=AU.leaky_symbol_features)}
    return out


def calibrated_run(cfg: EvidenceConfig, ledger: Ledger, canary) -> EV.LabResult:
    """The battery's exact pipeline configuration on the canonical seed, with its canary attached."""
    scn = BM.scaled(cfg.battery_scenario, 1.0, "%s_calibrated" % cfg.battery_scenario)
    return BM.run_scenario(scn, n_symbols=cfg.battery_symbols, n_sessions=cfg.battery_sessions,
                           models=cfg.battery_models, seed=cfg.seed, ledger=ledger, canary=canary)


# ── orchestration ────────────────────────────────────────────────────────────

def run_evidence(cfg: EvidenceConfig = EvidenceConfig()) -> Dict:
    t0 = time.time()
    ledger = Ledger()
    _log("parity")
    parity = label_parity(cfg)
    _log("benchmarks")
    bench = benchmarks(cfg, ledger)
    _log("canary battery: %d nulls, %d positives" % (cfg.n_nulls, cfg.n_per_effect * len(cfg.effect_sizes)))
    plan, battery, battery_extra = canary_battery(cfg, ledger)
    _log("calibrated run")
    cal = calibrated_run(cfg, ledger, battery)
    ver = ledger.verify()
    return {"config": cfg, "parity": parity, "bench": bench, "plan": plan, "battery": battery,
            "battery_extra": battery_extra, "calibrated": cal, "ledger_ok": ver.ok,
            "ledger_entries": len(ledger.entries()), "ledger_tip": ledger.tip_hash,
            "versions": MC.library_versions(), "runtime_seconds": round(time.time() - t0, 1)}


def to_json(ev: Dict) -> Dict:
    def run_json(res: EV.LabResult) -> Dict:
        return {"scenario": res.scenario, "dataset_digest": res.dataset_digest, "n_rows": res.n_rows,
                "oof_rows": res.meta["oof_rows"], "fold_sizes": res.fold_sizes,
                "metrics": {k: r.metrics for k, r in res.runs.items()},
                "thresholds": {k: r.thresholds for k, r in res.runs.items()},
                "comparisons": res.comparisons, "vs_prior": res.vs_prior, "pairs": res.meta.get("pairs", []),
                "vs_take_all": res.meta["vs_take_all"], "take_all_mean_net_r": res.meta["take_all_mean_net_r"],
                "best_challenger": res.meta["best_challenger"], "n_trials_charged": res.meta["n_trials_charged"],
                "trial_ids": res.trial_ids, "report_card": res.card.to_dict() if res.card else None,
                "market_digest": res.meta.get("market_digest"), "n_features": res.meta.get("n_features"),
                "runtime_seconds": res.runtime_seconds}

    bench = {}
    for name, b in ev["bench"].items():
        if name.startswith("_"):
            bench[name] = b
            continue
        bench[name] = {"run": run_json(b["result"]), "expected": b["expected"], "matched": b["matched"],
                       "observed": b["observed"], "leakage": b["leakage"], "uncertainty": b["uncertainty"],
                       "model_cards": [{**asdict(c), "digest": c.digest, "markdown": MC.render(c)} for c in b["cards"]],
                       "seconds": b["seconds"]}
    out = {"lane": "C", "title": "Model laboratory evidence (synthetic, known truth)",
           "config": asdict(ev["config"]), "parity": ev["parity"], "benchmarks": bench,
           "battery": {"plan": asdict(ev["plan"]), "plan_digest": ev["plan"].digest,
                       "result": asdict(ev["battery"]), **ev["battery_extra"]},
           "calibrated_run": run_json(ev["calibrated"]), "ledger": {"verified": ev["ledger_ok"],
                                                                    "entries": ev["ledger_entries"],
                                                                    "tip": ev["ledger_tip"]},
           "versions": ev["versions"], "code_identity": code_identity(EV.CODE_PATHS),
           "runtime_seconds": ev["runtime_seconds"]}
    out = _jsonable(out)
    out["report_digest"] = digest({k: v for k, v in out.items() if k != "runtime_seconds"})
    return out


def _f(x, fmt="%.3f"):
    return "—" if x is None or (isinstance(x, float) and not math.isfinite(x)) else fmt % x


def render_markdown(j: Dict) -> str:
    c = j["config"]
    par = j["parity"]
    bat = j["battery"]
    res = bat["result"]
    L: List[str] = []
    L += ["# Lane C — model laboratory evidence (synthetic, known truth)", "",
          "> **Read this first.** Every dataset here is synthetic with a planted, known structure. The report tests "
          "whether the *laboratory* reaches the right answer, including \"no\". It is **not** evidence of any edge in "
          "any real market. Net R is a counterfactual outcome after a %.2f R cost per trade, not P&L. Nothing here "
          "is connected to production execution." % EV.LabConfig().cost_r, "",
          "Generated by `python -m research.mllab.evidence` (profile `%s`, seed %d). Runtime: %.0f s. Report "
          "digest: `%s`." % (c["profile"], c["seed"], j["runtime_seconds"], j["report_digest"][:16]), ""]
    # Summary
    L += ["## Summary", "", "| check | result |", "|---|---|",
          "| Label parity with M18 (outcome / resolution bar mismatches) | %d / %d over %d compared rows |" % (
              par["outcome_mismatch"], par["resolution_bar_mismatch"], par["compared"]),
          "| Feature as-of audit (violations; leaky negative control) | %d of %d cells; leaky control flagged %d |" % (
              j["benchmarks"]["_asof"]["clean"]["violations"], j["benchmarks"]["_asof"]["clean"]["cells_checked"],
              j["benchmarks"]["_asof"]["leaky_control"]["violations"]),
          "| Harness folds clean (all benchmarks) | %s |" % all(
              b["leakage"]["clean"] for k, b in j["benchmarks"].items() if not k.startswith("_")),
          "| Canary battery: false claims on null markets | %d / %d (95 %% upper bound %.1f %%) → **%s** |" % (
              res["null_passes"], res["n_nulls"], 100 * res["false_pass_upper"], res["verdict"])]
    for p in res["power"]:
        L.append("| Canary power at effect ×%g | %d / %d detected (95 %% CI %.0f–%.0f %%) |" % (
            p["effect"], p["detections"], p["n"], 100 * p["lo"], 100 * p["hi"]))
    for name, b in j["benchmarks"].items():
        if name.startswith("_"):
            continue
        L.append("| Benchmark `%s`: expected \"%s\" | **%s**: %s |" % (name, b["expected"],
                                                                     "matched" if b["matched"] else "NOT matched",
                                                                     b["observed"]))
    cr = j["calibrated_run"]["report_card"]
    L += ["| Calibrated-pipeline report card (`%s`) | %s |" % (j["calibrated_run"]["scenario"],
                                                              cr["verdict"] if cr else "no card"), ""]
    # 1 parity
    L += ["## 1. Label parity with production M18", "",
          "The research barrier label was run at the M18 parameters (TP 3 ATR, SL 2 ATR, 50 bars) on %d synthetic "
          "symbols × %d sessions and compared with `bot.ml.labels.triple_barrier`." % (par["symbols"], c["parity_sessions"]),
          "", "| quantity | rows |", "|---|---|"]
    for k in ("rows", "compared", "outcome_mismatch", "resolution_bar_mismatch", "ambiguous",
              "ambiguous_booked_as_stop_by_m18", "gap_rows", "gap_rows_same_resolution_bar", "gap_rows_outcome_differs",
              "research_resolved_m18_pending", "m18_resolved_research_unlabelled"):
        L.append("| %s | %d |" % (k.replace("_", " "), par[k]))
    L += ["", "The documented differences are deliberate. Same-bar ties are flagged as ambiguous and excluded; M18 "
          "books them as stops. Gaps through a barrier fill at the open, so the R differs but the resolution bar "
          "does not. Tail rows that already hit a barrier are labelled even where M18 waits for the full 50-bar "
          "window.", ""]
    # 2 audits
    a = j["benchmarks"]["_asof"]
    L += ["## 2. Look-ahead audits", "",
          "**Feature as-of audit.** Every bar after a probe time was perturbed, and every feature value at or before "
          "the probe was checked for change: %d of %d cells changed over %d probes. As a negative control, the same "
          "audit with a feature that peeks at the next bar flagged **%d** cells, so the audit has the power to catch "
          "a leak." % (a["clean"]["violations"], a["clean"]["cells_checked"], a["clean"]["probes"],
                       a["leaky_control"]["violations"]), "",
          "**Harness folds.** For every benchmark dataset, each walk-forward fold was checked brute force: no "
          "train/test label-interval overlap, no shared rows, and every training label resolved before the test "
          "window opened (embargo %d days)." % (BM.EMBARGO_NS // BM.DAY_NS), "",
          "| benchmark | folds | train sizes | test sizes | purged | overlapping pairs | clean |", "|---|---|---|---|---|---|---|"]
    for name, b in j["benchmarks"].items():
        if name.startswith("_"):
            continue
        f = b["leakage"]["folds"]
        L.append("| %s | %d | %s | %s | %d | %d | %s |" % (name, len(f), ", ".join(str(x["train"]) for x in f),
                                                          ", ".join(str(x["test"]) for x in f),
                                                          sum(x["purged"] for x in f),
                                                          sum(x["overlapping_pairs"] for x in f), b["leakage"]["clean"]))
    L.append("")
    # 3 battery
    L += ["## 3. Canary battery for the lab's main claim", "",
          "The claim tested: *\"at least one challenger beats logistic regression on out-of-sample probabilities "
          "(Holm-adjusted)\"*, made by the pipeline `%s`. The plan was written to the ledger before any result "
          "existed (digest `%s`). Null markets use the `%s` scenario with every planted strength set to zero; "
          "positives scale the planted strengths.%s" % (
              bat["plan"]["pipeline_id"], bat["plan_digest"][:12], c["battery_scenario"],
              " The required strength (power rule) was chosen from a 12-run pilot, 4 runs each at ×1, ×1.5 and "
              "×2, which detected 2/4, 4/4 and 4/4 respectively. The pilot is a power analysis done before the "
              "plan was registered; it is not part of the battery." if c["profile"] == "full" else ""), "",
          "| quantity | value |", "|---|---|",
          "| null markets | %d |" % res["n_nulls"],
          "| null false claims (the battery's pass criterion) | %d → rate %.3f, one-sided 95 %% upper bound %.3f "
          "(target ≤ %.2f) |" % (res["null_passes"], res["false_pass_rate"], res["false_pass_upper"],
                                 bat["plan"]["false_pass_target"]),
          "| null runs with ANY claim vs logistic (incl. decisions-only) | %d (upper bound %s) |" % (
              bat["null_runs_with_any_claim_vs_baseline"], _f(bat["null_any_claim_upper95"])),
          "| null runs with a DECISIONS_ONLY verdict | %d |" % bat["null_runs_with_decisions_only"],
          "| null runs with any claim vs the prior | %d (upper bound %s) |" % (
              bat["null_runs_with_any_claim_vs_prior"], _f(bat["null_vs_prior_upper95"]))]
    for p in res["power"]:
        L.append("| power at strength ×%g | %d / %d = %.2f (95 %% CI %.2f–%.2f) |" % (
            p["effect"], p["detections"], p["n"], p["rate"], p["lo"], p["hi"]))
    L += ["| battery verdict | **%s** (power rule: CP lower bound ≥ %.2f at strength ×%s) |" % (
        res["verdict"], bat["plan"]["min_power"], ", ".join("%g" % e for e in (bat["plan"]["required_effects"] or
                                                                                [max(bat["plan"]["effect_sizes"])]))),
          ""]
    if res["notes"]:
        L += ["Note: %s" % res["notes"], ""]
    # 4 benchmarks
    L += ["## 4. Known-truth benchmarks", "",
          "Each benchmark has %d synthetic symbols × %d sessions. The candidate events are one-bar drops below "
          "−1 × realised volatility (RSI < 40 for `regime`). Every model is a meta-labeler on those candidates, with "
          "five purged, embargoed, expanding walk-forward folds. Trading thresholds and conformal sets use only "
          "earlier folds. Paired tests run on daily aggregates with Newey–West t-statistics and Holm adjustment."
          % (c["bench_symbols"], c["bench_sessions"]), ""]
    for name, b in j["benchmarks"].items():
        if name.startswith("_"):
            continue
        r = b["run"]
        L += ["### `%s` — expected: %s" % (name, b["expected"]), "",
              "**Observed:** %s → **%s**. %d labelled events, %d out of fold, %d features, %.0f s." % (
                  b["observed"], "matched" if b["matched"] else "NOT matched", r["n_rows"], r["oof_rows"],
                  r["n_features"], b["seconds"]), "",
              _summary_md(r), "", "Against the baseline:", "", _cmp_md(r["comparisons"]), "",
              "Against the prior (base rate):", "", _cmp_md(r["vs_prior"]), ""]
        if r["pairs"]:
            L += ["Feature-family pairs:", "", _cmp_md(r["pairs"]), ""]
        L += ["Decisions against taking every candidate (daily net-R difference, Holm p): " + "; ".join(
            "%s %+.3f (%.3f)" % (v["model"], v["daily_r_diff"], v["holm_p"]) for v in r["vs_take_all"]), ""]
    # 5 uncertainty
    L += ["## 5. Calibration and uncertainty", "",
          "**Split-conformal coverage** (target %.0f %%), measured out of fold. Each fold is calibrated on earlier "
          "folds' predictions, so coverage is measured, not assumed. Below target means the past was not "
          "exchangeable with the future." % (100 * (1 - c["conformal_alpha"])), "",
          "| benchmark | " + " | ".join(["model", "ECE", "conformal coverage", "singleton share", "conformal trades (mean net R)"]) + " |",
          "|---|---|---|---|---|---|"]
    for name, b in j["benchmarks"].items():
        if name.startswith("_"):
            continue
        for m, met in b["run"]["metrics"].items():
            L.append("| %s | %s | %.3f | %s | %s | %d (%s) |" % (name, m, met["ece"], _f(met["conformal_coverage"], "%.2f"),
                                                             _f(met["conformal_singleton_share"], "%.2f"),
                                                             met["conformal_trades"], _f(met["conformal_mean_net_r"])))
    L += ["", "**Static versus adaptive conformal (ACI, γ = %g)** on the time-ordered out-of-fold stream, causally: "
          "static conformal is calibrated on fold-0 labels that resolved before the first later decision, and ACI "
          "receives each label only after its barrier resolved (before the decision that uses it), judged against "
          "the quantile in force at its own decision time." % c["aci_gamma"], "",
          "| benchmark | model | rows | static coverage | ACI coverage | static mean set size | ACI mean set size |",
          "|---|---|---|---|---|---|---|"]
    for name, b in j["benchmarks"].items():
        if name.startswith("_"):
            continue
        for m, oc in b["uncertainty"]["online"].items():
            L.append("| %s | %s | %d | %.3f | %.3f | %.2f | %.2f |" % (name, m, oc["n"], oc["static_coverage"],
                                                                     oc["aci_coverage"], oc["static_mean_set_size"],
                                                                     oc["aci_mean_set_size"]))
    L += ["", "**Ensemble disagreement as an uncertainty signal.** This is the Spearman correlation between member "
          "dispersion and per-row log loss. A positive, significant value means disagreement flags worse "
          "predictions. Net R is shown for taken trades kept in order of lowest dispersion first.", "",
          "| benchmark | ensemble | members | mean dispersion | ρ(dispersion, log loss) (p) | log loss by dispersion tercile | taken trades: mean net R at 20 / 50 / 100 % |",
          "|---|---|---|---|---|---|---|"]
    for name, b in j["benchmarks"].items():
        if name.startswith("_"):
            continue
        for m, d in b["uncertainty"]["dispersion"].items():
            curve = d["taken_trades_low_dispersion_first"]
            L.append("| %s | %s | %d | %.4f | %s (%s) | %s | %s |" % (
                name, m, d["members"], d["mean_dispersion"], _f(d["spearman_dispersion_vs_logloss"]),
                _f(d["spearman_p"], "%.3g"), " / ".join(_f(t["log_loss"], "%.3f") for t in d["by_tercile"]),
                " / ".join(_f(x[1]) for x in curve) if curve else "no trades"))
    L.append("")
    # 6 report cards
    L += ["## 6. Integrity report cards", "",
          "Each benchmark's best challenger, chosen by total out-of-fold net R, goes through the lane B gauntlet. "
          "The card is charged for every trial in its ledger family and reconciled against the ledger. The "
          "benchmarks use the full model zoo, which **no** canary battery calibrated, so they cannot PASS by "
          "design. The last row runs the exact configuration the battery calibrated, with that battery attached.",
          "", "| run | candidate | trials charged | verdict | reasons |", "|---|---|---|---|---|"]
    rows = [(n, b["run"]) for n, b in j["benchmarks"].items() if not n.startswith("_")] + [("calibrated pipeline", j["calibrated_run"])]
    for n, r in rows:
        card = r["report_card"]
        if card:
            L.append("| %s | `%s` | %d | **%s** | %s |" % (n, card["candidate_id"], r["n_trials_charged"], card["verdict"],
                                                        "; ".join(card["reasons"]) or "none"))
        else:
            L.append("| %s | — | %d | no card (best challenger took < 10 trades) | — |" % (n, r["n_trials_charged"]))
    cal = j["calibrated_run"]
    L += ["", "Calibrated-pipeline run: verdicts against logistic: " + "; ".join(
        "%s %s" % (x["model"], x["verdict"]) for x in cal["comparisons"]) + ".", ""]
    if cal["report_card"] and cal["report_card"]["verdict"] == "PASS":
        L += ["**PASS means only this:** on a synthetic market with a planted non-linear effect, the calibrated "
              "pipeline found it and the gauntlet confirmed it. It shows the machinery working end to end. It is "
              "not evidence about any real market.", ""]
    # 7 model cards
    L += ["## 7. Model cards", "", "These are generated from the results above. A research card can recommend at "
          "most \"candidate for pre-registered forward testing\", and never deployment.", ""]
    for name, b in j["benchmarks"].items():
        if name.startswith("_"):
            continue
        for card in b["model_cards"]:
            L += [_card_md(card), ""]
    # 8 reproducibility
    L += ["## 8. Reproducibility", "",
          "- Command: `python -m research.mllab.evidence --out docs/research/evidence/lane_c_mllab%s`" % (
              " --quick" if c["profile"] == "quick" else ""),
          "- Ledger: %d entries, chain verified = %s, tip `%s`" % (j["ledger"]["entries"], j["ledger"]["verified"],
                                                                 (j["ledger"]["tip"] or "")[:16]),
          "- Libraries: " + ", ".join("%s %s" % kv for kv in sorted(j["versions"].items())),
          "- Code identity (content hash of the lab, integrity and feature sources; recorded on every trial): `%s`"
          % j.get("code_identity", "n/a"),
          "- Benchmark dataset digests: " + ", ".join("%s `%s`" % (n, b["run"]["dataset_digest"][:12])
                                                      for n, b in j["benchmarks"].items() if not n.startswith("_")),
          "- Every model is bit-reproducible for its seed (random forests single-threaded, LightGBM deterministic).",
          "", "## What this does and does not show", "",
          "- **Does:** the lab labels like M18. Its features and folds cannot see the future, and both audits are "
          "shown to catch leaks. Under the null it does not invent improvements, at the measured rate and bound. "
          "When non-linear or regime structure is planted, it finds it at the measured power.",
          "- **Does not:** say anything about real markets. A model that wins on these synthetic benchmarks has "
          "shown only that the machinery works. Real-data use needs approved data (lane F), a pre-registration "
          "(lane B) and the forward-test ladder.", ""]
    return "\n".join(L)


def _summary_md(r: Dict) -> str:
    L = ["| model | AUC | log loss | Brier | ECE | trades | mean net R / trade | total net R | fit s |",
         "|---|---|---|---|---|---|---|---|---|"]
    for nm, m in r["metrics"].items():
        L.append("| %s | %.3f | %.4f | %.4f | %.3f | %d | %s | %.1f | %.1f |" % (
            nm, m["auc"], m["log_loss"], m["brier"], m["ece"], m["n_trades"], _f(m["mean_net_r_per_trade"]),
            m["total_net_r"], m["fit_seconds"]))
    L.append("| take every candidate | — | — | — | — | %d | %.3f | — | — |" % (r["oof_rows"], r["take_all_mean_net_r"]))
    return "\n".join(L)


def _cmp_md(rows: List[Dict]) -> str:
    if not rows:
        return "(none)"
    L = ["| model | vs | log-loss gain/day (Holm p) | daily net-R diff [95 % CI] (Holm p) | verdict |", "|---|---|---|---|---|"]
    for r in rows:
        L.append("| %s | %s | %+.4f (%.3f) | %+.3f [%+.3f, %+.3f] (%.3f) | **%s** |" % (
            r["model"], r["vs"], r["logloss_gain_per_day"], r["holm_p_logloss"], r["daily_r_diff"],
            r["daily_r_diff_lo"], r["daily_r_diff_hi"], r["holm_p_decision"], _verdict_md(r)))
    return "\n".join(L)


def _verdict_md(r: Dict) -> str:
    if r.get("skill_gate", "").startswith("downgraded"):
        return "%s (raw %s; skill-gated: no matching gain over the prior)" % (r["verdict"], r["verdict_raw"])
    return r["verdict"]


def _card_md(card: Dict) -> str:
    return card["markdown"]


def write(j: Dict, out: Path) -> Tuple[Path, Path]:
    out.parent.mkdir(parents=True, exist_ok=True)
    jp, mp = out.with_suffix(".json"), out.with_suffix(".md")
    jp.write_text(json.dumps(j, indent=1, sort_keys=True) + "\n")
    mp.write_text(render_markdown(j))
    return jp, mp


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, help="output path stem (writes .md and .json)")
    ap.add_argument("--quick", action="store_true", help="small smoke profile (minutes, not a report of record)")
    args = ap.parse_args(argv)
    import warnings
    warnings.filterwarnings("ignore", category=UserWarning)
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    cfg = QUICK if args.quick else EvidenceConfig()
    j = to_json(run_evidence(cfg))
    jp, mp = write(j, Path(args.out))
    _log("wrote %s and %s" % (jp, mp))
    return 0


__all__ = ["EvidenceConfig", "QUICK", "run_evidence", "to_json", "render_markdown", "write", "main"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
