"""research.economics.analysis — what costs do to an edge (M21 WP12).

* ``size_sensitivity``  net result of one trade across notionals: where it
  stops paying, and where capacity binds.
* ``edge_after_costs``  gross vs net over a set of priced trades; says when
  costs erase the edge. Point estimates only: significance belongs to
  ``research.integrity`` (report card, DSR, holdout).
* ``fee_calibration``   realised vs modelled fees and slippage from paper
  fills. Suggests a multiplier; never applies it.
* ``reprice_labels``    re-prices lane A counterfactual labels with the
  economics model (point-in-time liquidity, fixed notional or fixed risk).

Verdicts on thin evidence are INCONCLUSIVE, never a pass.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from research.economics.model import (CAPPED, FILLED, INCONCLUSIVE, NO_FILL, BorrowInfo, CorporateActions,
                                      EconomicsModel, FillBar, LiquiditySnapshot, TradeEconomics, TradeSpec,
                                      liquidity_asof, missing_liquidity, price_trade)

EDGE_SURVIVES, EDGE_ERASED, NO_GROSS_EDGE = "EDGE_SURVIVES_COSTS", "EDGE_ERASED_BY_COSTS", "NO_GROSS_EDGE"
CAPACITY_LIMITED = "CAPACITY_LIMITED"
CALIBRATED, UNDERESTIMATES, OVERESTIMATES = "CALIBRATED", "MODEL_UNDERESTIMATES", "MODEL_OVERESTIMATES"
INCONCLUSIVE_VERDICT = "INCONCLUSIVE"


# ── size sensitivity ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SizeRow:
    notional_requested: float
    notional_filled: float
    status: str
    cost_bps: float
    gross_pnl: float
    net_pnl: float
    net_bps: float


@dataclass(frozen=True)
class SizeSensitivity:
    rows: Tuple[SizeRow, ...]
    min_viable_notional: Optional[float]
    max_viable_notional: Optional[float]
    verdict: str


def size_sensitivity(spec: TradeSpec, notionals: Sequence[float], *, model: EconomicsModel,
                     entry_liq: LiquiditySnapshot, **kw) -> SizeSensitivity:
    rows: List[SizeRow] = []
    for n in sorted(float(x) for x in notionals):
        te = price_trade(replace(spec, shares=n / spec.entry_ref_price), model=model, entry_liq=entry_liq, **kw)
        nb = te.net_pnl / te.notional * 1e4 if te.notional > 0 else float("nan")
        rows.append(SizeRow(n, te.notional, te.status, te.cost_bps, te.gross_pnl, te.net_pnl, nb))
    viable = [r for r in rows if r.status == FILLED and r.net_pnl > 0]
    lo = viable[0].notional_requested if viable else None
    hi = viable[-1].notional_requested if viable else None
    if not viable:
        verdict = EDGE_ERASED
    elif any(r.status in (CAPPED, NO_FILL) for r in rows if r.notional_requested > hi):
        verdict = CAPACITY_LIMITED
    else:
        verdict = EDGE_SURVIVES
    return SizeSensitivity(tuple(rows), lo, hi, verdict)


# ── edge after costs ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class EdgeReport:
    n_priced: int
    n_unpriced: int
    mean_gross: float
    mean_net: float
    mean_cost: float
    share_cost_exceeds_gross: float
    unit: str
    verdict: str
    note: str = "point estimates; significance is assessed by research.integrity, not here"


def edge_after_costs(trades: Iterable[TradeEconomics], *, unit: str = "r", min_n: int = 30) -> EdgeReport:
    """``unit``: "r" (net R; needs risk_per_share) or "usd"."""
    ts = list(trades)
    priced = [t for t in ts if t.status in (FILLED, CAPPED)]
    if unit == "r":
        g = np.array([t.gross_r for t in priced], dtype=float)
        n_ = np.array([t.net_r for t in priced], dtype=float)
    elif unit == "usd":
        g = np.array([t.gross_pnl for t in priced], dtype=float)
        n_ = np.array([t.net_pnl for t in priced], dtype=float)
    else:
        raise ValueError("unit must be 'r' or 'usd'")
    ok = np.isfinite(g) & np.isfinite(n_)
    g, n_ = g[ok], n_[ok]
    k = int(len(g))
    if k == 0:
        return EdgeReport(0, len(ts), float("nan"), float("nan"), float("nan"), float("nan"), unit,
                          INCONCLUSIVE_VERDICT)
    mg, mn = float(g.mean()), float(n_.mean())
    share = float(np.mean((g - n_) > np.maximum(g, 0)))
    if k < min_n:
        verdict = INCONCLUSIVE_VERDICT
    elif mg <= 0:
        verdict = NO_GROSS_EDGE
    elif mn <= 0:
        verdict = EDGE_ERASED
    else:
        verdict = EDGE_SURVIVES
    return EdgeReport(k, len(ts) - k, mg, mn, mg - mn, share, unit, verdict)


# ── realised vs modelled ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class ComponentCalibration:
    n: int
    median: float
    p75: float
    p90: float
    verdict: str


@dataclass(frozen=True)
class CalibrationReport:
    fees: ComponentCalibration           # ratio realised / modelled
    slippage: ComponentCalibration       # difference realised - modelled, bps
    suggested_fee_multiplier: Optional[float]
    suggested_slippage_add_bps: Optional[float]
    applied: bool = False                # suggestions are never applied automatically


def _component(x: np.ndarray, *, min_n: int, centre: float, tol: float) -> ComponentCalibration:
    x = x[np.isfinite(x)]
    n = int(len(x))
    if n == 0:
        return ComponentCalibration(0, float("nan"), float("nan"), float("nan"), INCONCLUSIVE_VERDICT)
    med, p75, p90 = (float(np.quantile(x, q)) for q in (0.5, 0.75, 0.9))
    if n < min_n:
        v = INCONCLUSIVE_VERDICT
    elif med > centre + tol:
        v = UNDERESTIMATES
    elif med < centre - tol:
        v = OVERESTIMATES
    else:
        v = CALIBRATED
    return ComponentCalibration(n, med, p75, p90, v)


def fee_calibration(records: Iterable[Mapping[str, float]], *, min_n: int = 30, fee_tolerance: float = 0.10,
                    slippage_tolerance_bps: float = 1.0) -> CalibrationReport:
    """``records``: one per real (paper) fill with ``modelled_fees``,
    ``realised_fees``, ``modelled_slippage_bps`` and ``realised_slippage_bps``
    (adverse slippage vs the same reference price; negative = improvement).
    Records with modelled_fees <= 0 do not enter the fee ratio."""
    rs = list(records)
    mf = np.array([float(r.get("modelled_fees", np.nan)) for r in rs])
    rf = np.array([float(r.get("realised_fees", np.nan)) for r in rs])
    ms = np.array([float(r.get("modelled_slippage_bps", np.nan)) for r in rs])
    rsl = np.array([float(r.get("realised_slippage_bps", np.nan)) for r in rs])
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(mf > 0, rf / mf, np.nan)
    fees = _component(ratio, min_n=min_n, centre=1.0, tol=fee_tolerance)
    slip = _component(rsl - ms, min_n=min_n, centre=0.0, tol=slippage_tolerance_bps)
    mult = max(fees.p75, 1.0) if fees.verdict == UNDERESTIMATES else None
    add = max(slip.p75, 0.0) if slip.verdict == UNDERESTIMATES else None
    return CalibrationReport(fees, slip, mult, add)


# ── lane A labels ────────────────────────────────────────────────────────────
def _fill_bar(bars: Optional[pd.DataFrame], idx) -> Optional[FillBar]:
    if bars is None or idx is None or not np.isfinite(idx) or int(idx) < 0 or int(idx) >= len(bars):
        return None
    r = bars.iloc[int(idx)]
    halted = bool(r["halted"]) if "halted" in bars.columns else False
    return FillBar(float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"]),
                   halted)


def reprice_labels(labels: pd.DataFrame, daily_by_symbol: Mapping[str, pd.DataFrame], *, model: EconomicsModel,
                   notional: Optional[float] = None, risk_dollars: Optional[float] = None,
                   bars_by_symbol: Optional[Mapping[str, pd.DataFrame]] = None,
                   borrow_by_symbol: Optional[Mapping[str, BorrowInfo]] = None,
                   actions_by_symbol: Optional[Mapping[str, CorporateActions]] = None,
                   basis: str = "split_adjusted", stress: float = 2.0, liquidity_window: int = 20) -> pd.DataFrame:
    """Add economics columns to ``research.evidence`` labels.

    Only ``definitive`` labels are priced; every other status gets
    ``econ_status = "not_priced"``. Liquidity is measured as of each label's
    decision time. Size: exactly one of ``notional`` (USD per trade) or
    ``risk_dollars`` (USD at risk to the stop per trade)."""
    if (notional is None) == (risk_dollars is None):
        raise ValueError("give exactly one of notional or risk_dollars")
    out = labels.copy()
    cols: Dict[str, list] = {k: [] for k in ("econ_status", "econ_reasons", "econ_flags", "econ_shares",
                                              "econ_cost_bps", "econ_net_usd", "r_net_econ", "r_net_econ_stress")}
    stressed = model.scale(stress)
    for _, r in labels.iterrows():
        if r.get("status") != "definitive":
            for k, v in (("econ_status", "not_priced"), ("econ_reasons", "label_" + str(r.get("status"))),
                         ("econ_flags", ""), ("econ_shares", 0.0), ("econ_cost_bps", np.nan),
                         ("econ_net_usd", np.nan), ("r_net_econ", np.nan), ("r_net_econ_stress", np.nan)):
                cols[k].append(v)
            continue
        sym = r["symbol"]
        entry, stop = float(r["entry_price"]), float(r["stop_price"])
        risk = abs(entry - stop)
        shares = notional / entry if notional is not None else (risk_dollars / risk if risk > 0 else 0.0)
        bars = bars_by_symbol.get(sym) if bars_by_symbol else None
        spec = TradeSpec(sym, r["direction"], shares, entry, float(r["exit_price"]),
                         pd.Timestamp(r["entry_ts_utc"]).date(), pd.Timestamp(r["exit_ts_utc"]).date(),
                         risk_per_share=risk if risk > 0 else None,
                         stop_gap=bool(r.get("gap_fill", False)) and r.get("outcome") == "stop",
                         entry_bar=_fill_bar(bars, r.get("entry_idx")), exit_bar=_fill_bar(bars, r.get("exit_idx")))
        daily = daily_by_symbol.get(sym)
        liq = (liquidity_asof(daily, r["decision_ts_utc"], symbol=sym, window=liquidity_window)
               if daily is not None else missing_liquidity(sym, r["decision_ts_utc"]))
        kw = dict(entry_liq=liq, borrow=(borrow_by_symbol or {}).get(sym),
                  actions=(actions_by_symbol or {}).get(sym), basis=basis)
        te = price_trade(spec, model=model, **kw)
        ts = price_trade(spec, model=stressed, **kw)
        cols["econ_status"].append(te.status)
        cols["econ_reasons"].append("|".join(te.reasons))
        cols["econ_flags"].append("|".join(te.flags))
        cols["econ_shares"].append(te.shares_filled)
        cols["econ_cost_bps"].append(te.cost_bps)
        cols["econ_net_usd"].append(te.net_pnl if te.priced else np.nan)
        cols["r_net_econ"].append(te.net_r if te.priced else np.nan)
        cols["r_net_econ_stress"].append(ts.net_r if ts.priced else np.nan)
    for k, v in cols.items():
        out[k] = v
    out["econ_model"] = model.name
    return out


__all__ = ["EDGE_SURVIVES", "EDGE_ERASED", "NO_GROSS_EDGE", "CAPACITY_LIMITED", "CALIBRATED", "UNDERESTIMATES",
           "OVERESTIMATES", "INCONCLUSIVE_VERDICT", "SizeRow", "SizeSensitivity", "size_sensitivity", "EdgeReport",
           "edge_after_costs", "ComponentCalibration", "CalibrationReport", "fee_calibration", "reprice_labels"]
