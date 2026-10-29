"""research.optimiser.walkforward — the M21.2 / M21.3 walk-forward optimiser driver (canonical B11, B12, B08; WP13).

Per template:

1. **Seal.** The last ``holdout_frac`` of the timeline goes into a
   ``SealedHistory``. Every later step sees only the research period. The
   sealed part is released ONCE per family, ledgered.
2. **Walk forward.** The research period is cut into ``n_folds +
   min_train_groups`` chronological blocks. Fold j trains on every earlier
   block (expanding) and tests block ``min_train_groups + j``; the first
   ``embargo_bars`` of each test block are skipped. A run sees bars only up to
   the end of its window (open positions close at the window end), and
   entries are allowed only inside the window (``membership`` mask), so no
   training trade can overlap a test window. ``leakage_report`` verifies this
   independently in the tests.
3. **Search.** In each fold, every grid genome is evaluated on the training
   window and recorded as a ledgered trial (``research.genome.search``). The
   **plateau** point is chosen, not the isolated peak.
4. **Test.** The chosen genome and the template's DEFAULT genome run on the
   test window GROSS (no engine fees). Every trade is re-priced with the WP12
   economics model, which sets point-in-time liquidity, participation caps
   and fees; trades it cannot fill are dropped, never priced as free. This is
   repeated at 2x costs and at larger sizes (liquidity sensitivity).
   Buy-and-hold and no-trade on the same window are the baselines.
5. **Integrity.** The out-of-sample (OOS) net daily returns of all folds are
   concatenated. The verdict uses: the Deflated Sharpe Ratio charged for
   EVERY trial in the template family; PBO over the research-period grid; the
   minimum number of OOS trades; survival at 2x costs; and comparison with
   the default parameters.
6. **Holdout.** Only a template that PASSES walk-forward is re-optimised on
   the whole research period (plateau pick). It is then evaluated ONCE on the
   sealed history with the same economics.

Verdicts: PASS, FAIL, INCONCLUSIVE.
- Thin OOS evidence is INCONCLUSIVE, never a pass.
- A positive OOS mean that fails the multiplicity-charged DSR is INCONCLUSIVE.
- A net OOS loss, a loss at 2x costs, or PBO above the cap is FAIL.

Historical holdout caveat: the sealed history existed before this run, so it
is "one-shot" (enforced by the ledger) but not provably out-of-sample. The
provably-forward holdout is the C-5 forward window, pre-registered and
attested with ``research.integrity.prereg`` and ``holdout.HoldoutVault``.

``origin="synthetic"`` results describe the machinery, not a market. Nothing
here contacts a broker, the network or production data.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from research.common.hashing import digest
from research.economics.model import INCONCLUSIVE as ECON_INCONCLUSIVE
from research.economics.model import (NO_FILL, CorporateActions, EconomicsModel, FillBar, TradeSpec, liquidity_asof,
                                      price_trade)
from research.genome import engine as E
from research.genome import search as SR
from research.genome.schema import Genome, seed as seed_genome, with_params
from research.genome.templates import STARTER_TEMPLATES, signals as template_signals
from research.integrity import stats as S
from research.integrity.ledger import Ledger, LedgerError
from research.integrity.pbo import pbo as pbo_fn

PASS, FAIL, INCONCLUSIVE = "PASS", "FAIL", "INCONCLUSIVE"
HOLDOUT_NOT_RUN = "NOT_RUN"

DEFAULT_AXES: Dict[str, Dict[str, Tuple]] = {
    "mean_reversion": {"rsi_entry": (25.0, 30.0, 35.0), "bb_std": (1.5, 2.0, 2.5)},
    "breakout_momentum": {"lookback": (15, 20, 25), "vol_ratio_min": (1.25, 1.5, 1.75)},
    "pullback_in_trend": {"rsi_max": (45.0, 50.0, 55.0), "extend_atr": (0.75, 1.0, 1.25)},
    "trend_confluence": {"min_votes": (3, 4, 5), "ema_fast": (15, 20, 25)},
}


@dataclass(frozen=True)
class WFConfig:
    templates: Tuple[str, ...] = STARTER_TEMPLATES
    axes: Optional[Mapping[str, Mapping[str, Sequence]]] = None
    n_folds: int = 4
    min_train_groups: int = 2
    embargo_bars: int = 5
    holdout_frac: float = 0.2
    metric: str = "sharpe_ann"
    min_oos_trades: int = 30
    dsr_min: float = 0.95
    pbo_max: float = 0.5
    pbo_blocks: int = 8
    stress: float = 2.0
    size_multipliers: Tuple[float, ...] = (1.0, 10.0, 100.0)
    economics: EconomicsModel = field(default_factory=EconomicsModel)
    engine: E.EngineConfig = field(default_factory=E.EngineConfig)
    basis: str = "split_adjusted"

    def __post_init__(self):
        if self.n_folds < 2 or self.min_train_groups < 1 or self.embargo_bars < 0:
            raise ValueError("need n_folds >= 2, min_train_groups >= 1, embargo_bars >= 0")
        if not 0.0 < self.holdout_frac < 0.5:
            raise ValueError("holdout_frac must be in (0, 0.5)")
        unknown = [t for t in self.templates if t not in DEFAULT_AXES and not (self.axes and t in self.axes)]
        if unknown:
            raise ValueError("no search axes for %s" % unknown)

    def axes_for(self, t: str) -> Mapping[str, Sequence]:
        return (self.axes or {}).get(t) or DEFAULT_AXES[t]


# ── sealed history ───────────────────────────────────────────────────────────
class SealedHistory:
    """Splits per-symbol bars at ``seal_ts`` and releases the sealed part once
    per family (ledgered). Research code receives only ``research_bars``."""

    def __init__(self, ledger: Ledger, name: str, bars: Mapping[str, pd.DataFrame], seal_ts: pd.Timestamp):
        self._ledger, self.name, self.seal_ts = ledger, name, pd.Timestamp(seal_ts)
        if ledger.entries("historical_holdout_seal", name):
            raise LedgerError("historical holdout %r already sealed" % name)
        self._full = {s: b.reset_index(drop=True) for s, b in bars.items()}
        self.research_bars = {s: b.loc[pd.to_datetime(b["ts_utc"], utc=True) < self.seal_ts].reset_index(drop=True)
                              for s, b in self._full.items()}
        sealed_rows = sum(len(b) - len(self.research_bars[s]) for s, b in self._full.items())
        self.sealed_digest = SR.data_digest({s: b.loc[pd.to_datetime(b["ts_utc"], utc=True) >= self.seal_ts]
                                             for s, b in self._full.items()})
        ledger.append("historical_holdout_seal", {"seal_ts_utc": self.seal_ts.isoformat(), "sealed_rows": sealed_rows,
                                                  "sealed_digest": self.sealed_digest,
                                                  "caveat": "historical: one-shot, not provably out-of-sample"},
                      ref_id=name)

    def release(self, family_id: str, spec_digest: str) -> Dict[str, pd.DataFrame]:
        if any(e.payload.get("family_id") == family_id
               for e in self._ledger.entries("historical_holdout_release", self.name)):
            raise LedgerError("historical holdout already spent for family %r" % family_id)
        self._ledger.append("historical_holdout_release", {"family_id": family_id, "spec_digest": spec_digest,
                                                           "sealed_digest": self.sealed_digest}, ref_id=self.name)
        return {s: b.copy() for s, b in self._full.items()}


# ── windows ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Window:
    lo: int                     # first timeline index where entries are allowed
    hi: int                     # last timeline index (inclusive) the run can see


def timeline_of(bars: Mapping[str, pd.DataFrame]) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(sorted(set().union(*[set(pd.to_datetime(b["ts_utc"], utc=True)) for b in bars.values()])))


def folds(n: int, cfg: WFConfig) -> List[Tuple[Window, Window]]:
    groups = np.array_split(np.arange(n), cfg.n_folds + cfg.min_train_groups)
    out = []
    for j in range(cfg.n_folds):
        g = cfg.min_train_groups + j
        train = Window(int(groups[0][0]), int(groups[g - 1][-1]))
        test = Window(min(int(groups[g][0]) + cfg.embargo_bars, int(groups[g][-1])), int(groups[g][-1]))
        out.append((train, test))
    return out


def _slice(bars, tl, w: Window, membership=None):
    end = tl[w.hi]
    lo_ts = tl[w.lo]
    sub, mask = {}, {}
    for s, b in bars.items():
        ts = pd.to_datetime(b["ts_utc"], utc=True)
        keep = (ts <= end).to_numpy()
        if not keep.any():
            continue
        sub[s] = b.loc[keep].reset_index(drop=True)
        m = (ts[keep] >= lo_ts).to_numpy()
        if membership is not None and s in membership:
            m &= np.asarray(membership[s], dtype=bool)[keep]
        mask[s] = m
    return sub, mask


# ── economics re-pricing ─────────────────────────────────────────────────────
def reprice(res: E.EngineResult, bars: Mapping[str, pd.DataFrame], genome: Genome, model: EconomicsModel, *,
            size_mult: float = 1.0, actions: Optional[Mapping[str, CorporateActions]] = None,
            basis: str = "split_adjusted") -> List:
    """WP12 economics for every trade of a GROSS engine run (fees and slippage 0)."""
    out = []
    sig_cache: Dict[str, pd.DataFrame] = {}
    for t in res.trades:
        b = bars[t.symbol]
        ts = pd.to_datetime(b["ts_utc"], utc=True)
        if t.symbol not in sig_cache:
            sig_cache[t.symbol] = template_signals(genome, b)
        idx = pd.DatetimeIndex(ts)
        dec = int(idx.get_loc(pd.Timestamp(t.decision_ts)))
        e_i = dec + 1
        x_i = int(idx.get_loc(pd.Timestamp(t.exit_bar_close_ts)))
        atr = float(sig_cache[t.symbol]["atr"].iloc[dec])
        risk = genome.exits.stop_atr * atr if np.isfinite(atr) and atr > 0 else None
        fb = lambda i: FillBar(*(float(b[k].iloc[i]) for k in ("open", "high", "low", "close", "volume")))
        spec = TradeSpec(t.symbol, "long", float(t.qty) * size_mult, float(t.entry_price), float(t.exit_price),
                         ts.iloc[e_i].date(), ts.iloc[x_i].date(), risk_per_share=risk,
                         stop_gap=t.exit_reason == "stop_loss" and t.exit_fill_basis == "bar_open_gap",
                         entry_bar=fb(e_i), exit_bar=None if t.exit_reason in ("eod", "delisted") else fb(x_i))
        liq = liquidity_asof(b, t.decision_ts, symbol=t.symbol)
        out.append((t, price_trade(spec, model=model, entry_liq=liq,
                                   actions=(actions or {}).get(t.symbol, CorporateActions(True)), basis=basis)))
    return out


def net_equity(res: E.EngineResult, priced, tl, w: Window) -> pd.Series:
    """Net equity up to the window end: gross engine equity minus each trade's
    economics cost (booked at exit), minus the gross P&L of trades the
    economics model could not fill (they never happened)."""
    eq = res.equity.reindex(tl[: w.hi + 1]).ffill()
    adj = pd.Series(0.0, index=eq.index)
    for t, te in priced:
        at = pd.Timestamp(t.exit_bar_close_ts)
        if at not in adj.index:
            continue
        if te.priced:
            adj.loc[at] -= te.total_cost - te.dividend_cash
        else:
            adj.loc[at] -= t.pnl_absolute
    return eq + adj.cumsum()


def _net_daily(res: E.EngineResult, priced, tl, w: Window) -> np.ndarray:
    """Daily net returns inside the window (see ``net_equity``)."""
    seg = net_equity(res, priced, tl, w).iloc[max(w.lo - 1, 0): w.hi + 1]
    return seg.pct_change().dropna().to_numpy()


def _oos(genome, bars, tl, w, cfg, membership, actions, *, model=None, size_mult=1.0):
    gross_cfg = replace(cfg.engine, fee_bps=0.0, slippage_bps=0.0)
    sub, mask = _slice(bars, tl, w, membership)
    res = E.run_genome(genome, sub, gross_cfg, mask)
    priced = reprice(res, sub, genome, model or cfg.economics, size_mult=size_mult, actions=actions, basis=cfg.basis)
    r = _net_daily(res, priced, tl, w)
    filled = [te for _, te in priced if te.priced]
    return {"returns": r, "n_trades": len(filled), "n_unfilled": len(priced) - len(filled),
            # kept apart (review of PR #19): a NO_FILL never happened; an INCONCLUSIVE trade was entered but
            # its exit price is unknown (halted / zero-volume exit bar, unverified raw basis)
            "n_no_fill": sum(te.status == NO_FILL for _, te in priced),
            "n_inconclusive": sum(te.status == ECON_INCONCLUSIVE for _, te in priced),
            "n_exit_capacity_breach": sum("exit_capacity_breach" in te.flags for te in filled),
            "net_usd": float(sum(te.net_pnl for te in filled)),
            "net_r": [te.net_r for te in filled if np.isfinite(te.net_r)],
            "capped": sum(te.status == "CAPPED" for te in filled), "res": res, "priced": priced}


def _sharpe_ann(r) -> float:
    r = np.asarray(r, dtype=float)
    return float(r.mean() / r.std(ddof=1) * math.sqrt(252)) if len(r) > 2 and r.std(ddof=1) > 0 else 0.0


def require_close_stamped(bars: Mapping[str, pd.DataFrame]) -> None:
    """Real bars must be WP11 ``to_close_stamped`` output: every row carries a
    ``bar_open_ts`` strictly before its ``ts_utc`` (the close). Raw M16 rows
    (stamped at the bar START, no ``bar_open_ts``) are refused — fail closed
    (review of PR #20)."""
    for s in sorted(bars):
        b = bars[s]
        if "bar_open_ts" not in b.columns:
            raise ValueError("real bars for %s carry no bar_open_ts: convert M16 start stamps with "
                             "research.dataqa.stamps.to_close_stamped first" % s)
        bo, ts = pd.to_datetime(b["bar_open_ts"], utc=True), pd.to_datetime(b["ts_utc"], utc=True)
        if bo.isna().any() or ts.isna().any() or (bo >= ts).any():
            raise ValueError("real bars for %s: bar_open_ts must be strictly before every ts_utc (close)" % s)


# ── per-template run ─────────────────────────────────────────────────────────
def run_template(template: str, bars: Mapping[str, pd.DataFrame], cfg: WFConfig, *, ledger: Ledger,
                 data_seed: int, sealed: Optional[SealedHistory] = None,
                 membership: Optional[Mapping[str, np.ndarray]] = None,
                 actions: Optional[Mapping[str, CorporateActions]] = None, origin: str = "synthetic") -> Dict:
    if origin not in ("synthetic", "real"):
        raise ValueError("origin must be 'synthetic' or 'real'")
    if origin == "real":
        require_close_stamped(bars)
    family = "wf:%s" % template
    axes = cfg.axes_for(template)
    genomes, n_invalid = SR.grid(template, axes)
    tl = timeline_of(bars)
    ddig = SR.data_digest(bars)
    fold_rows, oos_r, oos_def_r, oos_stress_r, net_r, trades = [], [], [], [], [], 0
    size_rows = {m: [] for m in cfg.size_multipliers}
    for k, (tr, te) in enumerate(folds(len(tl), cfg)):
        sub, mask = _slice(bars, tl, tr, membership)
        df = SR.evaluate(genomes, sub, ledger=ledger, family_id=family, data_digest=ddig, seed=data_seed,
                         cfg=cfg.engine, membership=mask)
        sel = SR.select(df, axes, ledger=ledger, family_id=family, metric=cfg.metric)
        chosen = {g.genome_id: g for g in genomes}[sel["best_plateau"]["genome_id"]]
        o = _oos(chosen, bars, tl, te, cfg, membership, actions)
        d = _oos(seed_genome(template), bars, tl, te, cfg, membership, actions)
        st = _oos(chosen, bars, tl, te, cfg, membership, actions, model=cfg.economics.scale(cfg.stress))
        for m in cfg.size_multipliers:
            sz = o if m == 1.0 else _oos(chosen, bars, tl, te, cfg, membership, actions, size_mult=m)
            size_rows[m].append(sz["net_usd"])
        tsub, _ = _slice(bars, tl, Window(te.lo, te.hi))
        bh = SR.baselines({s: b.loc[pd.to_datetime(b["ts_utc"], utc=True) >= tl[te.lo]].reset_index(drop=True)
                           for s, b in tsub.items() if (pd.to_datetime(b["ts_utc"], utc=True) >= tl[te.lo]).any()},
                          cfg.engine)["buy_and_hold_equal_weight"]
        oos_r.append(o["returns"])
        oos_def_r.append(d["returns"])
        oos_stress_r.append(st["returns"])
        net_r += o["net_r"]
        trades += o["n_trades"]
        fold_rows.append({"fold": k, "train": [str(tl[tr.lo]), str(tl[tr.hi])], "test": [str(tl[te.lo]),
                                                                                          str(tl[te.hi])],
                          "chosen": dict(chosen.p), "chosen_genome_id": chosen.genome_id,
                          "plateau_pick_genome_id": sel["best_plateau"]["genome_id"],
                          "chosen_plateau": sel["best_plateau"]["plateau"],
                          "isolated_peak": sel["best_isolated"], "train_points": int(len(df)),
                          "oos_trades": o["n_trades"], "oos_unfilled": o["n_unfilled"], "oos_capped": o["capped"],
                          "oos_no_fill": o["n_no_fill"], "oos_inconclusive": o["n_inconclusive"],
                          "oos_exit_capacity_breach": o["n_exit_capacity_breach"],
                          "oos_net_usd": o["net_usd"], "oos_sharpe": _sharpe_ann(o["returns"]),
                          "default_sharpe": _sharpe_ann(d["returns"]), "stress_net_sharpe": _sharpe_ann(st["returns"]),
                          "buy_and_hold_sharpe": bh["sharpe_ann"]})
    R = np.concatenate(oos_r) if oos_r else np.array([])
    n_trials = ledger.family_trial_count(family)
    srv = ledger.family_metric(family, "sr_per_period")
    sr_var = float(np.var(srv, ddof=1)) if len(srv) > 1 and np.var(srv, ddof=1) > 0 else None
    dsr = S.deflated_sharpe_ratio(R, n_trials, sr_var) if len(R) > 3 else None
    # PBO over the research period, grid x daily returns (gross engine, research costs)
    full = Window(0, len(tl) - 1)
    pbo_res, pbo_note = None, ""
    try:
        sub, mask = _slice(bars, tl, full, membership)
        cols = []
        for g in genomes:
            eq = E.run_genome(g, sub, cfg.engine, mask).equity.reindex(tl).ffill()
            cols.append(eq.pct_change().fillna(0.0).to_numpy())
        pbo_res = pbo_fn(np.column_stack(cols), n_blocks=cfg.pbo_blocks)
    except ValueError as exc:
        pbo_note = str(exc)
    summary = {
        "oos_days": int(len(R)), "oos_trades": int(trades), "oos_sharpe_ann": _sharpe_ann(R),
        "oos_no_fill": int(sum(f["oos_no_fill"] for f in fold_rows)),
        "oos_inconclusive": int(sum(f["oos_inconclusive"] for f in fold_rows)),
        "oos_exit_capacity_breach": int(sum(f["oos_exit_capacity_breach"] for f in fold_rows)),
        "oos_mean_net_r": float(np.mean(net_r)) if net_r else float("nan"),
        "default_params_oos_sharpe_ann": _sharpe_ann(np.concatenate(oos_def_r)) if oos_def_r else 0.0,
        "stress_oos_sharpe_ann": _sharpe_ann(np.concatenate(oos_stress_r)) if oos_stress_r else 0.0,
        "stress_multiplier": cfg.stress,
        "size_sensitivity_net_usd": {str(m): float(sum(v)) for m, v in size_rows.items()},
        "n_trials_family": int(n_trials), "invalid_grid_points": int(n_invalid),
        "dsr": None if dsr is None else {"dsr": dsr.dsr, "sr": dsr.sr, "sr0": dsr.sr0, "n_obs": dsr.n_obs,
                                         "n_trials": dsr.n_trials},
        "pbo": None if pbo_res is None else {"pbo": pbo_res.pbo, "n_splits": pbo_res.n_splits,
                                             "degradation_slope": pbo_res.degradation_slope},
        "pbo_note": pbo_note,
        "param_stability": _stability(fold_rows),
    }
    verdict, reasons = _verdict(summary, cfg)
    out = {"template": template, "family_id": family, "origin": origin, "folds": fold_rows, "summary": summary,
           "verdict": verdict, "reasons": reasons, "holdout": {"status": HOLDOUT_NOT_RUN}}
    if verdict == PASS and sealed is not None:
        out["holdout"] = _holdout(template, cfg, ledger=ledger, family=family, sealed=sealed, research=bars,
                                  data_seed=data_seed, membership=membership, actions=actions)
    ledger.append("wf_run", {k: v for k, v in out.items() if k != "folds"} | {"n_folds": len(fold_rows)},
                  ref_id=family)
    return out


def _stability(rows) -> Dict:
    if not rows:
        return {}
    keys = sorted(rows[0]["chosen"])
    return {k: {"values": [r["chosen"][k] for r in rows], "distinct": len({r["chosen"][k] for r in rows})}
            for k in keys}


def _verdict(s: Dict, cfg: WFConfig) -> Tuple[str, List[str]]:
    reasons = []
    # PBO is measured on the research-period grid, independent of the OOS trade count:
    # an overfit search fails whatever the OOS sample size.
    if s["pbo"] is not None and s["pbo"]["pbo"] > cfg.pbo_max:
        return FAIL, ["pbo_%.2f_above_%.2f" % (s["pbo"]["pbo"], cfg.pbo_max)]
    if s["oos_trades"] < cfg.min_oos_trades:
        return INCONCLUSIVE, ["only_%d_oos_trades_(min_%d)" % (s["oos_trades"], cfg.min_oos_trades)]
    if s["oos_sharpe_ann"] <= 0 or not (s["oos_mean_net_r"] > 0):
        return FAIL, ["no_oos_edge_after_costs"]
    if s["stress_oos_sharpe_ann"] <= 0:
        return FAIL, ["edge_does_not_survive_%gx_costs" % s["stress_multiplier"]]
    if s.get("oos_exit_capacity_breach", 0):
        reasons.append("exit_capacity_breach_trades_%d" % s["oos_exit_capacity_breach"])
    if s.get("oos_inconclusive", 0):
        # entered trades whose exit price is unknown cannot support a PASS
        return INCONCLUSIVE, reasons + ["oos_trades_with_unknown_exit_price_%d" % s["oos_inconclusive"]]
    if s["pbo"] is None:
        reasons.append("pbo_unavailable")
    if s["dsr"] is None or not (s["dsr"]["dsr"] >= cfg.dsr_min):
        return INCONCLUSIVE, reasons + ["dsr_below_%.2f_after_%d_trials" % (cfg.dsr_min, s["n_trials_family"])]
    if s["oos_sharpe_ann"] <= s["default_params_oos_sharpe_ann"]:
        reasons.append("optimisation_adds_nothing_over_default_params")
    if "pbo_unavailable" in reasons:
        return INCONCLUSIVE, reasons
    return PASS, reasons


def _holdout(template, cfg, *, ledger, family, sealed: SealedHistory, research, data_seed, membership, actions):
    tl = timeline_of(research)
    axes = cfg.axes_for(template)
    genomes, _ = SR.grid(template, axes)
    sub, mask = _slice(research, tl, Window(0, len(tl) - 1), membership)
    df = SR.evaluate(genomes, sub, ledger=ledger, family_id=family, data_digest=SR.data_digest(research),
                     seed=data_seed, cfg=cfg.engine, membership=mask)
    pick = {g.genome_id: g for g in genomes}[SR.select(df, axes, ledger=ledger, family_id=family,
                                                       metric=cfg.metric)["best_plateau"]["genome_id"]]
    full = sealed.release(family, pick.genome_id)
    ftl = timeline_of(full)
    lo = int(np.searchsorted(ftl.asi8, sealed.seal_ts.value))
    w = Window(lo, len(ftl) - 1)
    o = _oos(pick, full, ftl, w, cfg, None, actions)
    st = _oos(pick, full, ftl, w, cfg, None, actions, model=cfg.economics.scale(cfg.stress))
    ok = o["n_trades"] >= max(5, cfg.min_oos_trades // 4) and o["n_inconclusive"] == 0
    status = (INCONCLUSIVE if not ok else PASS if (_sharpe_ann(o["returns"]) > 0 and _sharpe_ann(st["returns"]) > 0)
              else FAIL)
    return {"status": status, "genome": dict(pick.p), "genome_id": pick.genome_id, "trades": o["n_trades"],
            "no_fill": o["n_no_fill"], "inconclusive": o["n_inconclusive"],
            "exit_capacity_breach": o["n_exit_capacity_breach"],
            "net_usd": o["net_usd"], "sharpe_ann": _sharpe_ann(o["returns"]),
            "stress_sharpe_ann": _sharpe_ann(st["returns"]),
            "n_trials_family_at_holdout": int(ledger.family_trial_count(family)),
            "caveat": "historical one-shot holdout; not provably out-of-sample (C-5 forward window is)"}


def run(bars: Mapping[str, pd.DataFrame], cfg: WFConfig = WFConfig(), *, ledger: Ledger, data_seed: int,
        origin: str = "synthetic", membership: Optional[Mapping[str, np.ndarray]] = None,
        actions: Optional[Mapping[str, CorporateActions]] = None, holdout_name: str = "wf-holdout") -> Dict:
    """All templates. ``bars``: close-stamped daily bars per symbol (WP11
    ``to_close_stamped`` output for real data, enforced)."""
    if origin not in ("synthetic", "real"):
        raise ValueError("origin must be 'synthetic' or 'real'")
    if origin == "real":
        require_close_stamped(bars)
    tl = timeline_of(bars)
    seal_ts = tl[int(len(tl) * (1.0 - cfg.holdout_frac))]
    sealed = SealedHistory(ledger, holdout_name, bars, seal_ts)
    research = sealed.research_bars
    mem = None
    if membership is not None:
        mem = {s: np.asarray(membership[s], dtype=bool)[:len(research[s])] for s in research if s in membership}
    per = {t: run_template(t, research, cfg, ledger=ledger, data_seed=data_seed, sealed=sealed, membership=mem,
                           actions=actions, origin=origin) for t in cfg.templates}
    return {"origin": origin, "seal_ts_utc": seal_ts.isoformat(), "config": _cfg_dict(cfg), "templates": per,
            "ledger_tip": ledger.tip_hash, "ledger_ok": ledger.verify().ok,
            "banner": "SYNTHETIC: describes the machinery, not a market" if origin == "synthetic" else ""}


def _cfg_dict(cfg: WFConfig) -> Dict:
    d = {k: v for k, v in asdict(cfg).items() if k not in ("economics", "engine")}
    d["economics"] = cfg.economics.name
    d["engine"] = {"fee_bps": cfg.engine.fee_bps, "slippage_bps": cfg.engine.slippage_bps}
    d["digest"] = digest(d)
    return d


__all__ = ["PASS", "FAIL", "INCONCLUSIVE", "HOLDOUT_NOT_RUN", "DEFAULT_AXES", "WFConfig", "SealedHistory", "Window",
           "timeline_of", "folds", "reprice", "net_equity", "require_close_stamped", "run_template", "run"]
