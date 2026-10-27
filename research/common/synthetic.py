"""research.common.synthetic — synthetic markets with KNOWN ground truth.

Why: every research lane must prove that its machinery (labels, statistics,
models, backtests, agents) *rejects noise* and *finds a planted effect of known
size* before it is pointed at real data. Real data can never provide that
ground truth.

What a ``SyntheticMarket`` contains:

* Observed OHLCV bars per symbol (``research.common.bars`` conventions),
  optionally corrupted like real feeds: unadjusted splits, missing bars,
  delistings (with pre-delisting underperformance, so survivorship bias is
  measurable).
* Ground truth per bar: market regime, the planted log-drift added to that
  bar, which planted effects triggered, the uncorrupted prices, and (if
  requested) the intrabar log-price path used to build high/low, so the true
  order of stop/target touches inside a bar is known.

Return model per bar (log returns, fat-tailed Student-t shocks):

    r_i = beta_i * m + s * sec_k(i) + idio_i + drift_i
    m, sec, idio scaled by the market regime's volatility multiplier

Planted effects are CAUSAL: a trigger is evaluated from information available
at the close of bar t (past closes and volumes only) and adds drift to bars
t+1 .. t+horizon. A strategy entering at the open of t+1 can capture it; a
model using only past data can learn it. Effect sizes are in units of each
symbol's per-bar volatility.

Nothing produced here describes real markets. It describes the machinery.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

from research.common.bars import BAR_COLUMNS, SESSION_OPEN_UTC, TF_MINUTES
from research.common.hashing import digest
from research.common.rng import stream

EFFECT_KINDS = (
    "drop_reversal",        # close-to-close return < -k*sigma          -> drift
    "rsi_oversold",         # RSI(period, SMA gain/loss) < level         -> drift
    "breakout",             # close > max(prior lookback closes) & volume ratio > v
    "pullback",             # EMA(fast) > EMA(slow) and close < EMA(fast)
    "quiet_drop_reversal",  # drop < -k*sigma AND volume ratio < v (interaction)
    "loud_drop_continuation",  # drop < -k*sigma AND volume ratio >= v
)


@dataclass(frozen=True)
class PlantedEffect:
    """A causal, observable-trigger effect with known size.

    strength: drift per bar, in units of the symbol's calm-regime per-bar
        sigma (sign gives direction: +0.15 means +0.15 sigma per bar for
        `horizon` bars).
    regime: if set, the trigger only fires while the market is in that regime.
    """
    name: str
    kind: str
    strength: float
    horizon: int = 5
    regime: Optional[str] = None
    k: float = 2.0                # drop threshold in sigmas
    level: float = 30.0           # RSI level
    period: int = 14              # RSI period
    lookback: int = 20            # breakout lookback
    vol_ratio: float = 1.0        # volume-ratio threshold
    fast: int = 20
    slow: int = 50

    def __post_init__(self):
        if self.kind not in EFFECT_KINDS:
            raise ValueError("unknown effect kind %r" % self.kind)
        if self.horizon < 1:
            raise ValueError("horizon must be >= 1")

    @property
    def warmup(self) -> int:
        return {"rsi_oversold": self.period + 1, "breakout": self.lookback + 1,
                "pullback": self.slow}.get(self.kind, 21)


@dataclass(frozen=True)
class RegimeSpec:
    names: Tuple[str, ...] = ("calm", "volatile")
    vol_mult: Tuple[float, ...] = (1.0, 2.0)
    market_drift: Tuple[float, ...] = (0.02, -0.02)   # market drift per bar, in market sigmas
    stay_prob: Tuple[float, ...] = (0.985, 0.95)      # persistence per session

    def __post_init__(self):
        n = len(self.names)
        if not (len(self.vol_mult) == len(self.market_drift) == len(self.stay_prob) == n) or n < 1:
            raise ValueError("RegimeSpec fields must have equal, non-zero length")


@dataclass(frozen=True)
class MarketSpec:
    n_symbols: int = 20
    n_sessions: int = 500
    timeframe: str = "1D"                      # "1D" or "15m"
    start: str = "2018-01-02"
    n_sectors: int = 4
    vol_annual: Tuple[float, float] = (0.20, 0.45)
    beta: Tuple[float, float] = (0.6, 1.4)
    sector_loading: float = 0.5
    market_vol_annual: float = 0.16
    tail_df: float = 5.0
    gap_share: float = 0.25                    # share of variance in the overnight gap
    regimes: RegimeSpec = field(default_factory=RegimeSpec)
    effects: Tuple[PlantedEffect, ...] = ()
    substeps: int = 12
    keep_paths: bool = False
    price0: Tuple[float, float] = (20.0, 200.0)
    volume0: Tuple[float, float] = (2e5, 5e6)
    delist_prob: float = 0.0
    delist_drift: float = -0.15                # sigma per bar over the last 40 bars before delisting
    missing_bar_prob: float = 0.0
    split_prob: float = 0.0
    seed: int = 7

    def __post_init__(self):
        if self.timeframe not in ("1D", "15m"):
            raise ValueError("timeframe must be '1D' or '15m'")
        if self.n_symbols < 1 or self.n_sessions < 1 or self.n_sectors < 1 or self.n_bars < 2:
            raise ValueError("need n_symbols >= 1, n_sectors >= 1 and at least 2 bars")
        if not 0.0 <= self.gap_share < 1.0:
            raise ValueError("gap_share must be in [0, 1)")
        if self.substeps < 2:
            raise ValueError("substeps must be >= 2")
        if len({e.name for e in self.effects}) != len(self.effects):
            raise ValueError("effect names must be unique")
        for e in self.effects:
            if e.regime is not None and e.regime not in self.regimes.names:
                raise ValueError("effect %r gated on unknown regime %r" % (e.name, e.regime))

    @property
    def bars_per_session(self) -> int:
        return 1 if self.timeframe == "1D" else 390 // TF_MINUTES[self.timeframe]

    @property
    def n_bars(self) -> int:
        return self.n_sessions * self.bars_per_session

    def bar_sigma_scale(self) -> float:
        """sqrt of the fraction of a trading year covered by one bar."""
        return float(np.sqrt(1.0 / (252.0 * self.bars_per_session)))

    def null(self) -> "MarketSpec":
        """Same market, no planted effects (a known-null control)."""
        return replace(self, effects=())

    def digest(self) -> str:
        return digest(self)


@dataclass
class SyntheticMarket:
    spec: MarketSpec
    spec_digest: str
    bars: Dict[str, pd.DataFrame]
    truth: Dict[str, pd.DataFrame]
    symbols: pd.DataFrame
    regimes: pd.DataFrame
    paths: Optional[Dict[str, np.ndarray]] = None

    def symbol_list(self):
        return list(self.symbols["symbol"])


# ─────────────────────────────────────────────────────────────────────

def _timestamps(spec: MarketSpec) -> pd.DatetimeIndex:
    days = pd.bdate_range(start=spec.start, periods=spec.n_sessions, tz="UTC")
    open_min = SESSION_OPEN_UTC[0] * 60 + SESSION_OPEN_UTC[1]
    if spec.timeframe == "1D":
        return days + pd.Timedelta(open_min + 390, unit="m")
    step = TF_MINUTES[spec.timeframe]
    offs = [pd.Timedelta(open_min + step * (j + 1), unit="m") for j in range(spec.bars_per_session)]
    return pd.DatetimeIndex([d + o for d in days for o in offs])


def _student(rng: np.random.Generator, df: float, size) -> np.ndarray:
    return rng.standard_t(df, size=size) * np.sqrt((df - 2.0) / df)


def _rsi_sma(logc_window: np.ndarray) -> np.ndarray:
    """RSI with simple-average gains/losses over the window's diffs (rows=symbols)."""
    p = np.exp(logc_window)
    d = np.diff(p, axis=1)
    gain = np.where(d > 0, d, 0.0).mean(axis=1)
    loss = np.where(d < 0, -d, 0.0).mean(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(loss > 0, gain / loss, np.inf)
    return np.where(np.isinf(rs), 100.0, 100.0 - 100.0 / (1.0 + rs))


def generate(spec: MarketSpec) -> SyntheticMarket:
    """Generate a synthetic market. Deterministic in `spec` (incl. its seed)."""
    S, T, B = spec.n_symbols, spec.n_bars, spec.bars_per_session
    seed = spec.seed
    r_par = stream(seed, "synthetic", "params")
    r_reg = stream(seed, "synthetic", "regime")
    r_mkt = stream(seed, "synthetic", "market")
    r_sec = stream(seed, "synthetic", "sector")
    r_idio = stream(seed, "synthetic", "idio")
    r_gap = stream(seed, "synthetic", "gap")
    r_path = stream(seed, "synthetic", "path")
    r_vol = stream(seed, "synthetic", "volume")
    r_cor = stream(seed, "synthetic", "corruption")

    scale = spec.bar_sigma_scale()
    sigma = r_par.uniform(*spec.vol_annual, size=S) * scale          # per-bar idio+total scale
    beta = r_par.uniform(*spec.beta, size=S)
    sector = np.arange(S) % spec.n_sectors
    r_par.shuffle(sector)
    price0 = np.exp(r_par.uniform(np.log(spec.price0[0]), np.log(spec.price0[1]), size=S))
    vol0 = np.exp(r_par.uniform(np.log(spec.volume0[0]), np.log(spec.volume0[1]), size=S)) / B
    m_sigma = spec.market_vol_annual * scale
    # Idiosyncratic sigma chosen so total per-bar sigma ~= sigma (calm regime).
    sys_var = (beta * m_sigma) ** 2 + (spec.sector_loading * m_sigma) ** 2
    idio_sigma = np.sqrt(np.maximum(sigma ** 2 - sys_var, (0.25 * sigma) ** 2))
    total_sigma = np.sqrt(sys_var + idio_sigma ** 2)

    # Delisting schedule (bar index after which the symbol stops trading).
    delist_at = np.full(S, -1, dtype=np.int64)
    if spec.delist_prob > 0:
        dead = r_par.random(S) < spec.delist_prob
        delist_at[dead] = r_par.integers(T // 3, T - 1, size=int(dead.sum()))

    # Market regimes (transitions at session starts).
    R = len(spec.regimes.names)
    reg_sess = np.zeros(spec.n_sessions, dtype=np.int64)
    for d in range(1, spec.n_sessions):
        prev = reg_sess[d - 1]
        if r_reg.random() < spec.regimes.stay_prob[prev] or R == 1:
            reg_sess[d] = prev
        else:
            others = [k for k in range(R) if k != prev]
            reg_sess[d] = others[int(r_reg.integers(len(others)))]
    regime = np.repeat(reg_sess, B)
    vmult = np.asarray(spec.regimes.vol_mult, dtype=float)[regime]
    mdrift = np.asarray(spec.regimes.market_drift, dtype=float)[regime] * m_sigma

    # Systematic shocks.
    mkt = mdrift + vmult * m_sigma * _student(r_mkt, spec.tail_df, T)
    sec = vmult[None, :] * m_sigma * _student(r_sec, spec.tail_df, (spec.n_sectors, T))
    idio = vmult[None, :] * idio_sigma[:, None] * _student(r_idio, spec.tail_df, (S, T))
    base_ret = beta[:, None] * mkt[None, :] + spec.sector_loading * sec[sector] + idio
    first_of_session = (np.arange(T) % B) == 0
    gap_frac = np.where(first_of_session, spec.gap_share, 0.0)
    # Split each bar's base return into an overnight gap and an intraday part.
    z_gap = r_gap.standard_normal((S, T))
    gap = np.where(first_of_session[None, :],
                   np.sqrt(gap_frac)[None, :] * vmult[None, :] * total_sigma[:, None] * z_gap, 0.0)
    intra_base = base_ret * np.sqrt(1.0 - gap_frac)[None, :]

    logc = np.zeros((S, T))
    logo = np.zeros((S, T))
    vol = np.zeros((S, T))
    drift = np.zeros((S, T))
    triggers = {e.name: np.zeros((S, T), dtype=np.int8) for e in spec.effects}
    remaining = {e.name: np.zeros(S, dtype=np.int64) for e in spec.effects}
    ema_f = {e.name: np.zeros(S) for e in spec.effects if e.kind == "pullback"}
    ema_s = {e.name: np.zeros(S) for e in spec.effects if e.kind == "pullback"}
    vol_noise = r_vol.standard_normal((S, T))
    intraday_shape = 1.0 + 0.6 * np.cos(np.linspace(0, 2 * np.pi, B)) if B > 1 else np.ones(1)

    prev_close = np.log(price0)
    regime_names = spec.regimes.names
    for t in range(T):
        d_t = np.zeros(S)
        for e in spec.effects:
            act = remaining[e.name] > 0
            d_t[act] += e.strength * total_sigma[act]
            remaining[e.name][act] -= 1
        # Pre-delisting underperformance.
        if spec.delist_prob > 0:
            near = (delist_at >= 0) & (delist_at - t < 40) & (delist_at >= t)
            d_t[near] += spec.delist_drift * total_sigma[near]
        drift[:, t] = d_t
        logo[:, t] = prev_close + gap[:, t]
        logc[:, t] = logo[:, t] + intra_base[:, t] + d_t
        r_cc = logc[:, t] - prev_close
        vr = np.abs(r_cc) / total_sigma
        vol[:, t] = vol0 * intraday_shape[t % B] * np.exp(0.35 * vol_noise[:, t]) \
            * (1.0 + 0.5 * vr) * np.sqrt(vmult[t])
        prev_close = logc[:, t]

        # Effect triggers at the close of bar t (causal: data up to t only).
        reg_name = regime_names[regime[t]]
        for e in spec.effects:
            if e.kind == "pullback":
                a_f, a_s = 2.0 / (e.fast + 1), 2.0 / (e.slow + 1)
                if t == 0:
                    ema_f[e.name][:] = logc[:, 0]
                    ema_s[e.name][:] = logc[:, 0]
                else:
                    ema_f[e.name] += a_f * (logc[:, t] - ema_f[e.name])
                    ema_s[e.name] += a_s * (logc[:, t] - ema_s[e.name])
            if t < e.warmup or (e.regime is not None and reg_name != e.regime):
                continue
            if e.kind in ("drop_reversal", "quiet_drop_reversal", "loud_drop_continuation"):
                hit = r_cc < -e.k * total_sigma
                if e.kind != "drop_reversal":
                    vratio = vol[:, t] / np.maximum(vol[:, t - 20:t].mean(axis=1), 1e-12)
                    hit &= (vratio < e.vol_ratio) if e.kind == "quiet_drop_reversal" else (vratio >= e.vol_ratio)
            elif e.kind == "rsi_oversold":
                hit = _rsi_sma(logc[:, t - e.period:t + 1]) < e.level
            elif e.kind == "breakout":
                vratio = vol[:, t] / np.maximum(vol[:, t - 20:t].mean(axis=1), 1e-12)
                hit = (logc[:, t] > logc[:, t - e.lookback:t].max(axis=1)) & (vratio > e.vol_ratio)
            else:  # pullback
                hit = (ema_f[e.name] > ema_s[e.name]) & (logc[:, t] < ema_f[e.name])
            if spec.delist_prob > 0:
                hit &= (delist_at < 0) | (t < delist_at)
            triggers[e.name][hit, t] = 1
            remaining[e.name][hit] = e.horizon

    # Intrabar paths: Brownian bridge from open to close (log space).
    m = spec.substeps
    bar_sig = vmult[None, :] * total_sigma[:, None] * np.sqrt(1.0 - gap_frac)[None, :]
    keep = spec.keep_paths
    paths = {} if keep else None
    hi = np.empty((S, T))
    lo = np.empty((S, T))
    for i in range(S):
        w = np.cumsum(r_path.standard_normal((T, m)), axis=1) / np.sqrt(m)
        k = np.arange(1, m + 1) / m
        bridge = w - k[None, :] * w[:, -1:]
        lin = logo[i][:, None] + k[None, :] * (logc[i] - logo[i])[:, None]
        p = lin + bar_sig[i][:, None] * bridge
        p[:, -1] = logc[i]
        full = np.concatenate([logo[i][:, None], p], axis=1)
        hi[i] = full.max(axis=1)
        lo[i] = full.min(axis=1)
        if keep:
            paths["S%03d" % i] = full          # same naming as the bars dict below

    # Assemble observed bars (with optional corruption) and truth.
    ts = _timestamps(spec)
    names = ["S%03d" % i for i in range(S)]
    bars: Dict[str, pd.DataFrame] = {}
    truth: Dict[str, pd.DataFrame] = {}
    split_at = np.full(S, -1, dtype=np.int64)
    split_ratio = np.ones(S)
    if spec.split_prob > 0:
        sp = r_cor.random(S) < spec.split_prob
        split_at[sp] = r_cor.integers(T // 4, T - T // 4, size=int(sp.sum()))
        split_ratio[sp] = r_cor.choice([2.0, 3.0, 4.0], size=int(sp.sum()))
    for i, sym in enumerate(names):
        o, h, l, c = np.exp(logo[i]), np.exp(hi[i]), np.exp(lo[i]), np.exp(logc[i])
        v = np.round(vol[i])
        factor = np.ones(T)
        if split_at[i] >= 0:
            factor[split_at[i]:] = 1.0 / split_ratio[i]
        observed = np.ones(T, dtype=bool)
        if spec.missing_bar_prob > 0:
            observed &= r_cor.random(T) >= spec.missing_bar_prob
        if delist_at[i] >= 0:
            observed[delist_at[i] + 1:] = False
        tr = pd.DataFrame({
            "ts_utc": ts,
            "regime": np.asarray(regime_names, dtype=object)[regime],
            "planted_drift": drift[i],
            "true_open": o, "true_high": h, "true_low": l, "true_close": c,
            "split_factor": factor,
            "observed": observed,
        })
        for e in spec.effects:
            tr["trigger_" + e.name] = triggers[e.name][i]
        truth[sym] = tr
        df = pd.DataFrame({
            "ts_utc": ts,
            "open": o * factor, "high": h * factor, "low": l * factor, "close": c * factor,
            "volume": v / factor,
            "quality_flags": np.zeros(T, dtype=np.int64),
        })
        bars[sym] = df.loc[observed, list(BAR_COLUMNS)].reset_index(drop=True)
    symbols = pd.DataFrame({
        "symbol": names, "sector": ["SEC%d" % s for s in sector], "beta": beta,
        "sigma_bar": total_sigma, "price0": price0,
        "delisted_at": [ts[j] if j >= 0 else pd.NaT for j in delist_at],
        "split_at": [ts[j] if j >= 0 else pd.NaT for j in split_at],
        "split_ratio": split_ratio,
    })
    regimes = pd.DataFrame({"ts_utc": ts, "regime": np.asarray(regime_names, dtype=object)[regime]})
    return SyntheticMarket(spec=spec, spec_digest=spec.digest(), bars=bars, truth=truth,
                           symbols=symbols, regimes=regimes, paths=paths)


__all__ = ["EFFECT_KINDS", "PlantedEffect", "RegimeSpec", "MarketSpec",
           "SyntheticMarket", "generate"]
