"""research.genome.engine — multi-symbol, portfolio-aware event backtester (canonical B10).

Execution semantics are M17's (``bot.backtesting.execution``), extended from
one symbol to a portfolio:

* A signal decided at a bar's close executes at that symbol's NEXT bar open.
  A missing bar delays it; it never fills on a bar that does not exist.
* Entry fill = open × (1 + slippage). Long only.
* Exits at the open come first (template signal, time stop), then entries.
* Sizing is M17's fixed-risk rule:
  `floor(min(equity × risk / (fill − stop), equity × max_position / fill))`.
  On top of that are a gross-exposure cap, a position-count cap and a cash
  check that reserves the entry fee. Candidates competing for scarce
  capacity are ranked by signal score, then symbol name, so the outcome is
  deterministic.
* Stops and targets are checked intrabar from the entry bar on.
  - A bar that touches both is resolved pessimistically: stop first.
  - A gap through a level fills at the open.
  - Slippage worsens every non-EOD fill.
* A trailing stop ratchets up at each close and applies from the next bar.
  A time stop exits at the open after `time_stop_bars` bars held.
* A symbol that stops trading (delisting) is closed at its last close; the
  result flags it. Positions still open at the end close at the last close
  with no slippage, as in M17's mark-to-close EOD exit.
* Optional as-of membership masks forbid entries in symbols that were not in
  the universe at signal time (survivorship control).

``m17_compat=True`` with percentage stops reproduces ``bot.backtesting``
trade-for-trade on a single symbol. ``research.genome.parity`` verifies this.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Mapping, Optional

import numpy as np
import pandas as pd

from research.common.hashing import digest
from research.genome.schema import ExitSpec, Genome, RiskSpec
from research.genome.templates import signals as template_signals

SKIP_REASONS = ("max_positions", "gross_exposure", "insufficient_cash", "zero_size", "atr_unavailable",
                "stop_above_fill", "not_in_universe", "reentry_same_bar")


@dataclass(frozen=True)
class EngineConfig:
    initial_equity: float = 100_000.0
    fee_bps: float = 5.0
    slippage_bps: float = 5.0
    max_positions: int = 10
    max_gross_exposure: float = 1.0
    stop_mode: str = "atr"                 # "atr" (genome exits) | "pct" (M17 parity)
    stop_pct: Optional[float] = None
    target_pct: Optional[float] = None
    m17_compat: bool = False               # no time stop / trailing: pure M17 semantics

    def __post_init__(self):
        if self.stop_mode not in ("atr", "pct"):
            raise ValueError("stop_mode must be 'atr' or 'pct'")
        if self.max_positions < 1 or not self.max_gross_exposure > 0:
            raise ValueError("max_positions >= 1 and max_gross_exposure > 0 required")


@dataclass(frozen=True)
class Trade:
    symbol: str
    qty: int
    entry_ts: pd.Timestamp
    entry_price: float
    exit_ts: pd.Timestamp
    exit_price: float
    exit_reason: str                       # signal | stop_loss | take_profit | time | delisted | eod
    fees_paid: float
    slippage_paid: float
    pnl_absolute: float
    pnl_pct: float
    bars_held: int
    r_multiple: float


@dataclass
class _Pos:
    symbol: str
    qty: int
    entry_price: float
    stop: Optional[float]
    target: Optional[float]
    entry_row: int
    entry_ts: pd.Timestamp
    fee: float
    slip: float
    atr: float
    risk_per_share: float
    highest_close: float


@dataclass
class EngineResult:
    trades: List[Trade]
    equity: pd.Series
    skipped: Dict[str, int]
    signals_seen: int
    config: Dict
    genome_id: str = ""
    meta: Dict = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return digest({"trades": [asdict(t) for t in self.trades], "equity": [float(x) for x in self.equity.values],
                       "skipped": self.skipped})


def run(bars: Mapping[str, pd.DataFrame], sigs: Mapping[str, pd.DataFrame], exits: ExitSpec, risk: RiskSpec,
        cfg: EngineConfig = EngineConfig(), membership: Optional[Mapping[str, np.ndarray]] = None,
        genome_id: str = "") -> EngineResult:
    syms = sorted(bars)
    fee, slip = cfg.fee_bps / 1e4, cfg.slippage_bps / 1e4
    ts_of = {s: pd.to_datetime(bars[s]["ts_utc"], utc=True).reset_index(drop=True) for s in syms}
    for s in syms:
        if len(sigs[s]) != len(bars[s]):
            raise ValueError("signals for %s are not aligned with its bars" % s)
        if not ts_of[s].is_monotonic_increasing or ts_of[s].duplicated().any():
            raise ValueError("bars for %s must be strictly increasing in time" % s)
    O = {s: bars[s]["open"].to_numpy(float) for s in syms}
    H = {s: bars[s]["high"].to_numpy(float) for s in syms}
    L = {s: bars[s]["low"].to_numpy(float) for s in syms}
    Cl = {s: bars[s]["close"].to_numpy(float) for s in syms}
    EN = {s: sigs[s]["entry"].to_numpy(bool) for s in syms}
    EX = {s: sigs[s]["exit"].to_numpy(bool) for s in syms}
    AT = {s: sigs[s]["atr"].to_numpy(float) for s in syms}
    SC = {s: np.nan_to_num(sigs[s]["score"].to_numpy(float)) for s in syms}
    timeline = pd.DatetimeIndex(sorted(set().union(*[set(ts_of[s]) for s in syms])))
    row_at = {s: dict(zip(ts_of[s], range(len(ts_of[s])))) for s in syms}
    last_row = {s: len(ts_of[s]) - 1 for s in syms}
    cash = float(cfg.initial_equity)
    pos: Dict[str, _Pos] = {}
    last_close: Dict[str, float] = {}
    trades: List[Trade] = []
    skipped = {k: 0 for k in SKIP_REASONS}
    eq_vals = np.empty(len(timeline))
    seen = 0
    min_cash = cash

    def close(s, px, ts, reason, row, slip_per_share):
        nonlocal cash
        p = pos.pop(s)
        gross = p.qty * px
        xfee = gross * fee
        cash += gross - xfee
        cost = p.qty * p.entry_price
        pnl = gross - cost - (p.fee + xfee)
        trades.append(Trade(s, p.qty, p.entry_ts, p.entry_price, ts, px, reason, p.fee + xfee,
                            p.slip + p.qty * slip_per_share, pnl, pnl / cost if cost > 0 else 0.0, row - p.entry_row,
                            pnl / (p.qty * p.risk_per_share) if p.risk_per_share > 0 else float("nan")))

    for ti, t in enumerate(timeline):
        here = [(s, row_at[s][t]) for s in syms if t in row_at[s]]
        exited_now = set()
        # 1. exits at the open
        for s, k in here:
            if s in pos and k >= 1:
                p = pos[s]
                timed = (not cfg.m17_compat) and k - p.entry_row >= exits.time_stop_bars
                if EX[s][k - 1] or timed:
                    gross = O[s][k]
                    fill = gross * (1 - slip)
                    close(s, fill, t, "signal" if EX[s][k - 1] else "time", k, gross - fill)
                    exited_now.add(s)
        # 2. entries at the open, ranked by score then symbol
        cands = [(s, k) for s, k in here if k >= 1 and EN[s][k - 1]]
        seen += len(cands)
        cands.sort(key=lambda sk: (-SC[sk[0]][sk[1] - 1], sk[0]))
        marks = {s: O[s][k] for s, k in here}
        for s, k in cands:
            if s in pos:
                continue
            if s in exited_now:
                skipped["reentry_same_bar"] += 1
                continue
            if membership is not None and not bool(membership[s][k - 1]):
                skipped["not_in_universe"] += 1
                continue
            if len(pos) >= cfg.max_positions:
                skipped["max_positions"] += 1
                continue
            fill = O[s][k] * (1 + slip)
            if cfg.stop_mode == "atr":
                a = AT[s][k - 1]
                if not (np.isfinite(a) and a > 0):
                    skipped["atr_unavailable"] += 1
                    continue
                stop = fill - exits.stop_atr * a
                target = fill + exits.target_atr * a if exits.target_atr is not None else None
                if stop >= fill:
                    skipped["stop_above_fill"] += 1
                    continue
            else:
                a = AT[s][k - 1]
                stop = fill * (1 - cfg.stop_pct) if cfg.stop_pct is not None else None
                target = fill * (1 + cfg.target_pct) if cfg.target_pct is not None else None
            gross_exp = sum(q.qty * marks.get(n, last_close.get(n, q.entry_price)) for n, q in pos.items())
            equity = cash + gross_exp
            shares_cap = equity * risk.max_position_pct / fill
            if stop is not None and stop < fill:
                raw = min(equity * risk.risk_per_trade / (fill - stop), shares_cap)
            else:
                raw = shares_cap
            qty = int(math.floor(raw))
            if qty <= 0:
                skipped["zero_size"] += 1
                continue
            room = cfg.max_gross_exposure * equity - gross_exp
            if qty * fill > room:
                qty = int(math.floor(max(room, 0.0) / fill))
                if qty <= 0:
                    skipped["gross_exposure"] += 1
                    continue
            afford = int(math.floor(cash / (fill * (1 + fee))))
            if qty > afford:
                qty = afford
                if qty <= 0:
                    skipped["insufficient_cash"] += 1
                    continue
            efee = qty * fill * fee
            cash -= qty * fill + efee
            min_cash = min(min_cash, cash)
            rps = (fill - stop) if stop is not None and stop < fill else 0.0
            pos[s] = _Pos(s, qty, fill, stop, target, k, t, efee, qty * (fill - O[s][k]),
                          float(a) if np.isfinite(a) else float("nan"), rps, -np.inf)
        # 3. intrabar stop / target (from the entry bar on); pessimistic stop-first
        for s, k in here:
            if s not in pos:
                continue
            p = pos[s]
            sl = p.stop is not None and L[s][k] <= p.stop
            tp = p.target is not None and H[s][k] >= p.target
            if sl:
                gross = O[s][k] if O[s][k] <= p.stop else p.stop
                fill = gross * (1 - slip)
                close(s, fill, t, "stop_loss", k, gross - fill)
            elif tp:
                gross = O[s][k] if O[s][k] >= p.target else p.target
                fill = gross * (1 - slip)
                close(s, fill, t, "take_profit", k, gross - fill)
        # 4. close: trailing stops, delistings, marks
        for s, k in here:
            last_close[s] = Cl[s][k]
            if s in pos:
                p = pos[s]
                p.highest_close = max(p.highest_close, Cl[s][k])
                if (not cfg.m17_compat) and exits.trail_atr is not None and np.isfinite(p.atr):
                    trail = p.highest_close - exits.trail_atr * p.atr
                    p.stop = trail if p.stop is None else max(p.stop, trail)
                if k == last_row[s] and t < timeline[-1]:
                    close(s, Cl[s][k], t, "delisted", k, 0.0)
        eq_vals[ti] = cash + sum(q.qty * last_close[n] for n, q in pos.items())
    for s in sorted(pos):
        k = last_row[s]
        close(s, Cl[s][k], ts_of[s].iloc[k], "eod", k, 0.0)
    if len(timeline):
        eq_vals[-1] = cash
    return EngineResult(trades, pd.Series(eq_vals, index=timeline, name="equity"), skipped, seen, asdict(cfg),
                        genome_id, {"min_cash": float(min_cash)})


def run_genome(g: Genome, bars: Mapping[str, pd.DataFrame], cfg: EngineConfig = EngineConfig(),
               membership: Optional[Mapping[str, np.ndarray]] = None) -> EngineResult:
    sigs = {s: template_signals(g, b) for s, b in bars.items()}
    return run(bars, sigs, g.exits, g.risk, cfg, membership, g.genome_id)


def metrics(res: EngineResult, periods_per_year: float = 252.0) -> Dict[str, float]:
    eq = res.equity
    rets = eq.pct_change().dropna().to_numpy()
    tr = res.trades
    pnl = np.array([t.pnl_absolute for t in tr])
    rm = np.array([t.r_multiple for t in tr if np.isfinite(t.r_multiple)])
    dd = (eq / eq.cummax() - 1.0).min() if len(eq) else 0.0
    sd = rets.std(ddof=1) if len(rets) > 1 else 0.0
    gains, losses = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    return {"total_return": float(eq.iloc[-1] / res.config["initial_equity"] - 1.0) if len(eq) else 0.0,
            "sharpe_ann": float(rets.mean() / sd * math.sqrt(periods_per_year)) if sd > 0 else 0.0,
            "max_drawdown": float(dd), "n_trades": float(len(tr)),
            "win_rate": float((pnl > 0).mean()) if len(pnl) else float("nan"),
            "mean_r": float(rm.mean()) if len(rm) else float("nan"),
            "profit_factor": float(gains / losses) if losses > 0 else float("inf") if gains > 0 else float("nan"),
            "signals_seen": float(res.signals_seen), "skipped": float(sum(res.skipped.values())),
            "mean_bars_held": float(np.mean([t.bars_held for t in tr])) if tr else float("nan")}


__all__ = ["SKIP_REASONS", "EngineConfig", "Trade", "EngineResult", "run", "run_genome", "metrics"]
