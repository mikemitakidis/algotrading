"""research.economics.model — trade-level execution economics (canonical B10; M21 WP12).

Prices one round trip in REAL terms (real share counts and real prices),
whatever price basis the research bars use, and itemises every cost:

* half-spread by dollar ADV bucket, floored at half a tick of the real price
  (a measured quoted spread replaces the bucket when supplied);
* market impact, square-root law: ``coef x daily_vol x sqrt(notional / ADV$)``;
* volatility-scaled slippage, plus extra adverse slippage on gap-through stops;
* broker and regulatory fees from ``research.economics.fees`` (dated);
* short borrow, with "easy" / "hard" / "unavailable" / "unknown" states;
* split-correct share counts and cash dividends.

Fail-closed rules (a trade that could not have happened is never priced as
free or as filled):

* unknown or insufficient liquidity history -> NO_FILL;
* a halted or zero-volume entry bar -> NO_FILL;
* a short with borrow "unavailable" or "unknown", or "hard" with no rate -> NO_FILL;
* an entry above the ADV or bar participation cap is CAPPED to the cap
  (NO_FILL below one real share); an exit above the cap is priced at full
  size and flagged ``exit_capacity_breach`` (the exit cannot be partial);
* a halted or zero-volume exit bar -> INCONCLUSIVE (that exit price was not
  available);
* raw-basis prices without a complete corporate-action record -> INCONCLUSIVE
  (a hidden split would corrupt the P&L).

Every spread, impact and slippage coefficient is an ASSUMPTION to calibrate
against realised paper fills (``research.economics.analysis.fee_calibration``).
Liquidity is measured point-in-time: only daily bars whose close is at or
before the decision time are used. Daily bars for liquidity should be
split-adjusted (dollar volume is basis-invariant; return volatility is not).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from research.economics.fees import (BUY, SELL, IBKR_PRO_FIXED, US_REGULATORY_2026, CommissionPlan, FeeBreakdown,
                                     RegulatoryFees, order_fees)

LIQ_OK, LIQ_INSUFFICIENT, LIQ_MISSING = "ok", "insufficient_history", "missing"
FILLED, CAPPED, NO_FILL, INCONCLUSIVE = "FILLED", "CAPPED", "NO_FILL", "INCONCLUSIVE"
BASES = ("raw", "split_adjusted", "all_adjusted")
BORROW_STATES = ("easy", "hard", "unavailable", "unknown")


# ── liquidity ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class LiquiditySnapshot:
    symbol: str
    asof_utc: Optional[pd.Timestamp]
    status: str
    sessions: int
    adv_dollars: float
    daily_vol: float                     # stdev of daily log returns (fraction)
    zero_volume_sessions: int
    last_close: float
    quoted_half_spread_bps: Optional[float] = None   # measured, when quotes exist

    @property
    def ok(self) -> bool:
        return self.status == LIQ_OK


def missing_liquidity(symbol: str = "", asof_utc=None) -> LiquiditySnapshot:
    return LiquiditySnapshot(symbol, asof_utc, LIQ_MISSING, 0, float("nan"), float("nan"), 0, float("nan"))


def liquidity_asof(daily: Optional[pd.DataFrame], asof_utc, *, symbol: str = "", window: int = 20,
                   min_sessions: int = 15) -> LiquiditySnapshot:
    """Dollar ADV and daily volatility from completed daily bars (``ts_utc`` =
    bar close) at or before ``asof_utc``. Rows with a non-finite or
    non-positive close, or negative / non-finite volume, are not counted."""
    asof = pd.Timestamp(asof_utc)
    asof = asof.tz_localize("UTC") if asof.tzinfo is None else asof.tz_convert("UTC")
    if daily is None or len(daily) == 0:
        return missing_liquidity(symbol, asof)
    ts = pd.to_datetime(daily["ts_utc"], utc=True)
    d = daily.loc[(ts <= asof).to_numpy()].tail(window)
    c = d["close"].to_numpy(dtype=float) if len(d) else np.array([])
    v = d["volume"].to_numpy(dtype=float) if len(d) else np.array([])
    good = np.isfinite(c) & (c > 0) & np.isfinite(v) & (v >= 0)
    c, v = c[good], v[good]
    n = int(len(c))
    if n == 0:
        return missing_liquidity(symbol, asof)
    adv = float(np.mean(c * v))
    vol = float(np.std(np.diff(np.log(c)), ddof=1)) if n >= 3 else float("nan")
    ok = n >= min_sessions and adv > 0 and np.isfinite(vol)
    return LiquiditySnapshot(symbol, asof, LIQ_OK if ok else LIQ_INSUFFICIENT, n, adv, vol,
                             int(np.sum(v == 0)), float(c[-1]))


@dataclass(frozen=True)
class FillBar:
    """The bar a fill happens in (same price basis as the trade). ``halted``
    comes from a halt feed or data flag when one exists."""
    open: float
    high: float
    low: float
    close: float
    volume: float
    halted: bool = False

    @property
    def tradable(self) -> bool:
        return (not self.halted) and np.isfinite(self.volume) and self.volume > 0

    @property
    def dollar_volume(self) -> float:
        return float(self.volume * self.close)


# ── borrow and corporate actions ─────────────────────────────────────────────
@dataclass(frozen=True)
class BorrowInfo:
    status: str                          # easy | hard | unavailable | unknown
    annual_rate: Optional[float] = None  # fraction per year
    source: str = ""

    def __post_init__(self):
        if self.status not in BORROW_STATES:
            raise ValueError("borrow status must be one of %s" % (BORROW_STATES,))


@dataclass(frozen=True)
class SplitEvent:
    ex_date: date
    ratio: float                         # new shares per old share (2.0 = 2-for-1; 0.1 = 1-for-10)

    def __post_init__(self):
        if not (self.ratio > 0 and math.isfinite(self.ratio)):
            raise ValueError("split ratio must be positive")


@dataclass(frozen=True)
class CashDividend:
    ex_date: date
    amount: float                        # USD per real share outstanding on the ex-date


@dataclass(frozen=True)
class CorporateActions:
    """``complete`` = the record is known to cover the symbol through the end of
    the data (e.g. a complete WP5 corporate-actions fetch). Adjusted prices are
    assumed back-adjusted to the end of that record."""
    complete: bool
    splits: Tuple[SplitEvent, ...] = ()
    dividends: Tuple[CashDividend, ...] = ()

    def factor_after(self, d: date) -> float:
        """Real price = split-adjusted price x factor_after(d)."""
        return float(np.prod([s.ratio for s in self.splits if s.ex_date > d])) if self.splits else 1.0

    def ratio_between(self, d0: date, d1: date) -> float:
        """Real shares multiply by this between d0 (exclusive) and d1 (inclusive)."""
        return float(np.prod([s.ratio for s in self.splits if d0 < s.ex_date <= d1])) if self.splits else 1.0


NO_ACTIONS_VERIFIED = CorporateActions(complete=True)


# ── the model ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class EconomicsModel:
    name: str = "wp12_base"
    plan: CommissionPlan = IBKR_PRO_FIXED
    regulatory: RegulatoryFees = US_REGULATORY_2026
    # (minimum dollar ADV, half-spread bps), descending; ASSUMPTIONS to calibrate
    spread_buckets: Tuple[Tuple[float, float], ...] = ((500e6, 1.0), (100e6, 2.0), (20e6, 5.0), (5e6, 12.0),
                                                       (1e6, 30.0), (0.0, 75.0))
    tick: float = 0.01
    impact_coef: float = 0.7
    base_slippage_bps: float = 1.0
    vol_slippage_coef: float = 0.02      # x daily vol (bps): 2 % daily vol -> 4 bps
    stop_gap_vol_coef: float = 0.10      # x daily vol (bps), gap-through stops only
    max_adv_participation: float = 0.01  # of dollar ADV
    max_bar_participation: float = 0.10  # of the fill bar's dollar volume
    extreme_daily_vol: float = 0.10
    easy_borrow_annual: float = 0.0025   # ASSUMPTION when "easy" carries no quoted rate
    borrow_day_count: float = 360.0
    monthly_shares: float = 0.0          # tiered commission volume (0 = highest rate)
    cost_multiplier: float = 1.0         # stress: scales every cost component

    def __post_init__(self):
        if not (0 < self.max_adv_participation <= 1 and 0 < self.max_bar_participation <= 1):
            raise ValueError("participation caps must be in (0, 1]")
        if not self.cost_multiplier >= 0:
            raise ValueError("cost_multiplier must be >= 0")

    def scale(self, k: float) -> "EconomicsModel":
        return replace(self, name="%s_x%g" % (self.name, k), cost_multiplier=self.cost_multiplier * k)

    def half_spread_bps(self, liq: LiquiditySnapshot, real_price: float) -> float:
        if liq.quoted_half_spread_bps is not None and np.isfinite(liq.quoted_half_spread_bps):
            bps = float(liq.quoted_half_spread_bps)
        else:
            bps = next(b for floor_adv, b in self.spread_buckets if liq.adv_dollars >= floor_adv)
        return max(bps, 0.5 * self.tick / real_price * 1e4)

    def impact_bps(self, liq: LiquiditySnapshot, notional: float) -> float:
        if notional <= 0:
            return 0.0
        return float(self.impact_coef * liq.daily_vol * math.sqrt(notional / liq.adv_dollars) * 1e4)

    def slippage_bps(self, liq: LiquiditySnapshot) -> float:
        return float(self.base_slippage_bps + self.vol_slippage_coef * liq.daily_vol * 1e4)

    def stop_gap_bps(self, liq: LiquiditySnapshot) -> float:
        return float(self.stop_gap_vol_coef * liq.daily_vol * 1e4)

    def cap_notional(self, liq: LiquiditySnapshot, bar: Optional[FillBar]) -> float:
        cap = self.max_adv_participation * liq.adv_dollars
        if bar is not None:
            cap = min(cap, self.max_bar_participation * bar.dollar_volume)
        return float(cap)


# ── one trade ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TradeSpec:
    symbol: str
    direction: str                       # long | short
    shares: float                        # requested, on the price basis of the refs
    entry_ref_price: float               # gross reference price (e.g. next open), before costs
    exit_ref_price: float
    entry_date: date
    exit_date: date
    risk_per_share: Optional[float] = None
    stop_gap: bool = False               # the exit is a stop that gapped through
    entry_bar: Optional[FillBar] = None
    exit_bar: Optional[FillBar] = None

    def __post_init__(self):
        if self.direction not in ("long", "short"):
            raise ValueError("direction must be 'long' or 'short'")
        if self.exit_date < self.entry_date:
            raise ValueError("exit_date before entry_date")


@dataclass(frozen=True)
class TradeEconomics:
    symbol: str
    direction: str
    status: str
    reasons: Tuple[str, ...]
    flags: Tuple[str, ...]
    shares_requested: float
    shares_filled: float                 # on the input price basis
    real_entry_shares: int
    real_exit_shares: int
    real_entry_price: float              # reference, real terms
    real_exit_price: float
    entry_fill: float
    exit_fill: float
    notional: float                      # real entry notional
    gross_pnl: float
    spread_cost: float
    impact_cost: float
    slippage_cost: float
    stop_gap_cost: float
    fees_entry: FeeBreakdown
    fees_exit: FeeBreakdown
    borrow_cost: float
    dividend_cash: float                 # + received (long), - paid (short)
    net_pnl: float
    gross_r: float
    net_r: float
    model: str

    @property
    def fees(self) -> float:
        return self.fees_entry.total + self.fees_exit.total

    @property
    def total_cost(self) -> float:
        return self.spread_cost + self.impact_cost + self.slippage_cost + self.stop_gap_cost + self.fees + \
            self.borrow_cost

    @property
    def cost_bps(self) -> float:
        return self.total_cost / self.notional * 1e4 if self.notional > 0 else float("nan")

    @property
    def priced(self) -> bool:
        return self.status in (FILLED, CAPPED)


_ZERO_FEES = FeeBreakdown(0, 0, 0, 0, 0, 0, 0)


def _unpriced(spec: TradeSpec, model: EconomicsModel, status: str, reasons, flags) -> TradeEconomics:
    nan = float("nan")
    return TradeEconomics(spec.symbol, spec.direction, status, tuple(reasons), tuple(flags), float(spec.shares),
                          0.0, 0, 0, nan, nan, nan, nan, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, _ZERO_FEES, _ZERO_FEES, 0.0,
                          0.0, 0.0, nan, nan, model.name)


def price_trade(spec: TradeSpec, *, model: EconomicsModel, entry_liq: LiquiditySnapshot,
                exit_liq: Optional[LiquiditySnapshot] = None, borrow: Optional[BorrowInfo] = None,
                actions: Optional[CorporateActions] = None, basis: str = "split_adjusted") -> TradeEconomics:
    if basis not in BASES:
        raise ValueError("basis must be one of %s" % (BASES,))
    long = spec.direction == "long"
    flags = []
    if actions is None:
        actions = CorporateActions(complete=False)
    if not actions.complete:
        flags.append("corporate_actions_unverified")
    # 1. can the entry happen at all?
    if not entry_liq.ok:
        return _unpriced(spec, model, NO_FILL, ["liquidity_" + entry_liq.status], flags)
    if spec.entry_bar is not None and not spec.entry_bar.tradable:
        why = "entry_bar_halted" if spec.entry_bar.halted else "entry_bar_zero_volume"
        return _unpriced(spec, model, NO_FILL, [why], flags)
    if not long:
        b = borrow or BorrowInfo("unknown")
        if b.status in ("unavailable", "unknown"):
            return _unpriced(spec, model, NO_FILL, ["borrow_" + b.status], flags)
        if b.status == "hard" and b.annual_rate is None:
            return _unpriced(spec, model, NO_FILL, ["borrow_hard_rate_unknown"], flags)
    if not (spec.shares > 0 and spec.entry_ref_price > 0 and spec.exit_ref_price > 0):
        return _unpriced(spec, model, NO_FILL, ["invalid_size_or_price"], flags)
    # 2. real terms
    fe = 1.0 if basis == "raw" else actions.factor_after(spec.entry_date)
    fx = 1.0 if basis == "raw" else actions.factor_after(spec.exit_date)
    if basis == "all_adjusted" and (actions.dividends or not actions.complete):
        flags.append("real_price_approximate_all_adjusted")
    p0, p1 = spec.entry_ref_price * fe, spec.exit_ref_price * fx
    q0 = int(math.floor(spec.shares / fe + 1e-9))
    status, reasons = FILLED, []
    cap = model.cap_notional(entry_liq, spec.entry_bar)
    if q0 * p0 > cap:
        q0 = int(math.floor(cap / p0))
        status = CAPPED
        flags.append("entry_participation_capped")
    if q0 < 1:
        return _unpriced(spec, model, NO_FILL, ["below_one_share_after_caps" if status == CAPPED else
                                                "below_one_share"], flags)
    ratio = actions.ratio_between(spec.entry_date, spec.exit_date)
    if ratio != 1.0 and basis == "raw":
        flags.append("split_in_window_corrected")
    q1_exact = q0 * ratio
    q1 = int(math.floor(q1_exact + 1e-9))
    if abs(q1_exact - q1) > 1e-9:
        flags.append("fractional_shares_after_split")
    if basis == "raw" and not actions.complete:
        status, reasons = INCONCLUSIVE, reasons + ["raw_basis_without_verified_corporate_actions"]
    # 3. exit
    xl = exit_liq if exit_liq is not None else entry_liq
    if not xl.ok:
        flags.append("exit_liquidity_unknown_used_entry")
        xl = entry_liq
    if spec.exit_bar is not None and not spec.exit_bar.tradable:
        status, reasons = INCONCLUSIVE, reasons + ["exit_bar_untradable"]
    n0, n1 = q0 * p0, q1 * p1
    if n1 > model.cap_notional(xl, spec.exit_bar):
        flags.append("exit_capacity_breach")
    for liq in {id(entry_liq): entry_liq, id(xl): xl}.values():
        if liq.daily_vol > model.extreme_daily_vol:
            flags.append("extreme_volatility")
        if liq.zero_volume_sessions > 0:
            flags.append("zero_volume_sessions_in_window")
    m = model.cost_multiplier
    hs0, im0, sl0 = model.half_spread_bps(entry_liq, p0), model.impact_bps(entry_liq, n0), model.slippage_bps(entry_liq)
    hs1, im1, sl1 = model.half_spread_bps(xl, p1), model.impact_bps(xl, n1), model.slippage_bps(xl)
    gp1 = model.stop_gap_bps(xl) if spec.stop_gap else 0.0
    adv0 = (hs0 + im0 + sl0) * m / 1e4
    adv1 = (hs1 + im1 + sl1 + gp1) * m / 1e4
    f0 = p0 * (1 + adv0) if long else p0 * (1 - adv0)
    f1 = p1 * (1 - adv1) if long else p1 * (1 + adv1)
    fees0 = order_fees(q0, f0, BUY if long else SELL, spec.entry_date, plan=model.plan,
                       regulatory=model.regulatory, monthly_shares=model.monthly_shares).scaled(m)
    fees1 = order_fees(q1, f1, SELL if long else BUY, spec.exit_date, plan=model.plan,
                       regulatory=model.regulatory, monthly_shares=model.monthly_shares).scaled(m)
    flags += [f for f in fees0.flags + fees1.flags if f not in flags]
    borrow_cost = 0.0
    if not long:
        rate = borrow.annual_rate
        if rate is None:
            rate = model.easy_borrow_annual
            flags.append("borrow_rate_assumed")
        nights = (spec.exit_date - spec.entry_date).days
        borrow_cost = n0 * rate * nights / model.borrow_day_count * m
    div = 0.0
    if basis != "all_adjusted":
        for dv in actions.dividends:
            if spec.entry_date < dv.ex_date <= spec.exit_date:
                held = q0 * actions.ratio_between(spec.entry_date, dv.ex_date)
                div += held * dv.amount * (1.0 if long else -1.0)
    gross = (n1 - n0) if long else (n0 - n1)
    spread = (n0 * hs0 + n1 * hs1) * m / 1e4
    impact = (n0 * im0 + n1 * im1) * m / 1e4
    slip = (n0 * sl0 + n1 * sl1) * m / 1e4
    gap = n1 * gp1 * m / 1e4
    net = gross - spread - impact - slip - gap - fees0.total - fees1.total - borrow_cost + div
    risk = spec.risk_per_share * fe if spec.risk_per_share else None
    gr = gross / (q0 * risk) if risk else float("nan")
    nr = net / (q0 * risk) if risk else float("nan")
    return TradeEconomics(spec.symbol, spec.direction, status, tuple(reasons), tuple(dict.fromkeys(flags)),
                          float(spec.shares), q0 * fe, q0, q1, p0, p1, f0, f1, n0, gross, spread, impact, slip, gap,
                          fees0, fees1, borrow_cost, div, net, gr, nr, model.name)


__all__ = ["LIQ_OK", "LIQ_INSUFFICIENT", "LIQ_MISSING", "FILLED", "CAPPED", "NO_FILL", "INCONCLUSIVE", "BASES",
           "LiquiditySnapshot", "missing_liquidity", "liquidity_asof", "FillBar", "BorrowInfo", "SplitEvent",
           "CashDividend", "CorporateActions", "NO_ACTIONS_VERIFIED", "EconomicsModel", "TradeSpec",
           "TradeEconomics", "price_trade"]
