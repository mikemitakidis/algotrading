"""research.genome.evidence — reproducible lane D evidence report.

    python -m research.genome.evidence --out docs/research/evidence/lane_d_genome [--quick]

Synthetic markets with known truth only. The report covers:
1. trade-for-trade parity with the production M17 backtester;
2. a template × planted-effect matrix: each starter template on markets with
   a planted effect versus the PAIRED null market (same seed and noise, effect
   strength zero), so differences isolate the planted effect;
3. portfolio constraints, and survivorship bias measured on a market with
   delistings;
4. a ledgered parameter search: the surface, plateau versus peak, and the
   Deflated Sharpe of the best point charged for every trial, on a planted
   and a null market.

Nothing here is evidence about real markets, and no figure is P&L.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from research.common.hashing import digest
from research.common.synthetic import MarketSpec, PlantedEffect, generate
from research.genome import engine as E
from research.genome import parity as PAR
from research.genome import schema as G
from research.genome import search as SR
from research.genome import templates as T
from research.integrity.ledger import Ledger

EFFECTS = {
    "rsi_oversold": PlantedEffect("rsi", "rsi_oversold", 0.5, horizon=5, level=30.0),
    "breakout": PlantedEffect("bo", "breakout", 0.5, horizon=5, lookback=20, vol_ratio=1.5),
    "pullback": PlantedEffect("pb", "pullback", 0.5, horizon=5, fast=20, slow=50),
}
MATCH = {"mean_reversion": "rsi_oversold", "breakout_momentum": "breakout", "pullback_in_trend": "pullback"}
SEARCH_AXES = {"rsi_entry": [20.0, 25.0, 30.0, 35.0, 40.0], "bb_std": [1.5, 2.0, 2.5], "rsi_exit": [50.0, 55.0, 60.0]}


@dataclass(frozen=True)
class DConfig:
    seed: int = 11
    n_symbols: int = 20
    n_sessions: int = 1000
    n_seeds: int = 8
    parity_symbols: int = 10
    parity_sessions: int = 1500
    strength_scale: float = 0.2          # 0.5 x 0.2 = 0.1 sigma per bar: modest, interpretable effects
    profile: str = "full"


QUICK = DConfig(n_symbols=6, n_sessions=400, n_seeds=2, parity_symbols=2, parity_sessions=500, profile="quick")


def _log(m):
    print("[lane-d %s] %s" % (time.strftime("%H:%M:%S"), m), file=sys.stderr, flush=True)


def _js(x):
    if isinstance(x, dict):
        return {str(k): _js(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_js(v) for v in x]
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return None if not math.isfinite(float(x)) else float(x)
    if isinstance(x, np.bool_):
        return bool(x)
    return x


def parity_section(c: DConfig) -> Dict:
    m = generate(MarketSpec(n_symbols=c.parity_symbols, n_sessions=c.parity_sessions, seed=c.seed))
    rows, total, mism, maxd = [], 0, 0, 0.0
    for fast, slow in ((10, 30), (20, 50)):
        for sp, tp in ((0.05, 0.10), (None, None), (0.02, None), (None, 0.03)):
            n = mm = 0
            for s, b in m.bars.items():
                r = PAR.compare(b, s, fast=fast, slow=slow, stop_pct=sp, target_pct=tp)
                n += r["trades_m17"]
                mm += len(r["mismatches"])
                maxd = max(maxd, r["max_abs_field_diff"])
            rows.append({"fast": fast, "slow": slow, "stop_pct": sp, "target_pct": tp, "trades": n, "mismatches": mm})
            total += n
            mism += mm
    return {"rows": rows, "trades": total, "mismatches": mism, "max_abs_field_diff": maxd,
            "symbols": c.parity_symbols, "sessions": c.parity_sessions}


def _scaled(e: PlantedEffect, k: float) -> PlantedEffect:
    from dataclasses import replace
    return replace(e, strength=e.strength * k)


def matrix_section(c: DConfig) -> Dict:
    cells: Dict[str, Dict[str, List[Dict]]] = {t: {e: [] for e in EFFECTS} for t in T.STARTER_TEMPLATES}
    for i in range(c.n_seeds):
        sd = c.seed * 100 + i
        null = generate(MarketSpec(n_symbols=c.n_symbols, n_sessions=c.n_sessions, seed=sd))
        base = {t: E.run_genome(G.seed(t), null.bars) for t in T.STARTER_TEMPLATES}
        for ename, eff in EFFECTS.items():
            mk = generate(MarketSpec(n_symbols=c.n_symbols, n_sessions=c.n_sessions, seed=sd,
                                     effects=(_scaled(eff, c.strength_scale),)))
            for t in T.STARTER_TEMPLATES:
                r = E.run_genome(G.seed(t), mk.bars)
                a, b = E.metrics(r), E.metrics(base[t])
                cells[t][ename].append({"seed": sd, "ret": a["total_return"], "ret_null": b["total_return"],
                                        "mean_r": a["mean_r"], "mean_r_null": b["mean_r"],
                                        "trades": a["n_trades"], "trades_null": b["n_trades"]})
        _log("matrix seed %d/%d" % (i + 1, c.n_seeds))
    out = {}
    for t, row in cells.items():
        out[t] = {}
        for e, lst in row.items():
            d = np.array([x["ret"] - x["ret_null"] for x in lst])
            dr = np.array([x["mean_r"] - x["mean_r_null"] for x in lst if np.isfinite(x["mean_r"]) and
                           np.isfinite(x["mean_r_null"])])
            se = d.std(ddof=1) / math.sqrt(len(d)) if len(d) > 1 else float("nan")
            out[t][e] = {"mean_return_diff": float(d.mean()), "se": float(se),
                         "t": float(d.mean() / se) if se and se > 0 else float("nan"),
                         "positive_seeds": int((d > 0).sum()), "n_seeds": int(len(d)),
                         "mean_r_diff": float(dr.mean()) if len(dr) else float("nan"),
                         "mean_trades": float(np.mean([x["trades"] for x in lst])),
                         "matched": MATCH.get(t) == e, "runs": lst}
    return out


def constraints_section(c: DConfig) -> Dict:
    m = generate(MarketSpec(n_symbols=c.n_symbols, n_sessions=c.n_sessions, seed=c.seed + 1))
    g = G.seed("trend_confluence")
    sweep = []
    for k in (1, 3, 5, 10, c.n_symbols):
        r = E.run_genome(g, m.bars, E.EngineConfig(max_positions=k, max_gross_exposure=1.0))
        mt = E.metrics(r)
        sweep.append({"max_positions": k, **{x: mt[x] for x in ("n_trades", "total_return", "sharpe_ann",
                                                               "max_drawdown")},
                      "skipped": dict(r.skipped), "signals_seen": r.signals_seen})
    # Survivorship: a market where some symbols delist after a decline.
    d = generate(MarketSpec(n_symbols=c.n_symbols, n_sessions=c.n_sessions, seed=c.seed + 2, delist_prob=0.4))
    last = {s: b["ts_utc"].iloc[-1] for s, b in d.bars.items()}
    end = max(last.values())
    survivors = {s: b for s, b in d.bars.items() if last[s] == end}
    full = E.run_genome(g, d.bars)
    surv = E.run_genome(g, survivors)
    bh_full = SR.baselines(d.bars)["buy_and_hold_equal_weight"]
    bh_surv = SR.baselines(survivors)["buy_and_hold_equal_weight"]
    return {"sweep": sweep, "survivorship": {
        "symbols": len(d.bars), "delisted": len(d.bars) - len(survivors),
        "full_universe": E.metrics(full), "survivors_only": E.metrics(surv),
        "delisted_exits": sum(t.exit_reason == "delisted" for t in full.trades),
        "buy_and_hold_full": bh_full, "buy_and_hold_survivors": bh_surv}}


def search_section(c: DConfig) -> Dict:
    out = {}
    L = Ledger()
    for name, effects in (("planted_rsi_oversold", (_scaled(EFFECTS["rsi_oversold"], c.strength_scale),)), ("null", ())):
        m = generate(MarketSpec(n_symbols=c.n_symbols, n_sessions=c.n_sessions, seed=c.seed + 3, effects=effects))
        gs, bad = SR.grid("mean_reversion", SEARCH_AXES)
        fam = "genome:mean_reversion:%s" % name
        df = SR.evaluate(gs, m.bars, ledger=L, family_id=fam, data_digest=SR.data_digest(m.bars), keep_equity=True)
        sel = SR.select(df, SEARCH_AXES, ledger=L, family_id=fam)
        pl = SR.plateau(df, SEARCH_AXES, "sharpe_ann")
        base = SR.baselines(m.bars)
        sel["share_beats_buy_and_hold"] = float((df["sharpe_ann"] > base["buy_and_hold_equal_weight"]["sharpe_ann"]).mean())
        out[name] = {"selection": sel, "invalid_points": bad, "baselines": base,
                     "surface": [{k: r[k] for k in ("rsi_entry", "bb_std", "rsi_exit", "sharpe_ann", "total_return",
                                                    "n_trades", "trial_id")} | {"plateau": float(pl[i])}
                                 for i, r in df.iterrows()]}
        _log("search %s: %d points" % (name, len(df)))
    out["ledger"] = {"entries": len(L.entries()), "verified": L.verify().ok, "tip": L.tip_hash}
    L.close()
    return out


def _versions() -> Dict[str, str]:
    from importlib.metadata import PackageNotFoundError, version
    out = {}
    for m in ("numpy", "pandas", "scipy"):
        try:
            out[m] = version(m)
        except PackageNotFoundError:
            out[m] = "not installed"
    return out


def run_evidence(c: DConfig = DConfig()) -> Dict:
    t0 = time.time()
    _log("parity")
    par = parity_section(c)
    _log("matrix")
    mat = matrix_section(c)
    _log("constraints")
    con = constraints_section(c)
    _log("search")
    sea = search_section(c)
    j = _js({"lane": "D", "config": asdict(c), "parity": par, "matrix": mat, "constraints": con, "search": sea,
             "effects": {k: asdict(v) for k, v in EFFECTS.items()},
             "genomes": {t: G.seed(t).to_dict() for t in T.STARTER_TEMPLATES}, "versions": _versions()})
    j["report_digest"] = digest(j)
    j["runtime_seconds"] = round(time.time() - t0, 1)
    return j


def _f(x, fmt="%.3f"):
    return "—" if x is None or (isinstance(x, float) and not math.isfinite(x)) else fmt % x


def render_markdown(j: Dict) -> str:
    c, par, mat, con, sea = j["config"], j["parity"], j["matrix"], j["constraints"], j["search"]
    L = ["# Lane D — Strategy Genome, starter templates and portfolio backtester (synthetic, known truth)", "",
         "> **Read this first.** Every market here is synthetic, with planted effects of known size. The report "
         "tests the *machinery*: parity with M17, causality, the accounting, and whether each template finds the "
         "effect it was designed for. It is **not** evidence of any edge in a real market. Returns are simulated "
         "on synthetic prices after %s bps fees and %s bps slippage per side; they are not P&L. Nothing here is "
         "connected to production execution." % (E.EngineConfig().fee_bps, E.EngineConfig().slippage_bps), "",
         "Generated by `python -m research.genome.evidence` (profile `%s`, seed %d). Runtime: %.0f s. Report "
         "digest: `%s`." % (c["profile"], c["seed"], j["runtime_seconds"], j["report_digest"][:16]), ""]
    diag = [(t, MATCH[t]) for t in MATCH]
    L += ["## Summary", "", "| check | result |", "|---|---|",
          "| M17 parity (trades compared / mismatches) | %d / %d, max field difference %.2e |" % (
              par["trades"], par["mismatches"], par["max_abs_field_diff"])]
    for t, e in diag:
        cell = mat[t][e]
        L.append("| `%s` on its planted `%s` effect vs the paired null | mean return %+.2f %% (t = %s), positive in "
                 "%d / %d seeds |" % (t, e, 100 * cell["mean_return_diff"], _f(cell["t"], "%.1f"),
                                      cell["positive_seeds"], cell["n_seeds"]))
    sv = con["survivorship"]
    L.append("| Survivorship bias (`trend_confluence`, %d of %d symbols delisted) | survivors-only return %+.1f %% vs "
             "full universe %+.1f %% |" % (sv["delisted"], sv["symbols"], 100 * sv["survivors_only"]["total_return"],
                                          100 * sv["full_universe"]["total_return"]))
    for name in ("planted_rsi_oversold", "null"):
        s = sea[name]["selection"]
        L.append("| Parameter search on `%s` (%d trials) | best Sharpe %.2f, plateau %.2f, DSR %s |" % (
            name, s["n_trials_family"], s["best_isolated"]["sharpe_ann"], s["best_isolated"]["plateau"],
            _f((s["dsr_best_isolated"] or {}).get("dsr"))))
    L += ["", "## 1. Trade-for-trade parity with M17", "",
          "The production simulator `bot.backtesting.execution.simulate` and the research portfolio engine "
          "(`m17_compat=True`, one slot, percentage stops) ran on the same %d synthetic symbols × %d sessions with "
          "the M17 `sma_crossover` signals. Every trade's timestamps, quantity, prices, exit reason, fees, "
          "slippage, P&L and bars held were compared, along with the final equity." % (par["symbols"],
                                                                                     par["sessions"]), "",
          "| SMA fast/slow | stop | target | trades | mismatches |", "|---|---|---|---|---|"]
    for r in par["rows"]:
        L.append("| %d/%d | %s | %s | %d | %d |" % (r["fast"], r["slow"], _f(r["stop_pct"], "%.2f"),
                                                    _f(r["target_pct"], "%.2f"), r["trades"], r["mismatches"]))
    L += ["", "## 2. Templates × planted effects (paired with the null market)", "",
          "Each cell is the mean, over %d seeds, of the template's total return on a market with the planted "
          "effect MINUS its return on the paired null market. The paired market has the same seed and noise, "
          "with effect strength zero. The standard error is across seeds. Diagonal cells (★) are the effect each "
          "template was designed to find. Planted strength: %s σ per bar for 5 bars after the trigger." % (
              c["n_seeds"], ", ".join("%s %.2f" % (k, v["strength"] * c["strength_scale"])
                                      for k, v in j["effects"].items())), "",
          "| template | " + " | ".join("`%s`" % e for e in EFFECTS) + " |", "|---|" + "---|" * len(EFFECTS)]
    for t in T.STARTER_TEMPLATES:
        cells = []
        for e in EFFECTS:
            x = mat[t][e]
            cells.append("%s%+.2f %% ± %.2f (%d/%d)" % ("★ " if x["matched"] else "", 100 * x["mean_return_diff"],
                                                       100 * x["se"], x["positive_seeds"], x["n_seeds"]))
        L.append("| `%s` | %s |" % (t, " | ".join(cells)))
    L += ["", "`trend_confluence` has no dedicated planted effect. Its row shows whether short-horizon effects "
          "leak into a trend-following rule.", "",
          "## 3. Portfolio constraints and survivorship", "",
          "`trend_confluence` defaults on %d symbols × %d sessions, sweeping the position-count cap (gross "
          "exposure cap 100 %%):" % (c["n_symbols"], c["n_sessions"]), "",
          "| max positions | signals | trades | skipped (capacity / exposure / cash) | return | Sharpe | max DD |",
          "|---|---|---|---|---|---|---|"]
    for r in con["sweep"]:
        sk = r["skipped"]
        L.append("| %d | %d | %d | %d / %d / %d | %+.1f %% | %.2f | %.1f %% |" % (
            r["max_positions"], r["signals_seen"], r["n_trades"], sk["max_positions"], sk["gross_exposure"],
            sk["insufficient_cash"], 100 * r["total_return"], r["sharpe_ann"], 100 * r["max_drawdown"]))
    L += ["", "**Survivorship.** On a market where %d of %d symbols decline (−0.15 σ per bar over their last 40 "
          "bars) and then delist, the same genome was run twice. The full universe keeps delisted symbols until "
          "they stop trading; %d position(s) were closed at delisting. The survivors-only universe is the classic "
          "biased backtest. The difference between the rows is the survivorship effect in this sample; its sign "
          "and size depend on the sample." % (
              sv["delisted"], sv["symbols"], sv["delisted_exits"]), "",
          "| universe | trades | return | Sharpe | buy-and-hold return |", "|---|---|---|---|---|",
          "| full (as traded) | %d | %+.1f %% | %.2f | %+.1f %% |" % (
              sv["full_universe"]["n_trades"], 100 * sv["full_universe"]["total_return"],
              sv["full_universe"]["sharpe_ann"], 100 * sv["buy_and_hold_full"]["total_return"]),
          "| survivors only (biased) | %d | %+.1f %% | %.2f | %+.1f %% |" % (
              sv["survivors_only"]["n_trades"], 100 * sv["survivors_only"]["total_return"],
              sv["survivors_only"]["sharpe_ann"], 100 * sv["buy_and_hold_survivors"]["total_return"]), "",
          "## 4. Ledgered parameter search: surface, plateau, deflation", "",
          "The `mean_reversion` grid is %s: %d points, with invalid combinations refused by the validator. Every "
          "point is a ledgered trial. The plateau score is the median Sharpe of a point and its one-step "
          "neighbours. The DSR of the best isolated point is charged for every trial in its ledger family, with "
          "the cross-trial Sharpe variance measured from the trials." % (
              " × ".join("%s %s" % (k, v) for k, v in SEARCH_AXES.items()),
              sea["planted_rsi_oversold"]["selection"]["n_points"]), "",
          "| market | trials | best isolated Sharpe (its plateau) | best plateau Sharpe (its plateau) | grid share Sharpe > 0 | grid share beating buy-and-hold | DSR of best isolated | buy-and-hold Sharpe |",
          "|---|---|---|---|---|---|---|---|"]
    for name in ("planted_rsi_oversold", "null"):
        s = sea[name]["selection"]
        L.append("| %s | %d | %.2f (%.2f) | %.2f (%.2f) | %.0f %% | %.0f %% | %s | %.2f |" % (
            name, s["n_trials_family"], s["best_isolated"]["sharpe_ann"], s["best_isolated"]["plateau"],
            s["best_plateau"]["sharpe_ann"], s["best_plateau"]["plateau"], 100 * s["share_positive"],
            100 * s["share_beats_buy_and_hold"],
            _f((s["dsr_best_isolated"] or {}).get("dsr")),
            sea[name]["baselines"]["buy_and_hold_equal_weight"]["sharpe_ann"]))
    L += ["", "Synthetic markets drift upward in the calm regime, so a long-only grid shows positive Sharpe even on "
          "the null market. The honest comparisons are against buy-and-hold and the deflated Sharpe.", "",
          "Ledger: %d entries, chain verified = %s, tip `%s`." % (
        sea["ledger"]["entries"], sea["ledger"]["verified"], (sea["ledger"]["tip"] or "")[:16]), "",
          "## 5. Reproducibility", "",
          "- Command: `python -m research.genome.evidence --out docs/research/evidence/lane_d_genome%s`" % (
              " --quick" if c["profile"] == "quick" else ""),
          "- Seed genomes: " + ", ".join("`%s`" % g["genome_id"] for g in j["genomes"].values()),
          "- Libraries: " + ", ".join("%s %s" % kv for kv in sorted(j["versions"].items())),
          "- The engine is deterministic: the same inputs give the same trade-list digest (tested).", "",
          "## What this does and does not show", "",
          "- **Does:** the research engine reproduces M17 exactly where their semantics overlap, and extends it "
          "to portfolios with explicit, tested capacity, exposure, cash, membership, delisting, time and trailing "
          "rules. Templates are causal, and each is measured against the effect it targets. The search charges "
          "for every trial and reports surfaces, not just peaks.",
          "- **Does not:** show that any template works on real markets. Synthetic markets are long-biased "
          "(positive drift in the calm regime), so absolute returns mostly reflect the market, not the template; "
          "only the paired differences isolate the planted effects. Real-data evaluation needs approved data "
          "(lane F), pre-registration (lane B) and the forward-test ladder.", ""]
    return "\n".join(L)


def write(j: Dict, out: Path) -> Tuple[Path, Path]:
    out.parent.mkdir(parents=True, exist_ok=True)
    jp, mp = out.with_suffix(".json"), out.with_suffix(".md")
    jp.write_text(json.dumps(j, indent=1, sort_keys=True) + "\n")
    mp.write_text(render_markdown(j))
    return jp, mp


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args(argv)
    import warnings
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    j = run_evidence(QUICK if a.quick else DConfig())
    jp, mp = write(j, Path(a.out))
    _log("wrote %s and %s" % (jp, mp))
    return 0


__all__ = ["DConfig", "QUICK", "EFFECTS", "MATCH", "run_evidence", "render_markdown", "write", "main"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
