"""research.integration.pipeline — the research lanes connected end to end (offline).

One deterministic run. It touches no broker, network, VPS or production data,
and it writes only inside the caller's work directory.

1. **F (data):** write a synthetic market with planted structure and light
   corruption as an M16-layout parquet store; snapshot it with a manifest and
   inventory it. Research reads the bars back from the store and uses only
   POINT-IN-TIME quality knowledge (second review, PR #11):
   - inadmissible bars (M16 hard rules, blocking quality-flag bits) are
     removed (``research_view``);
   - a symbol is eligible for new decisions at bar i unless a CAUSAL split
     flag (``causal_split_flags``: bars <= i only) fired at or before i;
     from then on it is quarantined (unadjusted prices are not comparable).
   The retrospective full-sample audit is still run and reported, but it
   decides nothing: excluding a symbol from its whole history because of a
   split detected later would leak the future into earlier decisions.
2. **E (knowledge):** sources and a falsifiable, point-in-time hypothesis.
3. **D (genome):** ledgered grid search of the mean-reversion template on the
   same as-of universe; plateau selection; portfolio run of the selected
   genome with per-candidate decision records and truthful fill times.
4. **A (counterfactual evidence):** EVERY engine decision becomes a candidate
   with its real stage: simulated entry, rejected by the template rule,
   blocked by risk, skipped for capacity, or blocked by a gate (position
   already open, re-entry, outside the as-of universe, unfillable, no next
   bar). All are labelled; ambiguous, unobserved and unfillable outcomes keep
   their status and are never turned into fills. Simulated entries are
   joined to the engine's trades by candidate id and fill-event time.
5. **C (model laboratory):** meta-label the genome's point-in-time
   candidates with purged walk-forward, causal conformal, ledgered trials.
6. **B (integrity):** report card for the selected genome whose outcomes are
   verified against the trial artifacts. The integrated pipeline has no
   calibrated canary of its own (lane C's separate canary is NOT borrowed),
   so it cannot PASS.
7. **E (outcome):** lineage, then the graveyard.

``checks`` lists the invariants the integration test asserts.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, Mapping, Tuple

import numpy as np
import pandas as pd

from research.agents.knowledge import KnowledgeLibrary, Prediction, Proposal
from research.agents.mock import FIXTURE_SOURCES
from research.agents.roles import Gate
from research.common.bars import TF_MINUTES
from research.common.rng import stream
from research.common.synthetic import MarketSpec, PlantedEffect, generate
from research.dataqa import inventory as INV
from research.dataqa import quality as QA
from research.dataqa import snapshot as SN
from research.evidence.candidates import Bracket, CandidateEvent, make_candidate_id, stage_counts
from research.evidence.costs import CostModel
from research.evidence.effective_n import effective_n
from research.evidence.labeler import SourceInfo, label_candidates
from research.genome import engine as E
from research.genome import schema as G
from research.genome import search as SR
from research.genome.templates import signals as template_signals
from research.integrity.ledger import Ledger
from research.integrity.provenance import code_identity
from research.integrity.report_card import GauntletConfig, evaluate as gauntlet
from research.mllab import evaluate as LAB
from research.mllab import models as MD
from research.mllab.datasets import build_dataset
from research.mllab.labels import BarrierSpec

DAY_NS = int(86400e9)
TF = "1D"
AXES = {"rsi_entry": [25.0, 30.0, 35.0], "bb_std": [1.5, 2.0, 2.5]}
# engine decision stage -> lane A candidate stage (a simulated entry is never "executed": no order exists)
STAGE_MAP = {"entered": "simulated_entry", "rejected_by_rule": "rejected", "position_already_open": "blocked_gate",
             "reentry_same_bar": "blocked_gate", "not_in_universe": "blocked_gate",
             "blocked_capacity": "skipped_capacity", "blocked_risk": "blocked_risk", "unfillable": "blocked_gate",
             "no_next_bar": "blocked_gate"}
ENGINE_CFG = E.EngineConfig(bar_minutes=TF_MINUTES[TF])     # synthetic bar-open convention, same as lane A


def point_in_time_universe(raw: Mapping[str, pd.DataFrame]) -> Tuple[Dict[str, pd.DataFrame],
                                                                     Dict[str, np.ndarray], Dict[str, Dict]]:
    """Admissible bars per symbol and an as-of eligibility mask that uses bars <= i only."""
    bars, member, info = {}, {}, {}
    for s, b in sorted(raw.items()):
        view, counts = QA.research_view(b)
        flags = QA.causal_split_flags(view)
        quarantined = np.logical_or.accumulate(flags) if len(flags) else flags.astype(bool)
        bars[s], member[s] = view, ~quarantined
        info[s] = {"rows": counts["rows"], "inadmissible_rows_removed": counts["excluded"],
                   "causal_split_flags": int(flags.sum()),
                   "quarantined_from": str(view["ts_utc"].iloc[int(np.argmax(flags))]) if flags.any() else None}
    return bars, member, info


def retrospective_exclusions(raw: Mapping[str, pd.DataFrame]) -> list:
    """The OLD (leaky) rule, kept only to report what it would have done: whole-history exclusion of any symbol
    with a split suspected anywhere in the full sample, or graded unusable."""
    q = QA.audit_panel(raw, timeframe=TF)["per_symbol"]
    return sorted(s for s, x in q.items() if x["grade"] == "unusable" or x["suspected_splits"])


def decide(genome: G.Genome, bars: Mapping[str, pd.DataFrame], member: Mapping[str, np.ndarray],
           cfg: E.EngineConfig = ENGINE_CFG) -> E.EngineResult:
    return E.run_genome(genome, bars, cfg, member)


def decisions_to_candidates(res: E.EngineResult, genome: G.Genome,
                            bars: Mapping[str, pd.DataFrame]) -> list:
    sig = {s: template_signals(genome, b) for s, b in bars.items()}
    row = {s: {pd.Timestamp(t): i for i, t in enumerate(pd.to_datetime(b["ts_utc"], utc=True))}
           for s, b in bars.items()}
    br = Bracket(genome.exits.stop_atr, genome.exits.target_atr or 3.0, genome.exits.time_stop_bars)
    out = []
    for d in res.decisions:
        s, ts = d["symbol"], pd.Timestamp(d["decision_ts"])
        a = float(sig[s]["atr"].iloc[row[s][ts]])
        out.append(CandidateEvent(candidate_id=d["candidate_id"], symbol=s, decision_ts_utc=ts, direction="long",
                                  stage=STAGE_MAP[d["stage"]], source="synthetic", timeframe=TF,
                                  strategy_id=genome.genome_id, atr_at_decision=a, bracket=br,
                                  reasons=tuple(x for x in ("engine:" + d["stage"], d["reason"]) if x)))
    return out


def _stage_report(lab: pd.DataFrame) -> Dict[str, Dict]:
    out = {}
    for st, g in lab.groupby("stage"):
        dfn = g[g["status"] == "definitive"]
        out[st] = {"n": int(len(g)), "status": g["status"].value_counts().to_dict(),
                   "definitive": int(len(dfn)),
                   "mean_r_net_definitive": float(dfn["r_net"].mean()) if len(dfn) else float("nan")}
    return out


def _neff(lab: pd.DataFrame) -> Dict:
    d = lab[lab["status"] == "definitive"]
    if not len(d):
        return {"status": "insufficient_evidence", "n_raw": 0}
    n = effective_n(d["r_net"].to_numpy(), dates=pd.to_datetime(d["decision_ts_utc"]).dt.date.astype(str),
                    symbols=d["symbol"].to_numpy(), entry_idx=d["entry_idx"].to_numpy(),
                    exit_idx=d["exit_idx"].to_numpy())
    return {k: n.get(k) for k in ("n_raw", "n_effective", "status")}


def run(workdir: Path, *, seed: int = 7, n_symbols: int = 16, n_sessions: int = 700) -> Dict:
    t0 = time.time()
    workdir = Path(workdir)
    L = Ledger()
    gate = Gate(L)
    kb = KnowledgeLibrary(L, gate)                       # no operator verifier: citations stay unverified

    # 1. F — data store, snapshot, inventory; point-in-time universe
    eff = (PlantedEffect("rsi", "rsi_oversold", 0.5, horizon=5, level=30.0),)
    mk = generate(MarketSpec(n_symbols=n_symbols, n_sessions=n_sessions, seed=seed, effects=eff,
                             missing_bar_prob=0.005, split_prob=0.15))
    store = workdir / "store"
    for s, b in mk.bars.items():
        p = store / "synthetic" / TF / ("%s.parquet" % s)
        p.parent.mkdir(parents=True, exist_ok=True)
        b.to_parquet(p, index=False)
    manifest = SN.build(store, name="integration")
    snap_problems = SN.verify(manifest, store)
    inv = INV.inventory_store(store)                     # retrospective: reported, decides nothing
    q = inv["quality"]["synthetic/%s" % TF]["per_symbol"]
    raw = {s: pd.read_parquet(store / "synthetic" / TF / ("%s.parquet" % s)) for s in sorted(q)}
    bars, member, pit = point_in_time_universe(raw)
    leaky_excluded = retrospective_exclusions(raw)
    data_digest = manifest["snapshot_digest"]
    first_day = str(min(pd.to_datetime(b["ts_utc"], utc=True).min() for b in bars.values()).date())

    # 2. E — knowledge and hypothesis
    sids = [kb.add_source("scout", s) for s in FIXTURE_SOURCES[:2]]
    hid = kb.propose(Proposal(
        statement="Oversold stocks below the lower Bollinger band with low RSI revert upward within days",
        mechanism="liquidity-driven overreaction", prediction=Prediction("report_card_verdict", ">=", 1.0),
        kill_criteria="report card not PASS, or the selected genome sits on no plateau", source_ids=tuple(sids),
        template="mean_reversion", proposed_by="hypothesist", test_window_start=first_day))

    # 3. D — ledgered grid search (same as-of universe and engine), plateau selection, portfolio run
    gs, bad = SR.grid("mean_reversion", AXES)
    fam = "integration:genome"
    df = SR.evaluate(gs, bars, ledger=L, family_id=fam, data_digest=data_digest, seed=seed, cfg=ENGINE_CFG,
                     membership=member, keep_equity=True)
    sel = SR.select(df, AXES, ledger=L, family_id=fam)
    best = {g.genome_id: g for g in gs}[sel["best_plateau"]["genome_id"]]
    best_tid = df.loc[df["genome_id"] == best.genome_id, "trial_id"].iloc[0]
    port = decide(best, bars, member)

    # 4. A — every engine decision is a candidate with its real stage; all are labelled
    cands = decisions_to_candidates(port, best, bars)
    cost = CostModel()
    lab = label_candidates(cands, bars, timeframe=TF, cost=cost, source=SourceInfo("synthetic", TF, False))
    by_id = lab.set_index("candidate_id")
    sim = lab[lab["stage"] == "simulated_entry"]
    join_ok, eod_pending = len(sim) == len(port.trades), 0
    for t in port.trades:
        r = by_id.loc[t.candidate_id] if t.candidate_id in by_id.index else None
        ok = (r is not None and r["stage"] == "simulated_entry" and pd.Timestamp(r["decision_ts_utc"]) == t.decision_ts
              and t.entry_fill_ts < t.entry_bar_close_ts)
        if ok and pd.isna(r["entry_ts_utc"]):
            # the label's bracket window runs past the data, so lane A keeps it PENDING with no entry recorded.
            # (Lane A labels use the counterfactual bracket - stop / target / time - not the template's signal
            # exit, so a trade the engine closed on a signal can still have an unresolved bracket.) The decision
            # and id join still hold; the entry time cannot be compared and is not asserted.
            ok = r["status"] == "pending"
            eod_pending += int(ok)
        elif ok:
            ok = pd.Timestamp(r["entry_ts_utc"]) == t.entry_fill_ts
        join_ok &= bool(ok)
    eng_counts = port.decision_counts()
    a_counts = stage_counts(cands)
    mapped = {}
    for k, v in eng_counts.items():
        mapped[STAGE_MAP[k]] = mapped.get(STAGE_MAP[k], 0) + v
    mapped = {k: v for k, v in mapped.items() if v}
    rowmap = {s: {pd.Timestamp(x): i for i, x in enumerate(pd.to_datetime(b["ts_utc"], utc=True))}
              for s, b in bars.items()}
    trades_across_flags = sum(1 for t in port.trades if not member[t.symbol][rowmap[t.symbol][t.exit_bar_close_ts]])

    # 5. C — meta-labeling of the genome's point-in-time candidates
    mem_by_obj = {id(bars[s]): member[s] for s in bars}

    def sampler(b, feats):
        m = template_signals(best, b)["entry"].to_numpy().copy() & mem_by_obj[id(b)]
        m[:60] = False
        return m
    spec = BarrierSpec(tp_mult=best.exits.target_atr or 3.0, sl_mult=best.exits.stop_atr,
                       horizon=best.exits.time_stop_bars)
    ds = build_dataset(bars, spec=spec, sampler=sampler, extra_meta={"snapshot_digest": data_digest,
                                                                      "genome_id": best.genome_id,
                                                                      "universe": "point_in_time"})
    fac = MD.factories(("prior", "logistic", "hgb"), seed=seed)
    labres = LAB.run(ds, fac, LAB.LabConfig(n_folds=4, embargo_ns=6 * DAY_NS, seed=seed), scenario="integration",
                     ledger=L)

    # 6. B — report card with outcome provenance for the selected genome
    r = np.array([t.r_multiple for t in port.trades if np.isfinite(t.r_multiple)])   # net of fees and slippage
    rets = [np.asarray(x, float) for x in df["_returns"]]
    fam_matrix = np.column_stack(rets) if len({len(x) for x in rets}) == 1 else None
    card = gauntlet(candidate_id="integration:%s" % best.genome_id, family_id=fam, gross=r, cost_per_obs=0.0,
                    n_trials=L.family_trial_count(fam), rng=stream(seed, "integration", "card"),
                    family_matrix=fam_matrix, cfg=GauntletConfig(n_boot=200), ledger=L,
                    trial_ids=tuple(df["trial_id"]), candidate_trial_id=best_tid)

    # 7. E — lineage and outcome
    kb.record_lineage("librarian", hypothesis_id=hid, genome_id=best.genome_id, trial_ids=list(df["trial_id"]))
    if card.verdict != "PASS":
        kb.reject("librarian", hid, reason="report card %s" % card.verdict, evidence=list(df["trial_id"]),
                  retest_trigger={"requires": ["approved_real_data_snapshot", "integrated_calibrated_canary"]})

    genome_trials = L.trials(fam)
    lab_trials = L.trials("mllab:integration")
    lab_rows_ok = True
    for s in bars:
        ts = pd.to_datetime(bars[s]["ts_utc"], utc=True).astype("int64").to_numpy()
        starts = ds.start[ds.symbol == s]
        idx = np.searchsorted(ts, starts)
        lab_rows_ok &= bool(len(idx) == 0 or member[s][np.minimum(idx, len(ts) - 1)].all())
    checks = {
        "snapshot_verifies": not snap_problems,
        "universe_is_point_in_time": all(
            np.array_equal(member[s], ~np.logical_or.accumulate(QA.causal_split_flags(bars[s]))) for s in bars),
        "inadmissible_bars_excluded": all(QA.admissible_mask(b).all() for b in bars.values()),
        "genome_trials_carry_snapshot_digest": all(t.payload["data_digest"] == data_digest for t in genome_trials),
        "genome_trials_carry_code_identity": all(t.payload["code_digest"] == code_identity(SR.CODE_PATHS)
                                                 for t in genome_trials),
        "genome_trials_carry_research_seed": all(t.payload["seed"] == seed for t in genome_trials),
        "lab_dataset_traces_to_snapshot": ds.meta.get("snapshot_digest") == data_digest,
        "lab_trials_carry_dataset_and_code": all(t.payload["data_digest"] == ds.digest and
                                                 t.payload["code_digest"] == code_identity(LAB.CODE_PATHS)
                                                 for t in lab_trials) and bool(lab_trials),
        "lab_rows_are_point_in_time_eligible": lab_rows_ok,
        "every_decision_is_one_candidate": len(cands) == len(port.decisions) == len(lab)
                                           and lab["candidate_id"].is_unique,
        "candidate_ids_use_lane_a_formula": all(
            c.candidate_id == make_candidate_id(c.symbol, c.decision_ts_utc, "long", best.genome_id) for c in cands),
        "stages_conserved": mapped == a_counts and sum(a_counts.values()) == len(cands),
        "engine_skips_match_decisions": sum(port.skipped.values()) == sum(
            v for k, v in eng_counts.items() if k not in ("entered", "rejected_by_rule", "position_already_open",
                                                         "no_next_bar")),
        "trades_join_simulated_entries_on_id_and_fill_time": bool(join_ok),
        "no_candidate_labelled_executed": "executed" not in a_counts,
        "ambiguous_bars_not_resolved_into_fills": bool(lab.loc[lab["status"] == "ambiguous", "outcome"].isna().all()),
        "card_charged_for_all_genome_trials": card.numbers["n_trials"] == float(len(genome_trials)),
        "card_provenance_verified": card.numbers.get("provenance_verified") == 1.0,
        "card_outcomes_verified": card.numbers.get("outcomes_verified") == 1.0,
        "card_cannot_pass_without_integrated_canary": card.verdict != "PASS",
        "card_computed_pbo_and_spa": fam_matrix is not None and "pbo" in card.numbers,
        "ledger_verifies": L.verify().ok,
    }
    out = {
        "data": {"symbols": n_symbols, "snapshot_digest": data_digest, "files": len(manifest["files"]),
                 "point_in_time": pit,
                 "inadmissible_rows_removed": int(sum(x["inadmissible_rows_removed"] for x in pit.values())),
                 "quarantined_symbols": sorted(s for s, x in pit.items() if x["quarantined_from"]),
                 "retrospective_grades": inv["quality"]["synthetic/%s" % TF]["grades"],
                 "retrospective_rule_would_exclude": leaky_excluded,
                 "retrospective_note": "full-sample audit: reported only, not used for any decision"},
        "hypothesis": {"id": hid, "status": kb.status(hid)},
        "genome": {"grid_points": len(gs), "invalid": bad, "selected": best.genome_id, "selected_trial": best_tid,
                   "selected_params": best.p, "selection": {k: v for k, v in sel.items() if k != "dsr_best_isolated"},
                   "dsr_best_isolated": sel["dsr_best_isolated"], "portfolio": E.metrics(port),
                   "baselines": SR.baselines(bars), "fill_time_basis": port.meta["fill_time_basis"],
                   "costs": {"engine_fee_bps": ENGINE_CFG.fee_bps, "engine_slippage_bps": ENGINE_CFG.slippage_bps,
                             "label_cost_model": cost.name},
                   "trades_ending_after_a_split_quarantine": int(trades_across_flags),
                   "trades_with_pending_bracket_labels": int(eod_pending)},
        "decisions": {"engine_stages": eng_counts, "candidate_stages": a_counts,
                      "per_stage": _stage_report(lab),
                      "effective_n_all_definitive": _neff(lab), "effective_n_simulated_entries": _neff(sim),
                      "note": "labels of non-entered candidates are counterfactual brackets that were never traded; "
                              "overlapping candidates are serially dependent (see effective N)"},
        "lab": {"rows": len(ds), "verdicts": {x["model"]: x["verdict"] for x in labres.comparisons},
                "vs_prior": {x["model"]: x["verdict"] for x in labres.vs_prior},
                "auc": {k: v.metrics["auc"] for k, v in labres.runs.items()},
                "card": labres.card.verdict if labres.card else None, "dataset_digest": ds.digest},
        "report_card": {"verdict": card.verdict, "reasons": list(card.reasons),
                        "provenance": {k: card.numbers.get(k) for k in ("provenance_verified", "outcomes_verified",
                                                                        "ledger_metadata_reconciled")}},
        "knowledge": kb.summary(),
        "ledger": {"entries": len(L.entries()), "tip": L.tip_hash},
        "checks": checks,
        "runtime_seconds": round(time.time() - t0, 1),
    }
    L.close()
    return out


def render(o: Dict) -> str:
    g, dc, lab, d = o["genome"], o["decisions"], o["lab"], o["data"]
    L = ["# Research lanes connected end to end (offline, synthetic)", "",
         "> One deterministic run through every lane: a synthetic M16-layout store, then F, E, D, A, C and B. No "
         "broker, network, VPS or production data is touched; writes stay inside a temporary work directory. "
         "Nothing here is market evidence.", "",
         "Generated by `python -m research.integration.pipeline --out docs/research/evidence/integration_e2e`.", "",
         "## Cross-lane invariants", "", "| check | result |", "|---|---|"]
    L += ["| %s | %s |" % (k.replace("_", " "), "✅" if v else "❌") for k, v in o["checks"].items()]
    L += ["", "## Point-in-time data handling", "",
          "- %d symbols in the snapshot `%s` (%d files). All stay in the universe; eligibility is decided bar by "
          "bar." % (d["symbols"], d["snapshot_digest"][:16], d["files"]),
          "- %d inadmissible bars removed (M16 hard rules or blocking quality flags)." % d["inadmissible_rows_removed"],
          "- Quarantined after a causal split flag (bars <= i only): %s." % (d["quarantined_symbols"] or "none"),
          "- The retrospective full-sample audit grades %s and its old exclusion rule would have removed %s from "
          "their WHOLE history, including decisions made before the split was knowable. It is reported only." % (
              d["retrospective_grades"], d["retrospective_rule_would_exclude"] or "none"), "",
          "## Decision records (selected genome `%s`)" % g["selected"], "",
          "| lane A stage | candidates | definitive | statuses | mean net R (definitive) |", "|---|---|---|---|---|"]
    for st, x in sorted(dc["per_stage"].items()):
        L.append("| %s | %d | %d | %s | %s |" % (st, x["n"], x["definitive"], x["status"],
                                                 "%+.3f" % x["mean_r_net_definitive"]
                                                 if np.isfinite(x["mean_r_net_definitive"]) else "—"))
    L += ["", "Engine stages: %s. Only `simulated_entry` candidates were filled by the backtest (simulated, never "
          "an order); every other label is a counterfactual bracket that was never traded. Labels measure the "
          "fixed bracket (stop / target / time), the engine's trades also use the template's signal exit, so the "
          "two outcomes of one entry are different measurements and are reported separately. Effective N: all "
          "definitive %s; simulated entries %s. Fills: %s; costs %s." % (
              dc["engine_stages"], dc["effective_n_all_definitive"], dc["effective_n_simulated_entries"],
              g["fill_time_basis"], g["costs"]), "",
          "## What each lane produced", "",
          "- **E (knowledge):** hypothesis `%s` is now **%s**; library %s." % (
              o["hypothesis"]["id"], o["hypothesis"]["status"],
              {k: v for k, v in o["knowledge"].items() if k != "status"}),
          "- **D (genome):** %d grid points, %d invalid. Plateau-selected `%s` with %s. Portfolio: %d trades, "
          "return %+.1f %%, Sharpe %.2f; equal-weight buy-and-hold Sharpe %.2f. DSR of the best isolated point: "
          "%.3f with %d trials. Trades ending after a split quarantine: %d." % (
              g["grid_points"], g["invalid"], g["selected"], g["selected_params"], g["portfolio"]["n_trades"],
              100 * g["portfolio"]["total_return"], g["portfolio"]["sharpe_ann"],
              g["baselines"]["buy_and_hold_equal_weight"]["sharpe_ann"], g["dsr_best_isolated"]["dsr"],
              g["dsr_best_isolated"]["n_trials"], g["trades_ending_after_a_split_quarantine"]),
          "- **C (lab):** meta-labeling the genome's point-in-time candidates on %d rows. Verdicts vs logistic: %s; "
          "vs prior: %s; AUC %s; lab report card %s." % (lab["rows"], lab["verdicts"], lab["vs_prior"],
                                                          {k: round(v, 3) for k, v in lab["auc"].items()}, lab["card"]),
          "- **B (integrity):** genome report card **%s** (provenance %s): %s. The integrated pipeline has no "
          "calibrated canary of its own; lane C's separate canary does not validate it." % (
              o["report_card"]["verdict"], o["report_card"]["provenance"], "; ".join(o["report_card"]["reasons"])),
          "- Ledger: %d entries, tip `%s`. Runtime %.0f s." % (o["ledger"]["entries"], (o["ledger"]["tip"] or "")[:16],
                                                               o["runtime_seconds"]), "",
          "## Isolation boundary (stated precisely)", "",
          "- Static: research code has no broker/network imports (AST boundary lint in the foundation suite).",
          "- In-process runtime: the integration test runs this pipeline with Python's socket connect/resolve and "
          "subprocess/os.system entry points replaced by functions that record and refuse any call, and asserts "
          "no socket call happened. The only process spawns attempted are joblib/loky's local CPU-topology "
          "probes (`lscpu --parse=core`, `cat /proc/cpuinfo`, triggered by scikit-learn); the guard refuses them "
          "and loky falls back to os.cpu_count. It also checks no broker/network/messaging module was imported.",
          "- Not claimed: OS-level network or credential isolation. A C extension or a separate process could "
          "bypass the in-process guard; an OS sandbox is required before any untrusted or networked code runs.", ""]
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


__all__ = ["AXES", "STAGE_MAP", "ENGINE_CFG", "point_in_time_universe", "retrospective_exclusions", "decide",
           "decisions_to_candidates", "run", "render", "main"]

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
