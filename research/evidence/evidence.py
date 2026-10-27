"""research.evidence.evidence — reproducible lane A evidence (CLI).

Four questions, answered on synthetic markets with known ground truth:

1. How much MORE trustworthy evidence does labelling every decision stage
   give than learning from executed trades alone (raw and EFFECTIVE N)?
2. How often are EOD labels ambiguous, how much does finer data resolve,
   and how wide is the pessimistic/optimistic range on what remains?
3. Do counterfactual labels match the M17 simulator exactly on unambiguous
   paths?
4. What would gate economics report for an illustrative gate layer, and are
   corrupted windows (missing bars, unadjusted splits) kept out of
   definitive labels?

Usage:  python -m research.evidence.evidence --out <dir> [--symbols N] [--sessions N]

Synthetic numbers describe the machinery. Counterfactual R is not P&L.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

from research.common.bars import multi_timeframe, resample
from research.common.rng import stream
from research.common.synthetic import MarketSpec, PlantedEffect, generate
from research.evidence import gate_economics as GE
from research.evidence import parity
from research.evidence.candidates import Bracket, CandidateEvent, make_candidate_id, stage_counts
from research.evidence.effective_n import effective_n
from research.evidence.labeler import SourceInfo, atr_asof, label_candidates, status_summary
from research.evidence.replay import apply_gates, position_release_times, scanner_stage_candidates


def _eff(lab: pd.DataFrame) -> Dict:
    d = lab[lab["status"] == "definitive"]
    return effective_n(d["r_net"].to_numpy(), dates=pd.to_datetime(d["decision_ts_utc"], utc=True).dt.floor("D").astype(str).to_numpy(),
                       symbols=d["symbol"].to_numpy(), sectors=d["sector"].to_numpy(),
                       entry_idx=d["entry_idx"].to_numpy(), exit_idx=d["exit_idx"].to_numpy())


def part_stage_replay(n_symbols: int, n_sessions: int, seed: int) -> Dict:
    spec = MarketSpec(n_symbols=n_symbols, n_sessions=n_sessions, timeframe="15m", seed=seed,
                      effects=(PlantedEffect("brk", "breakout", 0.08, horizon=26, lookback=40, vol_ratio=1.5),))
    m = generate(spec)
    sectors = dict(zip(m.symbols["symbol"], m.symbols["sector"]))
    bracket = Bracket(stop_atr_mult=1.0, target_atr_mult=1.5, max_holding_bars=26)
    cands: List[CandidateEvent] = []
    for s, b in m.bars.items():
        cands += scanner_stage_candidates(multi_timeframe(b), s, rng=stream(seed, "rejected", s), bracket=bracket,
                                          sector=sectors[s], rejected_rate=0.02)
    rules = {"low_volume": lambda e: e.features.get("vol_ratio", 1.0) < 0.8,
             "rsi_extreme": lambda e: (e.features.get("rsi", 50.0) > 70.0) if e.direction == "long"
             else (e.features.get("rsi", 50.0) < 30.0)}
    src = SourceInfo("synthetic", "15m", True)
    pre = label_candidates(cands, m.bars, timeframe="15m", source=src)       # labels do not depend on stage
    gated = apply_gates(cands, rules, max_entries_per_day=3, one_open_per_symbol=True,
                        release_times=position_release_times(pre))           # bar-time capacity
    lab = label_candidates(gated, m.bars, timeframe="15m", source=src)
    lab_exec = lab[lab["stage"] == "executed"]
    eff_all, eff_exec = _eff(lab), _eff(lab_exec)
    by_id = {c.candidate_id: c for c in gated}
    pr = parity.check(lab, m.bars, by_id, timeframe="15m")
    gates = GE.gate_table(lab, seed=seed, n_boot=500)
    stage_status = lab.groupby(["stage", "status"]).size().unstack(fill_value=0)
    return {"spec_digest": m.spec_digest, "n_candidates": len(gated), "stage_counts": stage_counts(gated),
            "status": status_summary(lab), "stage_status": stage_status.to_dict(orient="index"),
            "eff_all_stages": eff_all, "eff_executed_only": eff_exec,
            "multiplier_raw": eff_all["n_raw"] / max(eff_exec["n_raw"], 1.0),
            "multiplier_effective": eff_all["n_effective"] / max(eff_exec["n_effective"], 1.0),
            "parity": {"compared": pr.n_compared, "match": pr.n_match, "mismatch": pr.n_mismatch,
                       "known_divergence": pr.n_known_divergence, "skipped": pr.n_skipped},
            "gates_md": GE.to_markdown(gates),
            "mean_r_net_by_stage": lab[lab["status"] == "definitive"].groupby("stage")["r_net"].mean().to_dict()}


BRACKET_GRID = ((0.25, 0.375), (0.5, 0.75), (1.0, 1.5), (2.0, 3.0))


def part_eod_ambiguity(n_symbols: int, n_sessions: int, seed: int) -> Dict:
    """Daily candidates labelled on EOD bars, then resolved with 15m bars,
    across bracket widths (tight brackets are where EOD data fails)."""
    m = generate(MarketSpec(n_symbols=n_symbols, n_sessions=n_sessions, timeframe="15m", seed=seed + 1))
    daily = {s: resample(b, "1D") for s, b in m.bars.items()}
    fine = {s: b.assign(_ts=pd.to_datetime(b["ts_utc"], utc=True)) for s, b in m.bars.items()}

    def finer(sym, t0, t1):
        f = fine[sym]
        return f[(f["_ts"] > t0) & (f["_ts"] <= t1)]
    src = SourceInfo("synthetic", "1D", True)
    rows = []
    for stop_m, tgt_m in BRACKET_GRID:
        cands = []
        rng = stream(seed, "eod", repr(stop_m))
        for s, d in daily.items():
            atr = atr_asof(d).to_numpy()
            for i in range(20, len(d) - 1, 3):
                if not np.isfinite(atr[i]):
                    continue
                ts = pd.Timestamp(d["ts_utc"].iloc[i])
                direction = "long" if rng.random() < 0.5 else "short"
                cands.append(CandidateEvent(candidate_id=make_candidate_id(s, ts, direction, "eod_probe"), symbol=s,
                                            decision_ts_utc=ts, direction=direction, stage="historical_anchor",
                                            source="synthetic", timeframe="1D", strategy_id="eod_probe",
                                            atr_at_decision=float(atr[i]),
                                            bracket=Bracket(stop_atr_mult=stop_m, target_atr_mult=tgt_m,
                                                            max_holding_bars=10)))
        eod = label_candidates(cands, daily, timeframe="1D", source=src)
        res = label_candidates(cands, daily, timeframe="1D", source=src, finer=finer)
        amb = eod[eod["status"] == "ambiguous"]
        resolved = res.set_index("candidate_id").loc[amb["candidate_id"]] if len(amb) else res.iloc[0:0]
        fixed = resolved[resolved["status"] == "definitive"] if len(amb) else resolved
        rows.append({"stop_atr": stop_m, "target_atr": tgt_m, "n": len(cands),
                     "share_ambiguous_eod": float((eod["status"] == "ambiguous").mean()),
                     "share_ambiguous_after_15m": float((res["status"] == "ambiguous").mean()),
                     "resolved_by_15m": int(len(fixed)),
                     "pess_mean_r": float(amb["r_gross_pessimistic"].mean()) if len(amb) else float("nan"),
                     "opt_mean_r": float(amb["r_gross_optimistic"].mean()) if len(amb) else float("nan"),
                     "resolved_mean_r": float(fixed["r_gross"].mean()) if len(fixed) else float("nan"),
                     "resolved_stop_first": float((fixed["outcome"] == "stop").mean()) if len(fixed) else float("nan")})
    return {"grid": rows}


def part_data_quality(seed: int) -> Dict:
    m = generate(MarketSpec(n_symbols=12, n_sessions=400, seed=seed + 2, split_prob=0.5, missing_bar_prob=0.01,
                            delist_prob=0.3))
    cands = []
    for s, b in m.bars.items():
        atr = atr_asof(b).to_numpy()
        for i in range(20, len(b) - 1, 4):
            if np.isfinite(atr[i]):
                ts = pd.Timestamp(b["ts_utc"].iloc[i])
                cands.append(CandidateEvent(candidate_id=make_candidate_id(s, ts, "long", "dq"), symbol=s,
                                            decision_ts_utc=ts, direction="long", stage="historical_anchor",
                                            source="synthetic", timeframe="1D", strategy_id="dq",
                                            atr_at_decision=float(atr[i]), bracket=Bracket(2.0, 3.0, 20)))
    lab = label_candidates(cands, m.bars, timeframe="1D", source=SourceInfo("synthetic", "1D", False))
    meta = m.symbols.set_index("symbol")
    crosses = 0
    caught = 0
    for r in lab.itertuples(index=False):
        sa = meta.loc[r.symbol, "split_at"]
        if pd.notna(sa) and r.exit_ts_utc is not None and r.decision_ts_utc < sa <= r.exit_ts_utc:
            crosses += 1
            caught += int(r.status != "definitive")
    flags = lab["data_flags"].str.split("|").explode()
    return {"n": len(lab), "status": status_summary(lab), "windows_crossing_a_split": crosses,
            "of_which_not_definitive": caught,
            "delisted_symbols": int(m.symbols["delisted_at"].notna().sum()),
            "flag_counts": flags[flags != ""].value_counts().to_dict()}


def run(out_dir: Path, n_symbols: int = 12, n_sessions: int = 160, seed: int = 20261027) -> Dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    s = {"stage_replay": part_stage_replay(n_symbols, n_sessions, seed),
         "eod_ambiguity": part_eod_ambiguity(max(4, n_symbols // 2), n_sessions, seed),
         "data_quality": part_data_quality(seed),
         "config": {"n_symbols": n_symbols, "n_sessions": n_sessions, "seed": seed},
         "runtime_seconds": None}
    s["runtime_seconds"] = round(time.time() - t0, 1)
    (out_dir / "lane_a_evidence.json").write_text(json.dumps(s, indent=2, sort_keys=True, default=str))
    (out_dir / "lane_a_evidence.md").write_text(render(s))
    return s


def _f(v: float, fmt: str = "%.2f") -> str:
    return "n/a" if v is None or not np.isfinite(v) else fmt % v


def render(s: Dict) -> str:
    sr, ea, dq = s["stage_replay"], s["eod_ambiguity"], s["data_quality"]
    ex, al = sr["eff_executed_only"], sr["eff_all_stages"]
    L = ["# Lane A evidence: counterfactual candidate/outcome labels", "",
         "Generated by `python -m research.evidence.evidence` (seed %d). Synthetic markets; counterfactual R uses "
         "modelled fills and is **not P&L**. Numbers describe the machinery, not any market." % s["config"]["seed"], "",
         "## 1. Evidence multiplication (scanner replay, %d symbols x %d sessions of 15m bars)" % (
             s["config"]["n_symbols"], s["config"]["n_sessions"]), "",
         "Stages recorded: %s." % ", ".join("%s %d" % kv for kv in sorted(sr["stage_counts"].items())), "",
         "| learning set | definitive labels (raw N) | effective N (most conservative) | by uniqueness | two-way (date x symbol) |",
         "|---|---|---|---|---|",
         "| executed only (what a paper loop would learn from) | %d | %s | %s | %s |" % (
             ex["n_raw"], _f(ex["n_effective"], "%.0f"), _f(ex.get("n_uniqueness", float("nan")), "%.0f"),
             _f(ex.get("n_twoway", float("nan")), "%.0f")),
         "| every decision stage | %d | %s | %s | %s |" % (
             al["n_raw"], _f(al["n_effective"], "%.0f"), _f(al.get("n_uniqueness", float("nan")), "%.0f"),
             _f(al.get("n_twoway", float("nan")), "%.0f")),
         "", "Multiplier: **%sx raw**, **%sx effective**. The effective multiplier is the honest one: "
         "candidates on the same day and overlapping windows are not independent. The executed-only set comes "
         "from an ILLUSTRATIVE gate layer (3 entries/day, one open position per symbol until its labelled exit), "
         "so both multipliers depend on that scenario." % (
             _f(sr["multiplier_raw"], "%.1f"), _f(sr["multiplier_effective"], "%.1f")), "",
         "Label status shares (all stages): definitive %.1f %%, ambiguous %.1f %%, unobserved %.1f %%, "
         "unfillable %.1f %%, pending %.1f %%." % tuple(100 * sr["status"]["share_" + k] for k in
                                                       ("definitive", "ambiguous", "unobserved", "unfillable", "pending")),
         "", "## 2. M17 parity on unambiguous paths", "",
         "Compared %d long, definitive, stop/target-resolved labels with `bot.backtesting.execution.simulate`: "
         "**%d exact matches, %d mismatches**, %d documented divergences (gap through the target followed by a "
         "stop touch in the same bar, which M17 books as a stop), %d skipped." % (
             sr["parity"]["compared"], sr["parity"]["match"], sr["parity"]["mismatch"],
             sr["parity"]["known_divergence"], sr["parity"]["skipped"]),
         "", "## 3. EOD ambiguity and what finer data resolves", "",
         "Daily candidates (10-bar window, random long/short) labelled on EOD bars only, then re-labelled using "
         "the 15m bars of each ambiguous day. Pessimistic = stop first, optimistic = target first.", "",
         "| bracket (stop / target, x ATR) | candidates | ambiguous on EOD | ambiguous after 15m | resolved by 15m | "
         "mean R pessimistic / optimistic | mean R actually resolved | stop first among resolved |",
         "|---|---|---|---|---|---|---|---|"] + [
         "| %.2f / %.3g | %d | %.1f %% | %.1f %% | %d | %s / %s | %s | %s |" % (
             r["stop_atr"], r["target_atr"], r["n"], 100 * r["share_ambiguous_eod"],
             100 * r["share_ambiguous_after_15m"], r["resolved_by_15m"], _f(r["pess_mean_r"]), _f(r["opt_mean_r"]),
             _f(r["resolved_mean_r"]), _f(100 * r["resolved_stop_first"], "%.0f %%")) for r in ea["grid"]] + [
         "", "Tight brackets make EOD labels unreliable; treating ambiguous rows as certain would bias results "
         "anywhere within the pessimistic/optimistic range, so they never become definitive labels."] + [
         "", "## 4. Gate economics (illustrative gates on the replay)", "", sr["gates_md"],
         "## 5. Data quality keeps corrupted windows out", "",
         "%d candidates on a market with unadjusted splits, missing bars and %d delisted symbols: definitive %.1f %%, "
         "unobserved %.1f %%, pending %.1f %%. Windows crossing an unadjusted split: %d, of which %d were kept out "
         "of definitive labels. Flags: %s." % (
             dq["n"], dq["delisted_symbols"], 100 * dq["status"]["share_definitive"],
             100 * dq["status"]["share_unobserved"],
             100 * dq["status"]["share_pending"], dq["windows_crossing_a_split"], dq["of_which_not_definitive"],
             ", ".join("%s %d" % kv for kv in sorted(dq["flag_counts"].items())) or "none"),
         "", "_Runtime %.1f s._" % s["runtime_seconds"]]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--symbols", type=int, default=12)
    ap.add_argument("--sessions", type=int, default=160)
    a = ap.parse_args(argv)
    s = run(Path(a.out), a.symbols, a.sessions)
    print(json.dumps({"parity_mismatch": s["stage_replay"]["parity"]["mismatch"],
                      "multiplier_effective": round(s["stage_replay"]["multiplier_effective"], 2)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
