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

Timestamps (second review, PR #10). ``ts_utc`` is a bar's CLOSE. A fill at a
bar's open happens at that bar's OPEN, so every trade carries:

* ``decision_ts`` - close of the signal bar (when the decision was made);
* ``entry_fill_ts`` - open of the fill bar (the simulated execution event);
* ``entry_bar_close_ts`` / ``exit_bar_close_ts`` - closes of the fill bars.
  ``entry_ts`` / ``exit_ts`` keep M17's legacy meaning (the fill bar's close
  label) for parity only; they are NOT fill times and must not be used to
  join fills across lanes;
* ``exit_fill_ts`` - open for open exits (signal, time, gap through a stop or
  target), close for delisted / end-of-data marks, and ``NaT`` for intrabar
  stop / target fills, whose time inside the bar is unobservable on OHLC
  data; ``exit_fill_window_start/end`` bound it (bar open .. bar close).

Bar opens come from a ``bar_open_ts`` column (exchange calendar / vendor bar
start) or, for synthetic data only, ``EngineConfig.bar_minutes``. With
neither, fill times are ``NaT`` and stated as unknown; a close is never
substituted for an open.

Decision records (second review, PR #11). Every evaluated candidate gets one
record in ``EngineResult.decisions``: entered, rejected by the template's
rule (when the template reports a setup), position already open, re-entry
on the exit bar, outside the as-of universe, blocked by capacity, blocked by
risk/exposure/cash sizing, unfillable (no ATR, stop at/above fill) or no next
bar. Candidate ids use the lane A formula (symbol, decision time, direction,
strategy id = genome id), so cross-lane joins are on the decision, not on a
fill label.
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
DECISION_STAGES = ("entered", "rejected_by_rule", "position_already_open", "reentry_same_bar", "not_in_universe",
                   "blocked_capacity", "blocked_risk", "unfillable", "no_next_bar")
_STAGE_OF_SKIP = {"max_positions": "blocked_capacity", "gross_exposure": "blocked_risk",
                  "insufficient_cash": "blocked_risk", "zero_size": "blocked_risk", "atr_unavailable": "unfillable",
                  "stop_above_fill": "unfillable", "not_in_universe": "not_in_universe",
                  "reentry_same_bar": "reentry_same_bar"}
FILL_BASIS = ("bar_open", "bar_open_gap", "intrabar_unobservable", "bar_close")


def candidate_id(symbol: str, decision_ts, direction: str, strategy_id: str) -> str:
    """Same formula as lane A ``research.evidence.candidates.make_candidate_id`` (asserted in integration)."""
    return "C-" + digest({"s": symbol, "t": pd.Timestamp(decision_ts).isoformat(), "d": direction,
                          "k": strategy_id})[:16]


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
    bar_minutes: Optional[int] = None      # SYNTHETIC data only: bar open = close - bar_minutes (else bar_open_ts)

    def __post_init__(self):
        if self.stop_mode not in ("atr", "pct"):
            raise ValueError("stop_mode must be 'atr' or 'pct'")
        if self.max_positions < 1 or not self.max_gross_exposure > 0:
            raise ValueError("max_positions >= 1 and max_gross_exposure > 0 required")
        if self.bar_minutes is not None and not self.bar_minutes > 0:
            raise ValueError("bar_minutes must be > 0 when given")


@dataclass(frozen=True)
class Trade:
    symbol: str
    qty: int
    entry_ts: pd.Timestamp                 # LEGACY M17 label: close of the entry fill bar (not the fill time)
    entry_price: float
    exit_ts: pd.Timestamp                  # LEGACY M17 label: close of the exit bar (not the fill time)
    exit_price: float
    exit_reason: str                       # signal | stop_loss | take_profit | time | delisted | eod
    fees_paid: float
    slippage_paid: float
    pnl_absolute: float
    pnl_pct: float
    bars_held: int
    r_multiple: float
    candidate_id: str = ""
    decision_ts: Optional[pd.Timestamp] = None         # close of the signal bar
    entry_bar_close_ts: Optional[pd.Timestamp] = None
    entry_fill_ts: Optional[pd.Timestamp] = None       # open of the fill bar (NaT if the open time is unknown)
    exit_bar_close_ts: Optional[pd.Timestamp] = None
    exit_fill_ts: Optional[pd.Timestamp] = None        # NaT for intrabar fills (unobservable) or unknown opens
    exit_fill_window_start: Optional[pd.Timestamp] = None
    exit_fill_window_end: Optional[pd.Timestamp] = None
    exit_fill_basis: str = ""                          # one of FILL_BASIS


@dataclass
class _Pos:
    symbol: str
    qty: int
    entry_price: float
    stop: Optional[float]
    target: Optional[float]
    entry_row: int
    entry_ts: pd.Timestamp
    candidate_id: str
    decision_ts: pd.Timestamp
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
    decisions: List[Dict] = field(default_factory=list)

    @property
    def digest(self) -> str:
        return digest({"trades": [{k: (str(v) if isinstance(v, pd.Timestamp) else v) for k, v in asdict(t).items()}
                                  for t in self.trades],
                       "equity": [float(x) for x in self.equity.values], "skipped": self.skipped,
                       "decisions": [{k: str(v) for k, v in d.items()} for d in self.decisions]})

    def decision_counts(self) -> Dict[str, int]:
        out = {s: 0 for s in DECISION_STAGES}
        for d in self.decisions:
            out[d["stage"]] += 1
        return out


def _bar_opens(b: pd.DataFrame, ts_close: pd.Series, cfg: "EngineConfig") -> pd.Series:
    if "bar_open_ts" in b.columns:
        bo = pd.to_datetime(b["bar_open_ts"], utc=True).reset_index(drop=True)
        if bo.isna().any() or (bo >= ts_close).any():
            raise ValueError("bar_open_ts must be present and strictly before each bar's ts_utc (close)")
        return bo
    if cfg.bar_minutes is not None:
        return ts_close - pd.Timedelta(cfg.bar_minutes, unit="m")
    return pd.Series(pd.NaT, index=ts_close.index, dtype="datetime64[ns, UTC]")


def run(bars: Mapping[str, pd.DataFrame], sigs: Mapping[str, pd.DataFrame], exits: ExitSpec, risk: RiskSpec,
        cfg: EngineConfig = EngineConfig(), membership: Optional[Mapping[str, np.ndarray]] = None,
        genome_id: str = "") -> EngineResult:
    syms = sorted(bars)
    fee, slip = cfg.fee_bps / 1e4, cfg.slippage_bps / 1e4
    ts_of = {s: pd.to_datetime(bars[s]["ts_utc"], utc=True).reset_index(drop=True) for s in syms}
    open_of = {s: _bar_opens(bars[s], ts_of[s], cfg) for s in syms}
    basis = ("bar_open_ts column" if syms and all("bar_open_ts" in bars[s].columns for s in syms) else
             "synthetic convention: open = close - %d min" % cfg.bar_minutes if cfg.bar_minutes is not None else
             "unknown: fill times reported as NaT")
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
    SU = {s: (sigs[s]["setup"].to_numpy(bool) if "setup" in sigs[s].columns else None) for s in syms}
    RR = {s: (sigs[s]["reject_reason"].astype(str).to_numpy() if "reject_reason" in sigs[s].columns else None)
          for s in syms}
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
    decisions: List[Dict] = []
    open_decision: Dict[str, int] = {}

    def decide(s, k_sig, stage, reason="", score=float("nan")):
        dts = ts_of[s].iloc[k_sig]
        cid = candidate_id(s, dts, "long", genome_id)
        decisions.append({"candidate_id": cid, "genome_id": genome_id, "symbol": s, "decision_ts": dts,
                          "fill_bar_close_ts": ts_of[s].iloc[k_sig + 1] if k_sig + 1 <= last_row[s] else pd.NaT,
                          "stage": stage, "reason": reason, "score": float(score), "trade_index": -1})
        return cid

    def close(s, px, ts, reason, row, slip_per_share, fill_basis):
        nonlocal cash
        p = pos.pop(s)
        gross = p.qty * px
        xfee = gross * fee
        cash += gross - xfee
        cost = p.qty * p.entry_price
        pnl = gross - cost - (p.fee + xfee)
        bo, bc = open_of[s].iloc[row], ts_of[s].iloc[row]
        fill_ts = {"bar_open": bo, "bar_open_gap": bo, "intrabar_unobservable": pd.NaT, "bar_close": bc}[fill_basis]
        unobs = fill_basis == "intrabar_unobservable"
        trades.append(Trade(s, p.qty, p.entry_ts, p.entry_price, ts, px, reason, p.fee + xfee,
                            p.slip + p.qty * slip_per_share, pnl, pnl / cost if cost > 0 else 0.0, row - p.entry_row,
                            pnl / (p.qty * p.risk_per_share) if p.risk_per_share > 0 else float("nan"),
                            candidate_id=p.candidate_id, decision_ts=p.decision_ts, entry_bar_close_ts=p.entry_ts,
                            entry_fill_ts=open_of[s].iloc[p.entry_row], exit_bar_close_ts=bc, exit_fill_ts=fill_ts,
                            exit_fill_window_start=bo if unobs else fill_ts, exit_fill_window_end=bc if unobs else fill_ts,
                            exit_fill_basis=fill_basis))
        decisions[open_decision.pop(s)]["trade_index"] = len(trades) - 1

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
                    close(s, fill, t, "signal" if EX[s][k - 1] else "time", k, gross - fill, "bar_open")
                    exited_now.add(s)
        # 2a. setups the template's own rule rejected (recorded, never traded)
        for s, k in here:
            if k >= 1 and SU[s] is not None and SU[s][k - 1] and not EN[s][k - 1]:
                decide(s, k - 1, "rejected_by_rule", RR[s][k - 1] if RR[s] is not None else "", SC[s][k - 1])
        # 2. entries at the open, ranked by score then symbol
        cands = [(s, k) for s, k in here if k >= 1 and EN[s][k - 1]]
        seen += len(cands)
        cands.sort(key=lambda sk: (-SC[sk[0]][sk[1] - 1], sk[0]))
        marks = {s: O[s][k] for s, k in here}

        def skip(s, k, why):
            skipped[why] += 1
            decide(s, k - 1, _STAGE_OF_SKIP[why], why, SC[s][k - 1])

        for s, k in cands:
            if s in pos:
                decide(s, k - 1, "position_already_open", "one position per symbol", SC[s][k - 1])
                continue
            if s in exited_now:
                skip(s, k, "reentry_same_bar")
                continue
            if membership is not None and not bool(membership[s][k - 1]):
                skip(s, k, "not_in_universe")
                continue
            if len(pos) >= cfg.max_positions:
                skip(s, k, "max_positions")
                continue
            fill = O[s][k] * (1 + slip)
            if cfg.stop_mode == "atr":
                a = AT[s][k - 1]
                if not (np.isfinite(a) and a > 0):
                    skip(s, k, "atr_unavailable")
                    continue
                stop = fill - exits.stop_atr * a
                target = fill + exits.target_atr * a if exits.target_atr is not None else None
                if stop >= fill:
                    skip(s, k, "stop_above_fill")
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
                skip(s, k, "zero_size")
                continue
            room = cfg.max_gross_exposure * equity - gross_exp
            if qty * fill > room:
                qty = int(math.floor(max(room, 0.0) / fill))
                if qty <= 0:
                    skip(s, k, "gross_exposure")
                    continue
            afford = int(math.floor(cash / (fill * (1 + fee))))
            if qty > afford:
                qty = afford
                if qty <= 0:
                    skip(s, k, "insufficient_cash")
                    continue
            efee = qty * fill * fee
            cash -= qty * fill + efee
            min_cash = min(min_cash, cash)
            rps = (fill - stop) if stop is not None and stop < fill else 0.0
            cid = decide(s, k - 1, "entered", "", SC[s][k - 1])
            open_decision[s] = len(decisions) - 1
            pos[s] = _Pos(s, qty, fill, stop, target, k, t, cid, ts_of[s].iloc[k - 1], efee, qty * (fill - O[s][k]),
                          float(a) if np.isfinite(a) else float("nan"), rps, -np.inf)
        # 3. intrabar stop / target (from the entry bar on); pessimistic stop-first
        for s, k in here:
            if s not in pos:
                continue
            p = pos[s]
            sl = p.stop is not None and L[s][k] <= p.stop
            tp = p.target is not None and H[s][k] >= p.target
            if sl:
                gap = O[s][k] <= p.stop
                gross = O[s][k] if gap else p.stop
                fill = gross * (1 - slip)
                close(s, fill, t, "stop_loss", k, gross - fill, "bar_open_gap" if gap else "intrabar_unobservable")
            elif tp:
                gap = O[s][k] >= p.target
                gross = O[s][k] if gap else p.target
                fill = gross * (1 - slip)
                close(s, fill, t, "take_profit", k, gross - fill, "bar_open_gap" if gap else "intrabar_unobservable")
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
                    close(s, Cl[s][k], t, "delisted", k, 0.0, "bar_close")
        eq_vals[ti] = cash + sum(q.qty * last_close[n] for n, q in pos.items())
    for s in sorted(pos):
        k = last_row[s]
        close(s, Cl[s][k], ts_of[s].iloc[k], "eod", k, 0.0, "bar_close")
    if len(timeline):
        eq_vals[-1] = cash
    # a signal on a symbol's last bar has no next bar to fill on: recorded, never traded
    for s in syms:
        k = last_row[s]
        if k >= 0 and EN[s][k]:
            decide(s, k, "no_next_bar", "no later bar for this symbol", SC[s][k])
        elif k >= 0 and SU[s] is not None and SU[s][k]:
            decide(s, k, "rejected_by_rule", RR[s][k] if RR[s] is not None else "", SC[s][k])
    return EngineResult(trades, pd.Series(eq_vals, index=timeline, name="equity"), skipped, seen, asdict(cfg),
                        genome_id, {"min_cash": float(min_cash), "fill_time_basis": basis}, decisions)


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


__all__ = ["SKIP_REASONS", "DECISION_STAGES", "FILL_BASIS", "candidate_id", "EngineConfig", "Trade", "EngineResult",
           "run", "run_genome", "metrics"]
