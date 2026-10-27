"""research.mllab.evaluate — walk-forward model comparison with paired tests.

The question this answers is the operator's: does a more sophisticated model
GENUINELY improve out-of-sample, cost-adjusted results over a simpler one?

Protocol (no tuning on test data):
1. Purged, embargoed, expanding walk-forward folds (research.integrity.splits).
2. Every model is fitted on each training window and predicts the next test
   window only; predictions are pooled out-of-fold.
3. The trading threshold for fold j is chosen ONLY from out-of-fold
   predictions of folds < j (fold 0 uses the training break-even rate).
   Split-conformal sets for fold j are calibrated the same way.
4. Probability quality: AUC, log loss, Brier, ECE (M18 calibration code).
   Decision quality: trades, coverage, mean and total net R per trade.
5. Paired tests against the baseline (and the prior) on DAILY aggregates, so
   same-day correlation does not inflate significance: log-loss gain and
   daily net-R difference, Newey–West t, one-sided p, Holm-adjusted across
   challengers. Verdicts: IMPROVES (both), PROBABILITIES_ONLY, DECISIONS_ONLY,
   NO_EVIDENCE, WORSE. Skill gate: a gain over the baseline only counts if
   the challenger also beats the base-rate prior in the same way.
6. Every model is a ledgered trial; the best challenger gets an integrity
   report card charged for every model ever tried in the family. With a
   ledger the card's inputs are reconciled against it (trial ids, family
   count); without one the card is a local diagnostic and cannot PASS.

Known limitations (stated, not hidden): the split-conformal sets for fold j
are calibrated on the PAST folds' out-of-fold predictions, which came from
earlier fits of the same model family, not the fold-j fit; markets are not
exchangeable. Coverage is therefore measured out of sample, never assumed.
Holm adjustment is applied within each comparison table (vs baseline, vs
prior, extra pairs), not across tables; the report card's trial count is
the across-everything correction.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from bot.ml.evaluation.calibration import expected_calibration_error
from research.common.rng import stream
from research.integrity import multiple_testing as MT
from research.integrity import stats as S
from research.integrity.ledger import Ledger, TrialRecord
from research.integrity.controls import BatteryResult
from research.integrity.report_card import GauntletConfig, ReportCard, evaluate as gauntlet
from research.integrity.splits import walk_forward
from research.mllab.datasets import EventDataset
from research.mllab.uncertainty import SplitConformal

VERDICTS = ("IMPROVES", "PROBABILITIES_ONLY", "DECISIONS_ONLY", "NO_EVIDENCE", "WORSE")
EPS = 1e-6


@dataclass(frozen=True)
class LabConfig:
    n_folds: int = 5
    min_train_groups: int = 2
    embargo_ns: int = 0
    cost_r: float = 0.05
    baseline: str = "logistic"
    min_trades: int = 30
    conformal_alpha: float = 0.2
    alpha: float = 0.05
    hac_lags: int = 5
    seed: int = 0


@dataclass
class ModelRun:
    name: str
    p: np.ndarray                 # out-of-fold P(win), NaN outside test folds
    take: np.ndarray              # bool decisions on OOF rows
    take_conformal: np.ndarray    # bool decisions using conformal singleton {win}
    thresholds: List[float]
    fit_seconds: float
    metrics: Dict[str, float] = field(default_factory=dict)
    member_p: Optional[np.ndarray] = None   # (n, k) OOF member probabilities for ensembles, NaN outside test folds


@dataclass
class LabResult:
    scenario: str
    dataset_digest: str
    n_rows: int
    oof: np.ndarray
    runs: Dict[str, ModelRun]
    comparisons: List[Dict]
    vs_prior: List[Dict]
    card: Optional[ReportCard]
    fold_sizes: List[Dict]
    runtime_seconds: float
    meta: Dict = field(default_factory=dict)
    fold_of: Optional[np.ndarray] = None    # test-fold index per row (-1 = never tested)
    trial_ids: Dict[str, str] = field(default_factory=dict)


def verdict(holm_p_better_prob: float, holm_p_better_dec: float, holm_p_worse_prob: float,
            alpha: float) -> str:
    """Verdict from Holm-adjusted one-sided p-values. IMPROVES needs BOTH
    better probabilities (log loss) and better decisions (daily net R)."""
    better_p, better_d = holm_p_better_prob <= alpha, holm_p_better_dec <= alpha
    if better_p and better_d:
        return "IMPROVES"
    if better_p:
        return "PROBABILITIES_ONLY"
    if better_d:
        return "DECISIONS_ONLY"
    if holm_p_worse_prob <= alpha:
        return "WORSE"
    return "NO_EVIDENCE"


def skill_gate(row: Dict, prior_row: Optional[Dict], alpha: float) -> Dict:
    """A claim against a reference OTHER than the base-rate prior counts only
    where the challenger shows the same kind of improvement over the prior.

    Beating a baseline that is itself worse than the prior (an over-fitted or
    mis-calibrated logistic regression on a null market, say) demonstrates
    less damage, not information. The raw verdict is kept in
    ``verdict_raw``; a downgrade is explained in ``skill_gate``."""
    row["verdict_raw"] = row["verdict"]
    if prior_row is None:
        row["skill_gate"] = "ungated: no prior in this run"
        return row
    p_ok = prior_row["holm_p_logloss"] <= alpha
    d_ok = prior_row["holm_p_decision"] <= alpha
    row["verdict"] = verdict(row["holm_p_logloss"] if p_ok else 1.0, row["holm_p_decision"] if d_ok else 1.0,
                             row["holm_p_worse"], alpha)
    row["skill_gate"] = ("" if row["verdict"] == row["verdict_raw"] else
                         "downgraded from %s: no matching significant gain over the base-rate prior"
                         % row["verdict_raw"])
    return row


def _ll(y, p):
    p = np.clip(p, EPS, 1 - EPS)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def _best_threshold(p, r_net, min_trades: int, default: float) -> float:
    """Threshold maximising total net R on PAST out-of-fold predictions."""
    if len(p) < min_trades:
        return default
    grid = np.unique(np.quantile(p, np.linspace(0.0, 0.95, 40)))
    best, best_v = default, -np.inf
    for t in grid:
        m = p > t
        if m.sum() < min_trades:
            continue
        v = r_net[m].sum()
        if v > best_v:
            best, best_v = float(t), v
    return best if best_v > 0 else float("inf")      # no profitable past threshold: stand aside


def _breakeven(y, r, cost):
    wins, losses = r[y == 1], r[y == 0]
    if not len(wins) or not len(losses):
        return 0.5
    rw, rl = wins.mean(), losses.mean()
    return float(np.clip((cost - rl) / (rw - rl), 0.0, 1.0)) if rw > rl else 1.0


def _daily(values: np.ndarray, days: np.ndarray, all_days: np.ndarray) -> np.ndarray:
    s = pd.Series(values).groupby(days).sum()
    return s.reindex(all_days, fill_value=0.0).to_numpy()


def run(ds: EventDataset, factories: Dict[str, Callable[[], object]], cfg: LabConfig = LabConfig(), *,
        scenario: str = "scenario", ledger: Optional[Ledger] = None,
        extra_pairs: Sequence = (), canary: Optional[BatteryResult] = None) -> LabResult:
    """Walk-forward comparison. `extra_pairs` adds direct (model, reference)
    paired tests, e.g. ("hgb", "hgb_noctx") to isolate a feature family.
    `canary` is passed through to the report card (a CALIBRATED pipeline
    battery is one of its PASS requirements)."""
    t0 = time.time()
    if cfg.baseline not in factories:
        raise ValueError("baseline %r missing from factories" % cfg.baseline)
    folds = walk_forward(ds.start, ds.end, n_folds=cfg.n_folds, embargo=cfg.embargo_ns, expanding=True,
                         min_train_groups=cfg.min_train_groups)
    n = len(ds)
    oof = np.zeros(n, dtype=bool)
    fold_of = np.full(n, -1, dtype=np.int64)
    for j, f in enumerate(folds):
        oof[f.test] = True
        fold_of[f.test] = j
    r_net = ds.r - cfg.cost_r
    runs: Dict[str, ModelRun] = {}
    for name, fac in factories.items():
        p = np.full(n, np.nan)
        take = np.zeros(n, dtype=bool)
        take_cf = np.zeros(n, dtype=bool)
        thr, fit_s = [], 0.0
        seen: List[np.ndarray] = []
        members: Optional[np.ndarray] = None
        cov_n = cov_hit = single_n = 0
        for j, f in enumerate(folds):
            tr, te = f.train, f.test
            m = fac()
            t1 = time.time()
            m.fit(ds.X[tr], ds.y[tr], start=ds.start[tr], end=ds.end[tr])
            fit_s += time.time() - t1
            p[te] = m.predict_proba(ds.X[te])
            try:
                mp = np.asarray(m.member_proba(ds.X[te]), dtype=float)
            except AttributeError:
                mp = None
            if mp is not None and mp.ndim == 2:
                if members is None:
                    members = np.full((n, mp.shape[1]), np.nan)
                if mp.shape[1] == members.shape[1]:
                    members[te] = mp
            past = np.concatenate(seen) if seen else np.arange(0)
            default = _breakeven(ds.y[tr], ds.r[tr], cfg.cost_r)
            t = _best_threshold(p[past], r_net[past], cfg.min_trades, default) if len(past) else default
            thr.append(t)
            take[te] = p[te] > t
            if len(past) >= 100 and len(np.unique(ds.y[past])) == 2:
                cf = SplitConformal(cfg.conformal_alpha).fit(p[past], ds.y[past])
                take_cf[te] = cf.decision(p[te]).astype(bool)
                sets = cf.sets(p[te])
                cov_hit += int(sets[np.arange(len(te)), ds.y[te].astype(int)].sum())
                single_n += int((sets.sum(axis=1) == 1).sum())
                cov_n += len(te)
            seen.append(te)
        runs[name] = ModelRun(name, p, take, take_cf, thr, fit_s, member_p=members)
        runs[name].metrics["_conformal"] = (cov_hit, single_n, cov_n)
    # Metrics on pooled OOF rows.
    y, days = ds.y[oof], ds.day[oof]
    all_days = np.unique(days)
    from sklearn.metrics import roc_auc_score
    for run_ in runs.values():
        ch, sn, cn = run_.metrics.pop("_conformal")
        p = run_.p[oof]
        tk = run_.take[oof]
        rn = r_net[oof]
        run_.metrics = {
            "auc": float(roc_auc_score(y, p)) if len(np.unique(y)) == 2 and np.ptp(p) > 0 else 0.5,
            "log_loss": float(_ll(y, p).mean()), "brier": float(np.mean((p - y) ** 2)),
            "ece": float(expected_calibration_error(y, p)), "n_trades": float(tk.sum()),
            "coverage": float(tk.mean()), "mean_net_r_per_trade": float(rn[tk].mean()) if tk.any() else 0.0,
            "total_net_r": float(rn[tk].sum()), "win_rate_taken": float(y[tk].mean()) if tk.any() else float("nan"),
            "fit_seconds": run_.fit_seconds}
        cf_rows = oof & np.isin(np.arange(n), np.concatenate([f.test for f in folds[1:]])) if len(folds) > 1 else oof
        tkc = run_.take_conformal[cf_rows]
        run_.metrics.update({"conformal_coverage": ch / cn if cn else float("nan"),
                             "conformal_singleton_share": sn / cn if cn else float("nan"),
                             "conformal_trades": float(tkc.sum()),
                             "conformal_mean_net_r": float(r_net[cf_rows][tkc].mean()) if tkc.any() else 0.0})
    base_mean = float(r_net[oof].mean())

    def compare(ref: str, names: List[str]) -> List[Dict]:
        rows = []
        rl = _ll(y, runs[ref].p[oof])
        rd = _daily(np.where(runs[ref].take[oof], r_net[oof], 0.0), days, all_days)
        for nm in names:
            ml = _ll(y, runs[nm].p[oof])
            gain_daily = _daily(rl - ml, days, all_days)
            dd = _daily(np.where(runs[nm].take[oof], r_net[oof], 0.0), days, all_days) - rd
            _, t_ll, p_ll = S.mean_test(gain_daily, lags=cfg.hac_lags)
            _, t_ll_w, p_ll_w = S.mean_test(-gain_daily, lags=cfg.hac_lags)
            _, t_d, p_d = S.mean_test(dd, lags=cfg.hac_lags) if np.ptp(dd) > 0 else (0.0, 0.0, 1.0)
            est, lo, hi = S.bootstrap_ci(dd, np.mean, stream(cfg.seed, "cmp", ref, nm), n_boot=500,
                                         mean_block=cfg.hac_lags) if np.ptp(dd) > 0 else (0.0, 0.0, 0.0)
            rows.append({"model": nm, "vs": ref, "logloss_gain_per_day": float(gain_daily.mean()),
                         "t_logloss": float(t_ll), "p_logloss": float(p_ll if np.isfinite(p_ll) else 1.0),
                         "p_logloss_worse": float(p_ll_w if np.isfinite(p_ll_w) else 1.0),
                         "daily_r_diff": float(est), "daily_r_diff_lo": float(lo), "daily_r_diff_hi": float(hi),
                         "t_decision": float(t_d), "p_decision": float(p_d if np.isfinite(p_d) else 1.0)})
        if rows:
            a1 = MT.adjust([r["p_logloss"] for r in rows], "holm")
            a2 = MT.adjust([r["p_decision"] for r in rows], "holm")
            a3 = MT.adjust([r["p_logloss_worse"] for r in rows], "holm")
            for r, x1, x2, x3 in zip(rows, a1, a2, a3):
                r["holm_p_logloss"], r["holm_p_decision"], r["holm_p_worse"] = float(x1), float(x2), float(x3)
                r["verdict"] = verdict(x1, x2, x3, cfg.alpha)
        return rows

    challengers = [k for k in runs if k not in (cfg.baseline, "prior")]
    comparisons = compare(cfg.baseline, challengers)
    vs_prior = compare("prior", [k for k in runs if k != "prior"]) if "prior" in runs else []
    pair_rows = []
    for nm, ref in extra_pairs:
        if nm in runs and ref in runs:
            pair_rows += compare(ref, [nm])
    if pair_rows:
        for r, a1, a2, a3 in zip(pair_rows, MT.adjust([r["p_logloss"] for r in pair_rows], "holm"),
                                 MT.adjust([r["p_decision"] for r in pair_rows], "holm"),
                                 MT.adjust([r["p_logloss_worse"] for r in pair_rows], "holm")):
            r["holm_p_logloss"], r["holm_p_decision"], r["holm_p_worse"] = float(a1), float(a2), float(a3)
            r["verdict"] = verdict(a1, a2, a3, cfg.alpha)
    by_prior = {r["model"]: r for r in vs_prior}
    for r in comparisons + pair_rows:
        if r["vs"] != "prior":
            skill_gate(r, by_prior.get(r["model"]) if "prior" in runs else None, cfg.alpha)
    # Decisions versus simply taking every candidate (the primary rule alone).
    all_daily = _daily(r_net[oof], days, all_days)
    vs_take_all = []
    for nm in runs:
        dd = _daily(np.where(runs[nm].take[oof], r_net[oof], 0.0), days, all_days) - all_daily
        _, t_d, p_d = S.mean_test(dd, lags=cfg.hac_lags) if np.ptp(dd) > 0 else (0.0, 0.0, 1.0)
        vs_take_all.append({"model": nm, "daily_r_diff": float(dd.mean()), "t": float(t_d),
                            "p": float(p_d if np.isfinite(p_d) else 1.0)})
    if vs_take_all:
        for r, a in zip(vs_take_all, MT.adjust([r["p"] for r in vs_take_all], "holm")):
            r["holm_p"] = float(a)
    family = "mllab:%s" % scenario
    trial_ids: Dict[str, str] = {}
    if ledger is not None:
        for nm, rr in runs.items():
            trial_ids[nm] = ledger.record_trial(TrialRecord(
                family_id=family, kind="baseline" if nm in (cfg.baseline, "prior") else "research",
                spec_digest=nm, data_digest=ds.digest, code_digest="research.mllab", seed=cfg.seed,
                metrics={k: v for k, v in rr.metrics.items() if isinstance(v, float) and np.isfinite(v)},
                split={"n_folds": cfg.n_folds, "embargo_ns": cfg.embargo_ns}))
    # Charge the card for every model EVER tried in this family, not just this call.
    n_trials = max(len(runs), ledger.family_trial_count(family)) if ledger is not None else len(runs)
    card = None
    best = max(challengers, key=lambda k: runs[k].metrics["total_net_r"]) if challengers else None
    if best is not None and runs[best].take[oof].sum() >= 10:
        tk = runs[best].take[oof]
        fam = np.column_stack([_daily(np.where(runs[k].take[oof], r_net[oof], 0.0), days, all_days)
                               for k in runs])
        card = gauntlet(candidate_id="%s:%s" % (scenario, best), family_id=family,
                        gross=ds.r[oof][tk], cost_per_obs=cfg.cost_r, n_trials=n_trials,
                        rng=stream(cfg.seed, "card", scenario), family_matrix=fam if fam.shape[0] >= 40 else None,
                        canary=canary, cfg=GauntletConfig(n_boot=300, hac_lags=cfg.hac_lags),
                        ledger=ledger, trial_ids=tuple(trial_ids.values()))
    sizes = [{"train": int(len(f.train)), "test": int(len(f.test)), "purged": int(f.purged),
              "embargoed": int(f.embargoed)} for f in folds]
    return LabResult(scenario, ds.digest, n, oof, runs, comparisons, vs_prior, card, sizes,
                     round(time.time() - t0, 1), {"vs_take_all": vs_take_all, "pairs": pair_rows,
                                                  "take_all_mean_net_r": base_mean,
                                                  "take_all_total_net_r": float(r_net[oof].sum()),
                                                  "oof_rows": int(oof.sum()), "best_challenger": best,
                                                  "n_trials_charged": int(n_trials),
                                                  "feature_names": list(ds.feature_names),
                                                  "dataset_meta": dict(ds.meta)},
                     fold_of=fold_of, trial_ids=trial_ids)


def summary_table(res: LabResult) -> str:
    L = ["| model | AUC | log loss | Brier | ECE | trades | mean net R / trade | total net R | conformal coverage | conformal trades (mean R) | fit s |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for nm, r in res.runs.items():
        m = r.metrics
        L.append("| %s | %.3f | %.4f | %.4f | %.3f | %d | %.3f | %.1f | %.2f | %d (%.3f) | %.1f |" % (
            nm, m["auc"], m["log_loss"], m["brier"], m["ece"], m["n_trades"], m["mean_net_r_per_trade"],
            m["total_net_r"], m["conformal_coverage"], m["conformal_trades"], m["conformal_mean_net_r"],
            m["fit_seconds"]))
    L.append("| take every candidate | — | — | — | — | %d | %.3f | %.1f | — | — | — |" % (
        res.meta["oof_rows"], res.meta["take_all_mean_net_r"], res.meta["take_all_total_net_r"]))
    return "\n".join(L)


def comparison_table(rows: List[Dict]) -> str:
    L = ["| challenger | vs | log-loss gain/day (Holm p) | daily net-R diff [95 % CI] (Holm p) | verdict |",
         "|---|---|---|---|---|"]
    for r in rows:
        L.append("| %s | %s | %+.4f (%.3f) | %+.3f [%+.3f, %+.3f] (%.3f) | **%s** |" % (
            r["model"], r["vs"], r["logloss_gain_per_day"], r["holm_p_logloss"], r["daily_r_diff"],
            r["daily_r_diff_lo"], r["daily_r_diff_hi"], r["holm_p_decision"],
            r["verdict"] + (" (raw %s, skill-gated)" % r["verdict_raw"]
                            if r.get("skill_gate", "").startswith("downgraded") else "")))
    return "\n".join(L)


__all__ = ["VERDICTS", "LabConfig", "ModelRun", "LabResult", "verdict", "skill_gate", "run", "summary_table",
           "comparison_table"]
