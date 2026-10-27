"""research.integration.pipeline — the research lanes connected end to end (offline).

One deterministic run. It touches no broker, network, VPS or production data,
and it writes only inside the caller's work directory.

1. **F (data):** write a synthetic market with planted structure and light
   corruption as an M16-layout parquet store. Snapshot it with a manifest,
   inventory and quality-audit it, and drop symbols the audit cannot
   clear: unusable, or carrying a suspected unadjusted split. Research
   reads the bars back from the store.
2. **E (knowledge):** record sources and a falsifiable, point-in-time
   hypothesis through the role gate.
3. **D (genome):** run a grid search of the mean-reversion template. Every
   point is a ledgered trial whose ``data_digest`` is the snapshot digest.
   Select by plateau, not by peak, and run the selected genome through the
   portfolio engine.
4. **A (counterfactual evidence):** label EVERY candidate the selected
   genome produced, whether executed or skipped by capacity, and compute a
   fail-closed effective N.
5. **C (model laboratory):** meta-label the genome's own candidates
   (prior vs logistic vs boosted trees) with purged walk-forward, ledgered
   trials and skill-gated verdicts.
6. **B (integrity):** produce a ledger-reconciled report card for the
   selected genome. No canary battery exists for this pipeline, so it
   cannot PASS by design.
7. **E (outcome):** record lineage, then the graveyard or the outcome.

``checks`` lists the invariants an integration test asserts.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd

from research.agents.knowledge import KnowledgeLibrary, Prediction, Proposal
from research.agents.mock import FIXTURE_SOURCES
from research.agents.roles import Gate
from research.common.rng import stream
from research.common.synthetic import MarketSpec, PlantedEffect, generate
from research.dataqa import inventory as INV
from research.dataqa import snapshot as SN
from research.evidence.candidates import Bracket, CandidateEvent, make_candidate_id
from research.evidence.effective_n import effective_n
from research.evidence.labeler import SourceInfo, label_candidates
from research.genome import engine as E
from research.genome import schema as G
from research.genome import search as SR
from research.genome.templates import signals as template_signals
from research.integrity.ledger import Ledger
from research.integrity.report_card import GauntletConfig, evaluate as gauntlet
from research.mllab import evaluate as LAB
from research.mllab import models as MD
from research.mllab.datasets import build_dataset
from research.mllab.labels import BarrierSpec

DAY_NS = int(86400e9)
AXES = {"rsi_entry": [25.0, 30.0, 35.0], "bb_std": [1.5, 2.0, 2.5]}


def run(workdir: Path, *, seed: int = 7, n_symbols: int = 16, n_sessions: int = 700) -> Dict:
    t0 = time.time()
    workdir = Path(workdir)
    L = Ledger()
    gate = Gate(L)
    kb = KnowledgeLibrary(L, gate)

    # 1. F — data store, snapshot, inventory, quality
    eff = (PlantedEffect("rsi", "rsi_oversold", 0.5, horizon=5, level=30.0),)
    mk = generate(MarketSpec(n_symbols=n_symbols, n_sessions=n_sessions, seed=seed, effects=eff,
                             missing_bar_prob=0.005, split_prob=0.15))
    store = workdir / "store"
    for s, b in mk.bars.items():
        p = store / "synthetic" / "1D" / ("%s.parquet" % s)
        p.parent.mkdir(parents=True, exist_ok=True)
        b.to_parquet(p, index=False)
    manifest = SN.build(store, name="integration")
    snap_problems = SN.verify(manifest, store)
    inv = INV.inventory_store(store)
    q = inv["quality"]["synthetic/1D"]["per_symbol"]
    usable = sorted(s for s, x in q.items() if x["grade"] != "unusable" and not x["suspected_splits"])
    excluded = sorted(set(q) - set(usable))
    bars = {s: pd.read_parquet(store / "synthetic" / "1D" / ("%s.parquet" % s)) for s in usable}
    data_digest = manifest["snapshot_digest"]
    first_day = str(min(pd.to_datetime(b["ts_utc"], utc=True).min() for b in bars.values()).date())

    # 2. E — knowledge and hypothesis
    sids = [kb.add_source("scout", s) for s in FIXTURE_SOURCES[:2]]
    hid = kb.propose(Proposal(
        statement="Oversold stocks below the lower Bollinger band with low RSI revert upward within days",
        mechanism="liquidity-driven overreaction", prediction=Prediction("report_card_verdict", ">=", 1.0),
        kill_criteria="report card not PASS, or the selected genome sits on no plateau", source_ids=tuple(sids),
        template="mean_reversion", proposed_by="hypothesist", test_window_start=first_day))

    # 3. D — ledgered grid search, plateau selection, portfolio run
    gs, bad = SR.grid("mean_reversion", AXES)
    fam = "integration:genome"
    df = SR.evaluate(gs, bars, ledger=L, family_id=fam, data_digest=data_digest, keep_equity=True)
    sel = SR.select(df, AXES, ledger=L, family_id=fam)
    best = {g.genome_id: g for g in gs}[sel["best_plateau"]["genome_id"]]
    port = E.run_genome(best, bars)

    # 4. A — counterfactual labels for every candidate
    cands = []
    for s, b in bars.items():
        sg = template_signals(best, b)
        for i in np.flatnonzero(sg["entry"].to_numpy()):
            a = float(sg["atr"].iloc[i])
            if not np.isfinite(a) or a <= 0:
                continue
            ts = pd.Timestamp(b["ts_utc"].iloc[i])
            cands.append(CandidateEvent(candidate_id=make_candidate_id(s, ts, "long", best.genome_id), symbol=s,
                                        decision_ts_utc=ts, direction="long", stage="historical_anchor",
                                        source="synthetic", timeframe="1D", strategy_id=best.genome_id,
                                        atr_at_decision=a,
                                        bracket=Bracket(best.exits.stop_atr, best.exits.target_atr or 3.0,
                                                        best.exits.time_stop_bars)))
    lab = label_candidates(cands, bars, timeframe="1D", source=SourceInfo("synthetic", "1D", False))
    defin = lab[lab["status"] == "definitive"]
    neff = effective_n(defin["r_net"].to_numpy(), dates=pd.to_datetime(defin["decision_ts_utc"]).dt.date.astype(str),
                       symbols=defin["symbol"].to_numpy(), entry_idx=defin["entry_idx"].to_numpy(),
                       exit_idx=defin["exit_idx"].to_numpy()) if len(defin) else {"status": "insufficient_evidence"}

    # 5. C — meta-labeling of the genome's own candidates
    def sampler(b, feats):
        m = template_signals(best, b)["entry"].to_numpy().copy()
        m[:60] = False
        return m
    spec = BarrierSpec(tp_mult=best.exits.target_atr or 3.0, sl_mult=best.exits.stop_atr,
                       horizon=best.exits.time_stop_bars)
    ds = build_dataset(bars, spec=spec, sampler=sampler, extra_meta={"snapshot_digest": data_digest,
                                                                      "genome_id": best.genome_id})
    fac = MD.factories(("prior", "logistic", "hgb"), seed=seed)
    labres = LAB.run(ds, fac, LAB.LabConfig(n_folds=4, embargo_ns=6 * DAY_NS, seed=seed), scenario="integration",
                     ledger=L)

    # 6. B — ledger-reconciled report card for the selected genome
    r = np.array([t.r_multiple for t in port.trades if np.isfinite(t.r_multiple)])
    rets = [np.asarray(x, float) for x in df["_returns"]]
    fam_matrix = np.column_stack(rets) if len({len(x) for x in rets}) == 1 else None   # daily returns per trial
    card = gauntlet(candidate_id="integration:%s" % best.genome_id, family_id=fam, gross=r, cost_per_obs=0.0,
                    n_trials=L.family_trial_count(fam), rng=stream(seed, "integration", "card"),
                    family_matrix=fam_matrix, cfg=GauntletConfig(n_boot=200), ledger=L,
                    trial_ids=tuple(df["trial_id"]))

    # 7. E — lineage and outcome
    kb.record_lineage("librarian", hypothesis_id=hid, genome_id=best.genome_id, trial_ids=list(df["trial_id"]))
    if card.verdict != "PASS":
        kb.reject("librarian", hid, reason="report card %s" % card.verdict, evidence=list(df["trial_id"]),
                  retest_trigger={"requires": ["approved_real_data_snapshot", "calibrated_canary"]})

    genome_trials = L.trials(fam)
    checks = {
        "snapshot_verifies": not snap_problems,
        "split_suspects_excluded": all(not q[s]["suspected_splits"] for s in usable),
        "genome_trials_carry_snapshot_digest": all(t.payload["data_digest"] == data_digest for t in genome_trials),
        "lab_dataset_traces_to_snapshot": ds.meta.get("snapshot_digest") == data_digest,
        "every_candidate_labelled": len(lab) == len(cands),
        "card_charged_for_all_genome_trials": card.numbers["n_trials"] == float(len(genome_trials)),
        "card_provenance_verified": card.numbers.get("provenance_verified") == 1.0,
        "card_cannot_pass_without_canary": card.verdict != "PASS",
        "card_computed_pbo_and_spa": fam_matrix is not None and "pbo" in card.numbers,
        "ledger_verifies": L.verify().ok,
    }
    out = {
        "data": {"symbols": n_symbols, "usable": len(usable), "excluded": excluded, "snapshot_digest": data_digest,
                 "files": len(manifest["files"]), "grades": inv["quality"]["synthetic/1D"]["grades"]},
        "hypothesis": {"id": hid, "status": kb.status(hid)},
        "genome": {"grid_points": len(gs), "invalid": bad, "selected": best.genome_id,
                   "selected_params": best.p, "selection": {k: v for k, v in sel.items() if k != "dsr_best_isolated"},
                   "dsr_best_isolated": sel["dsr_best_isolated"], "portfolio": E.metrics(port),
                   "baselines": SR.baselines(bars)},
        "counterfactual": {"candidates": len(cands), "executed_trades": len(port.trades),
                           "definitive": int(len(defin)), "status_counts": lab["status"].value_counts().to_dict(),
                           "mean_r_net_definitive": float(defin["r_net"].mean()) if len(defin) else float("nan"),
                           "effective_n": {k: neff.get(k) for k in ("n_raw", "n_effective", "status")}},
        "lab": {"rows": len(ds), "verdicts": {x["model"]: x["verdict"] for x in labres.comparisons},
                "vs_prior": {x["model"]: x["verdict"] for x in labres.vs_prior},
                "auc": {k: v.metrics["auc"] for k, v in labres.runs.items()},
                "card": labres.card.verdict if labres.card else None},
        "report_card": {"verdict": card.verdict, "reasons": list(card.reasons)},
        "knowledge": kb.summary(),
        "ledger": {"entries": len(L.entries()), "tip": L.tip_hash},
        "checks": checks,
        "runtime_seconds": round(time.time() - t0, 1),
    }
    L.close()
    return out


def render(o: Dict) -> str:
    g, cf, lab, d = o["genome"], o["counterfactual"], o["lab"], o["data"]
    L = ["# Research lanes connected end to end (offline, synthetic)", "",
         "> One deterministic run through every lane: a synthetic M16-layout store, then F, E, D, A, C and B. No "
         "broker, network, VPS or production data is touched; writes stay inside a temporary work directory. "
         "Nothing here is market evidence.", "",
         "Generated by `python -m research.integration.pipeline --out docs/research/evidence/integration_e2e`.", "",
         "## Cross-lane invariants", "", "| check | result |", "|---|---|"]
    L += ["| %s | %s |" % (k.replace("_", " "), "✅" if v else "❌") for k, v in o["checks"].items()]
    L += ["", "## What each lane produced", "",
          "- **F (data):** %d symbols written; %d usable after the audit; excluded %s (suspected split or unusable); "
          "grades %s; snapshot `%s` over %d files." % (d["symbols"], d["usable"], d["excluded"] or "none",
                                                       d["grades"], d["snapshot_digest"][:16], d["files"]),
          "- **E (knowledge):** hypothesis `%s` was recorded and is now **%s**; library %s." % (
              o["hypothesis"]["id"], o["hypothesis"]["status"],
              {k: v for k, v in o["knowledge"].items() if k != "status"}),
          "- **D (genome):** %d grid points, %d invalid. Plateau-selected `%s` with %s. Portfolio: %d trades, "
          "return %+.1f %%, Sharpe %.2f; equal-weight buy-and-hold Sharpe %.2f. DSR of the best isolated point: "
          "%.3f with %d trials." % (
              g["grid_points"], g["invalid"], g["selected"], g["selected_params"], g["portfolio"]["n_trades"],
              100 * g["portfolio"]["total_return"], g["portfolio"]["sharpe_ann"],
              g["baselines"]["buy_and_hold_equal_weight"]["sharpe_ann"], g["dsr_best_isolated"]["dsr"],
              g["dsr_best_isolated"]["n_trials"]),
          "- **A (counterfactual):** %d candidates labelled against %d executed trades; %d definitive; statuses "
          "%s; mean definitive net R %+.3f; effective N %s (%s)." % (
              cf["candidates"], cf["executed_trades"], cf["definitive"], cf["status_counts"],
              cf["mean_r_net_definitive"], cf["effective_n"].get("n_effective"), cf["effective_n"].get("status")),
          "- **C (lab):** meta-labeling the genome's own candidates on %d rows. Verdicts vs logistic: %s; vs "
          "prior: %s; AUC %s; lab report card %s." % (lab["rows"], lab["verdicts"], lab["vs_prior"],
                                                       {k: round(v, 3) for k, v in lab["auc"].items()}, lab["card"]),
          "- **B (integrity):** genome report card **%s**: %s." % (o["report_card"]["verdict"],
                                                                 "; ".join(o["report_card"]["reasons"])),
          "- Ledger: %d entries, tip `%s`. Runtime %.0f s." % (o["ledger"]["entries"], (o["ledger"]["tip"] or "")[:16],
                                                               o["runtime_seconds"]), ""]
    return "\n".join(L)


def main(argv=None) -> int:
    import argparse
    import json
    import sys
    import tempfile
    import warnings
    warnings.filterwarnings("ignore")
    ap = argparse.ArgumentParser(description="research lanes end to end (offline)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as d:
        o = run(Path(d))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(o, indent=1, sort_keys=True, default=str) + "\n")
    out.with_suffix(".md").write_text(render(o))
    print("wrote %s.{md,json}" % out, file=sys.stderr)
    return 0 if all(o["checks"].values()) else 1


__all__ = ["AXES", "run", "render", "main"]

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
