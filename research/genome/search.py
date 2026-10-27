"""research.genome.search — ledgered parameter search over surfaces (canonical B11 foundations).

The goal is the shape of the performance SURFACE, not the best isolated
backtest:

* every evaluated genome is a ledgered trial (``research.integrity``), so the
  multiplicity charge is never forgotten;
* ``plateau`` scores each grid point by the median of itself and its one-step
  neighbours. A lone spike on a flat or negative surface is luck; a broad
  plateau is the minimum bar for robustness;
* ``select`` reports the best isolated point, the best plateau point and the
  isolated point's Deflated Sharpe Ratio. The DSR is charged for every trial
  in the ledger family, with the cross-trial Sharpe variance measured from
  the trials themselves;
* ``baselines`` gives the no-trade and equal-weight buy-and-hold references
  every genome must be compared with.

Provenance (second review, PR #10): every trial records the content hash of
the code that produced it (``CODE_PATHS``), the data snapshot digest, the
research seed of the market or manifest it ran on, and outcome artifacts
(digests of the trade R multiples and of the per-period return series) that
the report card can verify.
"""
from __future__ import annotations

import itertools
import math
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from research.common.hashing import digest
from research.genome.engine import EngineConfig, EngineResult, metrics, run_genome
from research.genome.schema import Genome, GenomeError, seed, with_params
from research.integrity import stats as S
from research.integrity.ledger import Ledger, TrialRecord
from research.integrity.provenance import code_identity, outcome_digest

CODE_PATHS = ("research/genome", "research/integrity", "research/common", "bot/backtesting")


def grid(template: str, axes: Mapping[str, Sequence], base: Optional[Genome] = None) -> Tuple[List[Genome], int]:
    """All valid genomes on the grid, plus the count of invalid combinations skipped."""
    g0 = base or seed(template)
    names = sorted(axes)
    out, bad = [], 0
    for combo in itertools.product(*[axes[n] for n in names]):
        try:
            out.append(with_params(g0, origin="grid", **dict(zip(names, combo))))
        except GenomeError:
            bad += 1
    return out, bad


def evaluate(genomes: Sequence[Genome], bars: Mapping[str, pd.DataFrame], *, ledger: Ledger, family_id: str,
             data_digest: str, seed: int, cfg: EngineConfig = EngineConfig(),
             membership: Optional[Mapping[str, np.ndarray]] = None, keep_equity: bool = False) -> pd.DataFrame:
    """``seed`` is the research seed of the market (synthetic generator seed or the manifest's seed); it is
    required so no trial is recorded with a placeholder. ``membership`` is the as-of universe mask passed to
    the engine (point-in-time eligibility); a search must use the same mask as the run it is compared with."""
    if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)):
        raise ValueError("seed must be the integer research seed of the data, got %r" % (seed,))
    code_id = code_identity(CODE_PATHS)
    rows = []
    for g in genomes:
        res = run_genome(g, bars, cfg, membership)
        m = metrics(res)
        r = res.equity.pct_change().dropna().to_numpy()
        m["sr_per_period"] = float(S.sharpe(r)) if len(r) > 2 and r.std() > 0 else 0.0
        rm = np.array([t.r_multiple for t in res.trades if np.isfinite(t.r_multiple)], dtype=float)
        tid = ledger.record_trial(TrialRecord(
            family_id=family_id, kind="research", spec_digest=g.genome_id, data_digest=data_digest,
            code_digest=code_id, seed=int(seed), metrics={k: v for k, v in m.items() if np.isfinite(v)},
            config={"genome": g.to_dict(), "engine": dict(res.config), "decisions": res.decision_counts()},
            artifacts={"gross": outcome_digest(rm), "family_series": outcome_digest(r)}))
        row = {"genome_id": g.genome_id, "trial_id": tid, **g.p, **m}
        if keep_equity:
            row["_returns"] = r
        rows.append(row)
    return pd.DataFrame(rows)


def plateau(df: pd.DataFrame, axes: Mapping[str, Sequence], metric: str) -> pd.Series:
    """Median of `metric` over each point and its one-step grid neighbours."""
    names = sorted(axes)
    pos = {n: {v: i for i, v in enumerate(axes[n])} for n in names}
    key = {tuple(pos[n][r[n]] for n in names): r[metric] for _, r in df.iterrows()}
    out = []
    for _, r in df.iterrows():
        c = tuple(pos[n][r[n]] for n in names)
        vals = [key[c]]
        for d in range(len(names)):
            for step in (-1, 1):
                nb = list(c)
                nb[d] += step
                if tuple(nb) in key:
                    vals.append(key[tuple(nb)])
        out.append(float(np.median(vals)))
    return pd.Series(out, index=df.index, name="plateau_%s" % metric)


def select(df: pd.DataFrame, axes: Mapping[str, Sequence], *, ledger: Ledger, family_id: str,
           metric: str = "sharpe_ann") -> Dict:
    pl = plateau(df, axes, metric)
    i_best = int(df[metric].astype(float).idxmax())
    i_rob = int(pl.idxmax())
    n_trials = ledger.family_trial_count(family_id)
    srv = float(np.var(df["sr_per_period"].astype(float), ddof=1)) if len(df) > 1 else None
    dsr = None
    if "_returns" in df.columns:
        d = S.deflated_sharpe_ratio(df.loc[i_best, "_returns"], n_trials, srv if srv and srv > 0 else None)
        dsr = {"sr_per_period": d.sr, "sr0": d.sr0, "dsr": d.dsr, "psr0": d.psr0, "n_obs": d.n_obs,
               "n_trials": d.n_trials}
    return {"n_trials_family": n_trials, "n_points": int(len(df)),
            "best_isolated": {"genome_id": df.loc[i_best, "genome_id"], metric: float(df.loc[i_best, metric]),
                              "plateau": float(pl.loc[i_best])},
            "best_plateau": {"genome_id": df.loc[i_rob, "genome_id"], metric: float(df.loc[i_rob, metric]),
                             "plateau": float(pl.loc[i_rob])},
            "share_positive": float((df[metric] > 0).mean()), "median": float(df[metric].median()),
            "dsr_best_isolated": dsr}


def baselines(bars: Mapping[str, pd.DataFrame], cfg: EngineConfig = EngineConfig()) -> Dict[str, Dict]:
    """No-trade and equal-weight buy-and-hold (entry at each symbol's first open, fees and slippage paid,
    held to its last close)."""
    syms = sorted(bars)
    timeline = pd.DatetimeIndex(sorted(set().union(*[set(pd.to_datetime(bars[s]["ts_utc"], utc=True))
                                                       for s in syms])))
    fee, slip = cfg.fee_bps / 1e4, cfg.slippage_bps / 1e4
    alloc = cfg.initial_equity / len(syms)
    total = pd.Series(0.0, index=timeline)
    for s in syms:
        b = bars[s]
        ts = pd.to_datetime(b["ts_utc"], utc=True)
        fill = float(b["open"].iloc[0]) * (1 + slip)
        qty = alloc / (fill * (1 + fee))
        val = pd.Series(qty * b["close"].to_numpy(float), index=ts)
        final = val.iloc[-1] * (1 - fee)
        val = val.reindex(timeline).ffill().fillna(alloc)
        val[timeline >= ts.iloc[-1]] = final          # exit fee at the symbol's last close
        total = total + val
    r = total.pct_change().dropna().to_numpy()
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    bh = {"total_return": float(total.iloc[-1] / cfg.initial_equity - 1.0),
          "sharpe_ann": float(r.mean() / sd * math.sqrt(252)) if sd > 0 else 0.0,
          "max_drawdown": float((total / total.cummax() - 1.0).min())}
    return {"no_trade": {"total_return": 0.0, "sharpe_ann": 0.0, "max_drawdown": 0.0},
            "buy_and_hold_equal_weight": bh}


def data_digest(bars: Mapping[str, pd.DataFrame]) -> str:
    return digest({s: digest(b[["ts_utc", "open", "high", "low", "close", "volume"]].astype(str).values.tolist())
                   for s, b in sorted(bars.items())})


__all__ = ["CODE_PATHS", "grid", "evaluate", "plateau", "select", "baselines", "data_digest"]
