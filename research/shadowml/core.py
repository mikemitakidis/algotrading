"""research.shadowml — WP16 regime thresholds, WP17 meta-labelling, WP18 learned regime weights (SHADOW).

All three sit on the model laboratory (``research.mllab``): purged, embargoed,
expanding walk-forward folds, out-of-fold (OOF) probabilities, and ledgered
trials. They answer narrow questions about a STRATEGY TEMPLATE's own entries.

WP17, meta-labelling (canonical B16). The template is the primary model; its
entry bars are the events (``template_sampler``). Barrier labels use the
template's own stop / target / time stop. A secondary model's OOF P(win)
maps to a size multiplier in {0, 0.5, 1}:

  take       p above the fold threshold;
  size down  p within ``band`` below it;
  skip       otherwise.

It can never size UP. The threshold for fold j comes only from earlier folds'
OOF predictions RESOLVED before fold j's first decision (``mllab.evaluate``).

WP16, regime-aware thresholds (B17). The regime is the market-volatility
percentile (``ctx.mkt_vol_pct``) observed ``lag`` sessions EARLIER, which
models detection lag. It is "volatile" above 2/3, else "calm". Each regime
gets its own threshold from past resolved folds when it has enough trades;
otherwise the global threshold applies.

WP18, learned regime weights (B19). The model is a blend of a global model
and a regime-specific model (``mllab.models.RegimeModel``):
``p = w * p_regime + (1 - w) * p_global``. The weight w is chosen on past
resolved folds by log loss from a bounded grid [0, 1], and each fold's w is
reported (drift).

Every result compares OOF decisions against a reference on DAILY aggregates
(Newey-West, one-sided):
- IMPROVES or WORSE when the difference is significant at ``alpha``;
- NO_EVIDENCE otherwise;
- INCONCLUSIVE below the minimum number of OOF events.

Results are SHADOW: ``can_change_live`` is False. The ``evidence`` dict feeds a
WP14 proposal and never applies anything itself. Synthetic results describe the
machinery, not a market.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from research.genome.schema import Genome
from research.genome.templates import signals as template_signals
from research.integrity import stats as S
from research.integrity.ledger import Ledger
from research.mllab import datasets as D
from research.mllab import evaluate as EV
from research.mllab import models as MD
from research.mllab.labels import BarrierSpec

IMPROVES, NO_EVIDENCE, WORSE, INCONCLUSIVE = "IMPROVES", "NO_EVIDENCE", "WORSE", "INCONCLUSIVE"
MULTIPLIERS = (0.0, 0.5, 1.0)
VOL_SPLIT = 2.0 / 3.0
WEIGHT_GRID = (0.0, 0.25, 0.5, 0.75, 1.0)


@dataclass(frozen=True)
class ShadowConfig:
    meta_model: str = "hgb"
    baseline: str = "logistic"
    n_folds: int = 5
    min_train_groups: int = 2
    embargo_days: int = 6
    cost_r: float = 0.05
    min_trades: int = 30
    min_oof_events: int = 200
    band: float = 0.05
    regime_lag: int = 1
    min_regime_trades: int = 30
    alpha: float = 0.05
    seed: int = 0

    def lab(self) -> EV.LabConfig:
        return EV.LabConfig(n_folds=self.n_folds, min_train_groups=self.min_train_groups,
                            embargo_ns=self.embargo_days * 86_400 * 10**9, cost_r=self.cost_r,
                            baseline=self.baseline, min_trades=self.min_trades, seed=self.seed)


# ── events: the template's own entries ───────────────────────────────────────
def template_sampler(genome: Genome) -> D.Sampler:
    def f(bars, feats):
        return template_signals(genome, bars)["entry"].to_numpy(dtype=bool)
    f.__name__ = "template:%s" % genome.genome_id
    return f


def template_barrier(genome: Genome) -> BarrierSpec:
    e = genome.exits
    return BarrierSpec(tp_mult=float(e.target_atr if e.target_atr is not None else 3.0), sl_mult=float(e.stop_atr),
                       horizon=int(e.time_stop_bars))


def template_dataset(bars, genome: Genome, *, truth=None, extra_meta=None) -> D.EventDataset:
    return D.build_dataset(bars, spec=template_barrier(genome), sampler=template_sampler(genome), truth=truth,
                           extra_meta={"template": genome.template, "genome_id": genome.genome_id,
                                       **(extra_meta or {})})


def lab(ds: D.EventDataset, cfg: ShadowConfig, *, models: Sequence[str], ledger: Optional[Ledger] = None,
        scenario: str = "shadowml", extra: Optional[Dict] = None) -> EV.LabResult:
    """``extra``: additional named factories (e.g. ``regime_factory`` variants)."""
    ctx_cols = [i for i, f in enumerate(ds.feature_names) if f.startswith("ctx.")]
    fac = MD.factories([m for m in models if m not in (extra or {})], seed=cfg.seed,
                       regime_col=ds.feature_names.index("ctx.mkt_vol_pct"), context_cols=ctx_cols,
                       n_features=len(ds.feature_names))
    fac.update({k: v for k, v in (extra or {}).items() if k in models})
    return EV.run(ds, fac, cfg.lab(), scenario=scenario, ledger=ledger)


def regime_factory(ds: D.EventDataset, base: str, seed: int = 0, min_rows: int = 100):
    """A RegimeModel over any named model, split on the market-volatility percentile."""
    col = ds.feature_names.index("ctx.mkt_vol_pct")
    return lambda: MD.RegimeModel(lambda: MD.make(base, seed), col, VOL_SPLIT, min_rows=min_rows,
                                  name="regime_%s" % base)


# ── shared helpers ───────────────────────────────────────────────────────────
def _past_resolved(ds, fold_of, j) -> np.ndarray:
    te = np.flatnonzero(fold_of == j)
    past = np.flatnonzero((fold_of >= 0) & (fold_of < j))
    return past[ds.end[past] < ds.start[te].min()] if len(te) and len(past) else np.arange(0)


def _compare(ds, oof_mask, new_val, ref_val, cfg: ShadowConfig) -> Dict:
    days = ds.day[oof_mask]
    diff = pd.Series(new_val[oof_mask] - ref_val[oof_mask]).groupby(days).sum().to_numpy()
    n = int(oof_mask.sum())
    if n < cfg.min_oof_events or len(diff) < 10:
        return {"verdict": INCONCLUSIVE, "n_oof_events": n, "reason": "only_%d_oof_events_(min_%d)"
                % (n, cfg.min_oof_events)}
    m, t, p_better = S.mean_test(diff, 5)
    _, _, p_worse = S.mean_test(-diff, 5)
    v = IMPROVES if p_better < cfg.alpha else WORSE if p_worse < cfg.alpha else NO_EVIDENCE
    return {"verdict": v, "n_oof_events": n, "n_days": int(len(diff)), "mean_daily_diff": m, "t_hac": t,
            "p_better": p_better, "p_worse": p_worse,
            "new_total": float(new_val[oof_mask].sum()), "ref_total": float(ref_val[oof_mask].sum())}


def _evidence(cmp: Dict, kind: str) -> Dict:
    return {"wf_verdict": "PASS" if cmp["verdict"] == IMPROVES else
            "FAIL" if cmp["verdict"] == WORSE else "INCONCLUSIVE",
            "holdout_status": "NOT_RUN", "source": "research.shadowml.%s" % kind, "shadow": True}


# ── WP17: meta-labelling ─────────────────────────────────────────────────────
def size_multipliers(p: np.ndarray, thresholds: Sequence[float], fold_of: np.ndarray, band: float) -> np.ndarray:
    """{0, 0.5, 1} per OOF row; NaN outside test folds. Never above 1."""
    out = np.full(len(p), np.nan)
    for j, t in enumerate(thresholds):
        m = fold_of == j
        if not np.isfinite(t):                         # no profitable past threshold: stand aside
            out[m] = 0.0
            continue
        out[m] = np.where(p[m] > t, 1.0, np.where(p[m] > t - band, 0.5, 0.0))
    return out


def meta_label(ds: D.EventDataset, cfg: ShadowConfig = ShadowConfig(), *, ledger: Optional[Ledger] = None,
               res: Optional[EV.LabResult] = None) -> Dict:
    res = res or lab(ds, cfg, models=("prior", cfg.baseline, cfg.meta_model), ledger=ledger, scenario="wp17_meta")
    run = res.runs[cfg.meta_model]
    oof = res.oof
    mult = size_multipliers(run.p, run.thresholds, res.fold_of, cfg.band)
    r_net = ds.r - cfg.cost_r
    meta_val = np.where(oof, np.nan_to_num(mult) * r_net, 0.0)
    take_all = np.where(oof, r_net, 0.0)
    cmp = _compare(ds, oof, meta_val, take_all, cfg)
    used = np.unique(mult[np.isfinite(mult)])
    return {"kind": "meta_label", "model": cfg.meta_model, "shadow": True, "can_change_live": False,
            "multipliers_used": [float(x) for x in used], "max_multiplier": float(np.nanmax(mult)) if oof.any() else 0.0,
            "share_skipped": float(np.mean(mult[oof] == 0.0)) if oof.any() else float("nan"),
            "share_sized_down": float(np.mean(mult[oof] == 0.5)) if oof.any() else float("nan"),
            "thresholds": [float(t) for t in run.thresholds], "lab_metrics": dict(run.metrics),
            "comparison_vs_take_all": cmp, "evidence": _evidence(cmp, "meta_label"),
            "dataset_digest": ds.digest, "_mult": mult}


# ── WP16: regime-aware thresholds ────────────────────────────────────────────
def lagged_regime(ds: D.EventDataset, bars, lag: int) -> np.ndarray:
    """'volatile' / 'calm' / 'unknown' per event from the market-volatility
    percentile ``lag`` sessions before the decision (detection lag)."""
    per = {s: D.symbol_features(b) for s, b in bars.items()}
    pct = D.market_context(per, bars)["ctx.mkt_vol_pct"].shift(lag)
    at = pct.reindex(pd.to_datetime(ds.start, utc=True)).to_numpy()
    return np.where(np.isfinite(at), np.where(at > VOL_SPLIT, "volatile", "calm"), "unknown").astype(object)


def _thr(p, r_net, cfg, default):
    return EV._best_threshold(p, r_net, cfg.min_regime_trades, default)


def regime_thresholds(ds: D.EventDataset, bars, cfg: ShadowConfig = ShadowConfig(), *,
                      res: Optional[EV.LabResult] = None, ledger: Optional[Ledger] = None) -> Dict:
    res = res or lab(ds, cfg, models=("prior", cfg.baseline, cfg.meta_model), ledger=ledger, scenario="wp16_regime")
    run, fold_of, oof = res.runs[cfg.meta_model], res.fold_of, res.oof
    reg = lagged_regime(ds, bars, cfg.regime_lag)
    r_net = ds.r - cfg.cost_r
    take_reg = np.zeros(len(ds), dtype=bool)
    table = []
    for j, t_glob in enumerate(run.thresholds):
        te = fold_of == j
        past = _past_resolved(ds, fold_of, j)
        row = {"fold": j, "global": float(t_glob)}
        for r in ("calm", "volatile", "unknown"):
            pr = past[reg[past] == r]
            t = _thr(run.p[pr], r_net[pr], cfg, t_glob) if r != "unknown" and len(pr) >= cfg.min_regime_trades \
                else t_glob
            row[r] = float(t)
            m = te & (reg == r)
            take_reg[m] = run.p[m] > t
        table.append(row)
    new_val = np.where(oof & take_reg, r_net, 0.0)
    ref_val = np.where(oof & run.take, r_net, 0.0)
    cmp = _compare(ds, oof, new_val, ref_val, cfg)
    by = {r: {"oof_events": int((oof & (reg == r)).sum()), "taken": int((oof & take_reg & (reg == r)).sum()),
              "net_r": float(r_net[oof & take_reg & (reg == r)].sum())} for r in ("calm", "volatile", "unknown")}
    return {"kind": "regime_thresholds", "shadow": True, "can_change_live": False, "lag_sessions": cfg.regime_lag,
            "thresholds_by_fold": table, "by_regime": by, "comparison_vs_global_threshold": cmp,
            "evidence": _evidence(cmp, "regime_thresholds"), "_regime": reg}


# ── WP18: learned regime weights ─────────────────────────────────────────────
def regime_weights(ds: D.EventDataset, cfg: ShadowConfig = ShadowConfig(), *, res: Optional[EV.LabResult] = None,
                   ledger: Optional[Ledger] = None, global_model: str = "hgb", regime_model: str = "regime_hgb"
                   ) -> Dict:
    res = res or lab(ds, cfg, models=("prior", cfg.baseline, global_model, regime_model), ledger=ledger,
                     scenario="wp18_weights")
    pg, pr = res.runs[global_model].p, res.runs[regime_model].p
    fold_of, oof = res.fold_of, res.oof
    y = ds.y.astype(float)
    pb = np.full(len(ds), np.nan)
    weights = []
    for j in range(int(fold_of.max()) + 1 if len(fold_of) else 0):
        te = fold_of == j
        past = _past_resolved(ds, fold_of, j)
        if len(past) >= cfg.min_trades:
            losses = [EV._ll(y[past], w * pr[past] + (1 - w) * pg[past]).mean() for w in WEIGHT_GRID]
            w = float(WEIGHT_GRID[int(np.argmin(losses))])
        else:
            w = 0.0                                    # no evidence yet: the global model
        weights.append(w)
        pb[te] = w * pr[te] + (1 - w) * pg[te]
    ll_new = np.where(oof, -EV._ll(y, np.nan_to_num(pb, nan=0.5)), 0.0)
    ll_ref = np.where(oof, -EV._ll(y, np.nan_to_num(pg, nan=0.5)), 0.0)
    cmp = _compare(ds, oof, ll_new, ll_ref, cfg)
    drift = [abs(b - a) for a, b in zip(weights, weights[1:])]
    return {"kind": "regime_weights", "shadow": True, "can_change_live": False, "weights_by_fold": weights,
            "weight_bounds": [min(WEIGHT_GRID), max(WEIGHT_GRID)], "max_fold_to_fold_drift": max(drift) if drift
            else 0.0, "comparison_log_likelihood_vs_global": cmp, "evidence": _evidence(cmp, "regime_weights"),
            "_p": pb}


__all__ = ["IMPROVES", "NO_EVIDENCE", "WORSE", "INCONCLUSIVE", "MULTIPLIERS", "VOL_SPLIT", "WEIGHT_GRID",
           "ShadowConfig", "template_sampler", "template_barrier", "template_dataset", "lab", "regime_factory",
           "size_multipliers",
           "meta_label", "lagged_regime", "regime_thresholds", "regime_weights"]
