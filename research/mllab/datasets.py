"""research.mllab.datasets — leakage-safe event datasets from bars.

Features: the M18 single-symbol feature groups (``bot.ml.features``
SAFE_FEATURE_GROUPS_V2) minus price-level columns that are not comparable
across symbols, plus causal cross-sectional MARKET CONTEXT built from the
panel at each timestamp (equal-weight market return and volatility, its
expanding percentile, breadth above EMA50, dispersion). Missing values go
through the M18 missingness policy (neutral fill + indicator columns).

Labels: ``research.mllab.labels.barrier_outcomes`` (ambiguous / pending rows
dropped). Every row carries its decision time (``start``) and label
resolution time (``end``) so the integrity-lane splits can purge overlap.

``audit_feature_asof`` perturbs FUTURE bars and proves that features at or
before a probe time do not change (feature-as-of audit, canonical B14).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from bot.ml.features import SAFE_FEATURE_GROUPS_V2
from bot.ml.features.missingness import apply_missingness_fill
from research.common.hashing import array_digest, digest
from research.common.bars import TF_MINUTES
from research.mllab.labels import BarrierSpec, barrier_outcomes, path_observability, unobserved_paths

LEVEL_FEATURES = ("price_return.close", "vol_regime.atr_14_sma_true_range", "momentum.macd_line",
                  "momentum.macd_signal", "momentum.macd_hist", "volume_liquidity.dollar_vol_20")
CTX_FEATURES = ("ctx.mkt_ret_1", "ctx.mkt_ret_5", "ctx.mkt_vol_20", "ctx.mkt_vol_pct", "ctx.breadth50",
                "ctx.dispersion")
ATR_COL = "vol_regime.atr_14_sma_true_range"
DATASET_SCHEMA = "mllab-eventdataset-v2"

Sampler = Callable[[pd.DataFrame, pd.DataFrame], np.ndarray]


def every_k(k: int = 1, warmup: int = 60) -> Sampler:
    def f(bars, feats):
        m = np.zeros(len(bars), dtype=bool)
        m[warmup::k] = True
        return m
    f.__name__ = "every_%d" % k
    return f


def drop_rule(k_sigma: float = 1.5, warmup: int = 60) -> Sampler:
    """Primary rule for meta-labeling: one-bar return below -k * realized vol."""
    def f(bars, feats):
        r = feats["price_return.log_ret_1"].to_numpy()
        rv = feats["vol_regime.realized_vol_20"].to_numpy()
        m = np.isfinite(r) & np.isfinite(rv) & (r < -k_sigma * rv)
        m[:warmup] = False
        return m
    f.__name__ = "drop_%.2f" % k_sigma
    return f


def oversold_rule(level: float = 40.0, warmup: int = 60) -> Sampler:
    """Primary rule: RSI(14, live-parity SMA gain/loss) below `level`."""
    def f(bars, feats):
        r = feats["momentum.rsi_14_sma_gain_loss"].to_numpy()
        m = np.isfinite(r) & (r < level)
        m[:warmup] = False
        return m
    f.__name__ = "rsi_below_%g" % level
    return f


@dataclass
class EventDataset:
    X: np.ndarray
    feature_names: List[str]
    y: np.ndarray
    r: np.ndarray
    start: np.ndarray            # int64 ns, decision time
    end: np.ndarray              # int64 ns, label resolution time
    symbol: np.ndarray
    day: np.ndarray
    truth_regime: np.ndarray     # diagnostics only, never a feature
    meta: Dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.y)

    def subset(self, idx) -> "EventDataset":
        idx = np.asarray(idx)
        return EventDataset(self.X[idx], self.feature_names, self.y[idx], self.r[idx], self.start[idx],
                            self.end[idx], self.symbol[idx], self.day[idx], self.truth_regime[idx], dict(self.meta))

    @property
    def digest(self) -> str:
        """Identity of EVERYTHING that can change training, purging or the statistics (second review, P1):
        features, labels, outcomes, decision AND resolution times (purging), symbols and days (daily paired
        tests), the diagnostic regime column, and the metadata (barrier spec, sampler, scenario, source
        snapshot), under an explicit schema version."""
        return digest({"schema": DATASET_SCHEMA, "X": array_digest(self.X), "y": array_digest(self.y),
                       "r": array_digest(self.r), "start": array_digest(np.asarray(self.start, dtype=np.int64)),
                       "end": array_digest(np.asarray(self.end, dtype=np.int64)),
                       "symbol": [str(x) for x in self.symbol], "day": [str(x) for x in self.day],
                       "truth_regime": [str(x) for x in self.truth_regime], "features": list(self.feature_names),
                       "meta": self.meta})


def symbol_features(bars: pd.DataFrame) -> pd.DataFrame:
    parts = [mod.compute(bars) for mod in SAFE_FEATURE_GROUPS_V2.values()]
    f = pd.concat(parts, axis=1)
    f["momentum.macd_hist_over_close"] = f["momentum.macd_hist"] / bars["close"].astype(float).to_numpy()
    return f


def market_context(per_symbol: Dict[str, pd.DataFrame], bars: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Cross-sectional context indexed by ts (all values known at that close)."""
    rets, above, idx = [], [], []
    for s, f in per_symbol.items():
        ts = pd.to_datetime(bars[s]["ts_utc"], utc=True)
        rets.append(pd.Series(f["price_return.log_ret_1"].to_numpy(), index=ts, name=s))
        above.append(pd.Series((f["trend.ema_distance_50"].to_numpy() > 0).astype(float), index=ts, name=s))
    R = pd.concat(rets, axis=1).sort_index()
    A = pd.concat(above, axis=1).reindex(R.index)
    mkt = R.mean(axis=1)
    vol = mkt.rolling(20, min_periods=20).std()
    pct = vol.expanding(min_periods=60).rank(pct=True)
    ctx = pd.DataFrame({"ctx.mkt_ret_1": mkt, "ctx.mkt_ret_5": mkt.rolling(5, min_periods=5).sum(),
                        "ctx.mkt_vol_20": vol, "ctx.mkt_vol_pct": pct, "ctx.breadth50": A.mean(axis=1),
                        "ctx.dispersion": R.std(axis=1)})
    return ctx


def build_dataset(bars: Dict[str, pd.DataFrame], *, spec: BarrierSpec = BarrierSpec(),
                  sampler: Sampler = every_k(2), truth: Optional[Dict[str, pd.DataFrame]] = None,
                  include_context: bool = True, extra_meta: Optional[Dict] = None, timeframe: str = "1D",
                  calendar=None) -> EventDataset:
    """Labels whose barrier path crosses a missing expected bar or an invalid / blocked bar are dropped
    (comprehensive audit, PR #7): an unobserved path is never a definitive training or evaluation outcome."""
    per = {s: symbol_features(b) for s, b in bars.items()}
    ctx = market_context(per, bars) if include_context else None
    rows_X, ys, rs, st, en, sy, dy, tr = [], [], [], [], [], [], [], []
    n_unobserved = 0
    names: Optional[List[str]] = None
    for s, b in bars.items():
        f = per[s].drop(columns=[c for c in LEVEL_FEATURES if c in per[s].columns])
        ts = pd.to_datetime(b["ts_utc"], utc=True)
        if ctx is not None:
            f = pd.concat([f.reset_index(drop=True), ctx.reindex(ts).reset_index(drop=True)], axis=1)
        out = barrier_outcomes(b["open"], b["high"], b["low"], b["close"], per[s][ATR_COL], spec)
        obs = path_observability(b["ts_utc"], flags=b["quality_flags"] if "quality_flags" in b.columns else None,
                                 ohlc=(b["open"], b["high"], b["low"], b["close"]), timeframe=timeframe,
                                 tf_minutes=TF_MINUTES.get(timeframe, 390), calendar=calendar)
        unobs = unobserved_paths(out, obs)
        cand = sampler(b, per[s]) & np.isfinite(out["y"]) & ~out["ambiguous"] & ~out["pending"]
        n_unobserved += int((cand & unobs).sum())
        keep = cand & ~unobs
        idx = np.flatnonzero(keep)
        if not len(idx):
            continue
        names = list(f.columns)
        rows_X.append(f.iloc[idx].to_numpy(dtype=float))
        ys.append(out["y"][idx])
        rs.append(out["r"][idx])
        tsn = ts.to_numpy(dtype="datetime64[ns]").astype(np.int64)
        st.append(tsn[idx])
        en.append(tsn[out["resolve_idx"][idx]])
        sy.append(np.full(len(idx), s, dtype=object))
        dy.append(ts.dt.floor("D").astype(str).to_numpy()[idx])
        if truth is not None and s in truth and "regime" in truth[s]:
            treg = truth[s].set_index(pd.to_datetime(truth[s]["ts_utc"], utc=True))["regime"].reindex(ts).to_numpy()
            tr.append(treg[idx].astype(object))
        else:
            tr.append(np.full(len(idx), "", dtype=object))
    if names is None:
        raise ValueError("no labelled events")
    X = np.vstack(rows_X)
    order = np.argsort(np.concatenate(st), kind="mergesort")
    Xf, ind, ind_names = _fill(X, names)
    if ind.shape[1]:
        Xf = np.column_stack([Xf, ind])
    feat_names = names + list(ind_names)
    ds = EventDataset(Xf[order], feat_names, np.concatenate(ys)[order], np.concatenate(rs)[order],
                      np.concatenate(st)[order], np.concatenate(en)[order], np.concatenate(sy)[order],
                      np.concatenate(dy)[order], np.concatenate(tr)[order],
                      {"barrier": spec.__dict__, "sampler": getattr(sampler, "__name__", "custom"),
                       "n_symbols": len(bars), "unobserved_path_labels_excluded": n_unobserved,
                       "observability": "calendar" if calendar is not None else "business-day/contiguous (no calendar)",
                       **(extra_meta or {})})
    if not np.isfinite(ds.X).all():
        raise ValueError("non-finite values reached the feature matrix")
    return ds


_KNOWN_GROUPS = ("price_return", "trend", "momentum", "vol_regime", "volume_liquidity")


def _fill(X: np.ndarray, names: Sequence[str]):
    """Missingness policy for M18 + context columns: M18 policy where it
    applies, neutral 0 fill + indicator for the research context columns."""
    m18 = [i for i, n in enumerate(names) if n.split(".")[0] in _KNOWN_GROUPS]
    other = [i for i, n in enumerate(names) if i not in set(m18)]
    Xa, ind_a, names_a = apply_missingness_fill(X[:, m18], [names[i] for i in m18])
    Xo = X[:, other].copy()
    miss = ~np.isfinite(Xo)
    Xo[miss] = 0.0
    keep = miss.any(axis=0)
    ind_o = miss[:, keep].astype(float)
    names_o = ["%s__was_missing" % names[other[k]] for k in np.flatnonzero(keep)]
    out = np.empty_like(X)
    out[:, m18] = Xa
    out[:, other] = Xo
    return out, np.column_stack([ind_a, ind_o]) if ind_o.size else ind_a, list(names_a) + names_o


def audit_feature_asof(bars: Dict[str, pd.DataFrame], *, probes: int = 3, seed: int = 0,
                       feature_fn: Callable[[pd.DataFrame], pd.DataFrame] = None) -> Dict[str, float]:
    """Perturb all bars AFTER a probe time and verify that every feature row at
    or before the probe is unchanged. Returns counts; `violations` must be 0.

    `feature_fn` defaults to ``symbol_features``; passing a deliberately
    leaky function is the audit's own negative control (it must flag it)."""
    from research.common.rng import stream
    feature_fn = feature_fn or symbol_features
    rng = stream(seed, "mllab", "asof")
    any_sym = next(iter(bars))
    ts_all = pd.to_datetime(bars[any_sym]["ts_utc"], utc=True)
    base_per = {s: feature_fn(b) for s, b in bars.items()}
    base_ctx = market_context(base_per, bars)
    checked = violations = 0
    for _ in range(probes):
        t = ts_all.iloc[int(rng.integers(len(ts_all) // 3, len(ts_all) - 5))]
        pert = {}
        for s, b in bars.items():
            b2 = b.copy()
            fut = pd.to_datetime(b2["ts_utc"], utc=True) > t
            k = rng.uniform(0.5, 1.5, size=int(fut.sum()))
            for col in ("open", "high", "low", "close"):
                b2.loc[fut, col] = b2.loc[fut, col].to_numpy() * k
            b2.loc[fut, "volume"] = b2.loc[fut, "volume"].to_numpy() * rng.uniform(0.2, 5.0, size=int(fut.sum()))
            pert[s] = b2
        per2 = {s: feature_fn(b) for s, b in pert.items()}
        ctx2 = market_context(per2, pert)
        for s in bars:
            past = (pd.to_datetime(bars[s]["ts_utc"], utc=True) <= t).to_numpy()
            a, b_ = base_per[s].to_numpy()[past], per2[s].to_numpy()[past]
            checked += a.size
            violations += int((~np.isclose(a, b_, equal_nan=True, rtol=1e-12, atol=1e-12)).sum())
        cp = base_ctx.index <= t
        a, b_ = base_ctx.to_numpy()[cp], ctx2.to_numpy()[cp]
        checked += a.size
        violations += int((~np.isclose(a, b_, equal_nan=True, rtol=1e-12, atol=1e-12)).sum())
    return {"cells_checked": float(checked), "violations": float(violations), "probes": float(probes)}


__all__ = ["LEVEL_FEATURES", "CTX_FEATURES", "EventDataset", "every_k", "drop_rule", "oversold_rule", "symbol_features",
           "market_context", "build_dataset", "audit_feature_asof"]
