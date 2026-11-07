"""research.economics.fees — dated broker and regulatory fee schedules (US equities).

Every number here is a dated, sourced ASSUMPTION used to model costs. None of
it is a quote, and a schedule can change without notice. Each rate records its
source and whether it was checked against that source; a trade dated outside
every verified window is priced at the most expensive known rate and flagged
``fee_rate_unverified_for_date``. Nothing here contacts a broker.

Sources (fetched 2026-10-28):
* IBKR Pro US stock commissions, Fixed and Tiered:
  https://www.interactivebrokers.com/en/pricing/commissions-stocks.php
  Fixed: USD 0.005/share, min USD 1.00, max 1 % of trade value.
  Tiered: USD 0.0035 (<= 300k shares/month), 0.0020, 0.0015, 0.0010, 0.0005;
  min USD 0.35, max 1 % of trade value. Tiered adds clearing (NSCC/DTC USD
  0.00020/share, capped at 0.5 % of trade value), exchange fees (venue- and
  order-dependent; modelled here as a flat per-share ASSUMPTION) and
  pass-through fees (NYSE 0.000175 x commission, FINRA 0.000565 x commission).
* SEC Section 31 fee: USD 20.60 per million of sale proceeds from 2026-04-04
  (SEC fee rate advisory 2026-2).
* FINRA Trading Activity Fee: USD 0.000166 per share sold, max USD 8.30 per
  trade (FINRA By-Laws Schedule A, Section 1); fee holiday for trades from
  2026-10-01 to 2026-12-31 (IBKR pricing page).
* FINRA CAT fee: USD 0.000003 x quantity, as listed on the IBKR pricing page.

IBKR states that when 1 % of trade value is below the minimum, the 1 % cap
applies; ``commission`` follows that rule.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Optional, Tuple

BUY, SELL = "buy", "sell"


@dataclass(frozen=True)
class DatedRate:
    value: float
    valid_from: date
    valid_to: Optional[date]          # inclusive; None = open-ended
    source: str
    verified: bool = True

    def covers(self, d: date) -> bool:
        return d >= self.valid_from and (self.valid_to is None or d <= self.valid_to)


def rate_on(rates: Tuple[DatedRate, ...], d: date) -> Tuple[float, bool, str]:
    """(value, verified, source) for date ``d``. Outside every window the most
    expensive known rate applies and the result is marked unverified."""
    if not rates:
        raise ValueError("empty rate schedule")
    hits = [r for r in rates if r.covers(d)]
    if hits:
        r = hits[-1]
        return r.value, r.verified, r.source
    worst = max(rates, key=lambda r: r.value)
    return worst.value, False, worst.source + " (date not covered; most expensive known rate)"


@dataclass(frozen=True)
class RegulatoryFees:
    sec_per_dollar_sold: Tuple[DatedRate, ...]
    taf_per_share_sold: Tuple[DatedRate, ...]
    taf_max_per_trade: float = 8.30
    cat_per_share: float = 0.000003


US_REGULATORY_2026 = RegulatoryFees(
    sec_per_dollar_sold=(
        DatedRate(20.60e-6, date(2026, 4, 4), None, "SEC fee rate advisory 2026-2"),
    ),
    taf_per_share_sold=(
        DatedRate(0.000166, date(2026, 1, 1), date(2026, 9, 30), "FINRA By-Laws Schedule A s.1"),
        DatedRate(0.0, date(2026, 10, 1), date(2026, 12, 31), "IBKR pricing page: TAF fee holiday"),
    ),
)


@dataclass(frozen=True)
class CommissionPlan:
    name: str
    tiers: Tuple[Tuple[float, float], ...]   # (monthly shares upper bound, USD per share), ascending
    min_per_order: float
    max_pct_of_value: float
    clearing_per_share: float = 0.0
    clearing_max_pct_of_value: float = 0.0
    exchange_per_share: float = 0.0          # ASSUMPTION (venue/order dependent); 0 = included in plan
    pass_through_pct_of_commission: float = 0.0
    source: str = ""

    def per_share(self, monthly_shares: float) -> float:
        for upper, rate in self.tiers:
            if monthly_shares < upper:
                return rate
        return self.tiers[-1][1]


INF = float("inf")

IBKR_PRO_FIXED = CommissionPlan(
    name="ibkr_pro_fixed", tiers=((INF, 0.005),), min_per_order=1.00, max_pct_of_value=0.01,
    source="IBKR Pro Fixed, pricing page fetched 2026-10-28")

IBKR_PRO_TIERED = CommissionPlan(
    name="ibkr_pro_tiered",
    tiers=((300_000, 0.0035), (3_000_000, 0.0020), (20_000_000, 0.0015), (100_000_000, 0.0010),
           (INF, 0.0005)),
    min_per_order=0.35, max_pct_of_value=0.01,
    clearing_per_share=0.00020, clearing_max_pct_of_value=0.005,
    exchange_per_share=0.0030,               # ASSUMPTION: liquidity-removing fee upper bound
    pass_through_pct_of_commission=0.000175 + 0.000565,
    source="IBKR Pro Tiered, pricing page fetched 2026-10-28; exchange fee is an assumption")


@dataclass(frozen=True)
class FeeBreakdown:
    commission: float
    clearing: float
    exchange: float
    pass_through: float
    sec: float
    taf: float
    cat: float
    flags: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def broker(self) -> float:
        return self.commission + self.clearing + self.exchange + self.pass_through

    @property
    def regulatory(self) -> float:
        return self.sec + self.taf + self.cat

    @property
    def total(self) -> float:
        return self.broker + self.regulatory

    def scaled(self, k: float) -> "FeeBreakdown":
        return replace(self, commission=self.commission * k, clearing=self.clearing * k,
                       exchange=self.exchange * k, pass_through=self.pass_through * k, sec=self.sec * k,
                       taf=self.taf * k, cat=self.cat * k)


def commission(plan: CommissionPlan, shares: float, price: float, *, monthly_shares: float = 0.0) -> float:
    if shares <= 0 or price <= 0:
        return 0.0
    raw = plan.per_share(monthly_shares) * shares
    return min(max(raw, plan.min_per_order), plan.max_pct_of_value * shares * price)


def order_fees(shares: float, price: float, side: str, trade_date: date, *,
               plan: CommissionPlan = IBKR_PRO_FIXED, regulatory: RegulatoryFees = US_REGULATORY_2026,
               monthly_shares: float = 0.0) -> FeeBreakdown:
    """Fees for one fill of ``shares`` real (unadjusted) shares at a real price.

    SEC and TAF apply to sales only (a long exit or a short entry)."""
    if side not in (BUY, SELL):
        raise ValueError("side must be 'buy' or 'sell'")
    if not shares > 0 or not price > 0:
        return FeeBreakdown(0, 0, 0, 0, 0, 0, 0)
    value = shares * price
    comm = commission(plan, shares, price, monthly_shares=monthly_shares)
    clearing = shares * plan.clearing_per_share
    if plan.clearing_max_pct_of_value > 0:
        clearing = min(clearing, plan.clearing_max_pct_of_value * value)
    exchange = shares * plan.exchange_per_share
    passthru = comm * plan.pass_through_pct_of_commission
    flags = []
    sec = taf = 0.0
    if side == SELL:
        r, ok, _ = rate_on(regulatory.sec_per_dollar_sold, trade_date)
        sec = r * value
        flags += [] if ok else ["fee_rate_unverified_for_date:sec"]
        r, ok, _ = rate_on(regulatory.taf_per_share_sold, trade_date)
        taf = min(r * shares, regulatory.taf_max_per_trade)
        flags += [] if ok else ["fee_rate_unverified_for_date:taf"]
    cat = regulatory.cat_per_share * shares
    return FeeBreakdown(comm, clearing, exchange, passthru, sec, taf, cat, tuple(flags))


__all__ = ["BUY", "SELL", "DatedRate", "rate_on", "RegulatoryFees", "US_REGULATORY_2026", "CommissionPlan",
           "IBKR_PRO_FIXED", "IBKR_PRO_TIERED", "FeeBreakdown", "commission", "order_fees"]
