"""research.genome.templates — the four starter templates (canonical A15).

Each template turns one symbol's bars into causal per-bar signals, decided at
a bar's CLOSE and executed by the engine at the NEXT bar's open:

    entry  bool   enter long at the next open (if capacity allows)
    exit   bool   template exit at the next open (if a position is open)
    atr    float  ATR at the signal bar (sizes the stop and target)
    score  float  signal strength, used to rank entries when capacity binds

Indicators come from the production M17 module (``bot.backtesting.indicators``),
so a template's indicator values are the ones the backtester already uses.

1. ``trend_confluence``: trend-following confluence. Long when at least
   `min_votes` of five trend votes agree. The votes are close > fast EMA,
   fast EMA > slow EMA, close > long SMA, MACD histogram > 0 and RSI >
   floor. The long SMA (`htf_window`) stands in for a higher timeframe on
   single-timeframe data. True multi-timeframe confluence on real data is
   M17's ``scanner_replica``.
2. ``mean_reversion``: RSI below a level AND close below the lower
   Bollinger band. Exit when RSI recovers or close regains the middle band.
3. ``breakout_momentum``: close above the highest close of the prior
   `lookback` bars, on volume ≥ `vol_ratio_min` × average and an
   expanding ATR. Exit on a close below the prior `exit_lookback`-bar low
   close.
4. ``pullback_in_trend``: in an uptrend (fast EMA > slow EMA), a fresh
   pullback below the fast EMA with RSI ≤ `rsi_max`. Exit when the trend
   breaks or price extends `extend_atr` ATR above the fast EMA.

``sma_crossover`` adapts the existing M17 strategy unchanged. It is the M17
parity reference, not a fifth template.

Diagnostics (second review, PR #11): each template also reports ``setup``
(its primary trigger fired) and ``reject_reason`` (which of its own filters
failed when the setup did not become an entry). ``entry`` is unchanged and
always implies ``setup``; the engine records setups without entries as
``rejected_by_rule`` decisions. They are NOT trades and are serially
correlated (a persistent condition repeats on consecutive bars).
"""
from __future__ import annotations

from typing import Callable, Dict

import numpy as np
import pandas as pd

from bot.backtesting import indicators as ind
from research.genome.schema import Genome, ParamSpec, TemplateSpec

SIGNAL_COLUMNS = ("entry", "exit", "atr", "score")
DIAGNOSTIC_COLUMNS = ("setup", "reject_reason")


def _reasons(index, *pairs) -> pd.Series:
    """First failing filter per row: pairs of (failed: bool Series, reason)."""
    out = pd.Series("", index=index, dtype=object)
    for failed, why in reversed(pairs):
        out = out.where(~failed.fillna(True).astype(bool), why)
    return out


def _atr(bars, period=14):
    return ind.atr(bars["high"].astype(float), bars["low"].astype(float), bars["close"].astype(float), period)


def _fresh(cond: pd.Series) -> pd.Series:
    c = cond.fillna(False).astype(bool)
    return c & ~c.shift(1, fill_value=False).astype(bool)


def trend_confluence(bars: pd.DataFrame, p: Dict) -> pd.DataFrame:
    c = bars["close"].astype(float)
    ef, es = ind.ema(c, p["ema_fast"]), ind.ema(c, p["ema_slow"])
    htf = ind.sma(c, p["htf_window"])
    hist = ind.macd(c)["hist"]
    r = ind.rsi(c, 14)
    votes = ((c > ef).astype(int) + (ef > es).astype(int) + (c > htf).astype(int) + (hist > 0).astype(int)
             + (r > p["rsi_floor"]).astype(int))
    ready = ef.notna() & es.notna() & htf.notna() & hist.notna() & r.notna()
    on = (votes >= p["min_votes"]) & ready
    a = _atr(bars)
    entry = _fresh(on)
    near = _fresh((votes == p["min_votes"] - 1) & ready)          # one vote short of the rule
    return pd.DataFrame({"entry": entry, "exit": ready & (c < es), "atr": a,
                         "score": votes / 5.0 + (c / es - 1.0).fillna(0.0),
                         "setup": entry | near, "reject_reason": _reasons(bars.index, (near & ~entry, "one_vote_short"))},
                        index=bars.index)


def mean_reversion(bars: pd.DataFrame, p: Dict) -> pd.DataFrame:
    c = bars["close"].astype(float)
    r = ind.rsi(c, p["rsi_period"], mode=p["rsi_mode"])
    bb = ind.bollinger(c, p["bb_window"], p["bb_std"])
    oversold = (r < p["rsi_entry"]).fillna(False)
    entry = oversold & (c < bb["lower"])
    ex = (r > p["rsi_exit"]) | (c > bb["middle"])
    a = _atr(bars)
    return pd.DataFrame({"entry": entry.fillna(False), "exit": ex.fillna(False), "atr": a,
                         "score": ((p["rsi_entry"] - r) / p["rsi_entry"]).fillna(0.0),
                         "setup": oversold | entry.fillna(False),
                         "reject_reason": _reasons(bars.index, (oversold & ~(c < bb["lower"]), "not_below_lower_band"))},
                        index=bars.index)


def breakout_momentum(bars: pd.DataFrame, p: Dict) -> pd.DataFrame:
    c = bars["close"].astype(float)
    prior_hi = c.shift(1).rolling(p["lookback"], min_periods=p["lookback"]).max()
    prior_lo = c.shift(1).rolling(p["exit_lookback"], min_periods=p["exit_lookback"]).min()
    vr = ind.volume_ratio(bars["volume"].astype(float), 20)
    a = _atr(bars)
    expand = a / a.rolling(50, min_periods=50).mean()
    brk = (c > prior_hi).fillna(False)
    vol_ok, exp_ok = (vr >= p["vol_ratio_min"]), (expand >= p["atr_expansion"])
    entry = (brk & vol_ok & exp_ok).fillna(False)
    return pd.DataFrame({"entry": entry, "exit": (c < prior_lo).fillna(False), "atr": a,
                         "score": ((c / prior_hi - 1.0) / (a / c)).fillna(0.0), "setup": brk | entry,
                         "reject_reason": _reasons(bars.index, (brk & ~vol_ok, "volume_below_min"),
                                                   (brk & ~exp_ok, "atr_not_expanding"))}, index=bars.index)


def pullback_in_trend(bars: pd.DataFrame, p: Dict) -> pd.DataFrame:
    c = bars["close"].astype(float)
    ef, es = ind.ema(c, p["ema_fast"]), ind.ema(c, p["ema_slow"])
    r = ind.rsi(c, 14)
    a = _atr(bars)
    dip = (ef > es) & (c < ef)
    pull = dip & (r <= p["rsi_max"])
    ex = (ef < es) | (c > ef + p["extend_atr"] * a)
    entry = _fresh(pull)
    setup = _fresh(dip) | entry
    return pd.DataFrame({"entry": entry, "exit": ex.fillna(False), "atr": a,
                         "score": ((ef - c) / a).fillna(0.0), "setup": setup,
                         "reject_reason": _reasons(bars.index, (setup & ~entry, "rsi_above_max"))}, index=bars.index)


def sma_crossover(bars: pd.DataFrame, p: Dict) -> pd.DataFrame:
    from bot.backtesting.strategy import SIG_ENTRY, SIG_EXIT, SmaCrossoverStrategy
    sig = SmaCrossoverStrategy({"fast_window": p["fast_window"], "slow_window": p["slow_window"]}).run(bars)
    return pd.DataFrame({"entry": sig["signal"] == SIG_ENTRY, "exit": sig["signal"] == SIG_EXIT,
                         "atr": _atr(bars), "score": 0.0}, index=bars.index)


I, F, C = "int", "float", "choice"
TEMPLATES: Dict[str, TemplateSpec] = {
    "trend_confluence": TemplateSpec("trend_confluence", "trend-following confluence of five trend votes", {
        "ema_fast": ParamSpec(I, 20, 5, 60, step=5), "ema_slow": ParamSpec(I, 50, 20, 200, step=10),
        "htf_window": ParamSpec(I, 150, 50, 250, step=25), "rsi_floor": ParamSpec(F, 50.0, 40.0, 60.0, step=5.0),
        "min_votes": ParamSpec(I, 4, 3, 5, step=1)}, constraints=(("ema_fast", "ema_slow"),)),
    "mean_reversion": TemplateSpec("mean_reversion", "RSI oversold below the lower Bollinger band", {
        "rsi_period": ParamSpec(I, 14, 2, 21, step=2), "rsi_entry": ParamSpec(F, 30.0, 10.0, 45.0, step=5.0),
        "rsi_exit": ParamSpec(F, 55.0, 45.0, 75.0, step=5.0), "bb_window": ParamSpec(I, 20, 10, 40, step=5),
        "bb_std": ParamSpec(F, 2.0, 1.0, 3.0, step=0.25),
        "rsi_mode": ParamSpec(C, "sma_gain_loss", choices=("sma_gain_loss", "wilder"))},
        constraints=(("rsi_entry", "rsi_exit"),)),
    "breakout_momentum": TemplateSpec("breakout_momentum", "close breakout on volume with ATR expansion", {
        "lookback": ParamSpec(I, 20, 5, 60, step=5), "exit_lookback": ParamSpec(I, 10, 3, 30, step=2),
        "vol_ratio_min": ParamSpec(F, 1.5, 1.0, 3.0, step=0.25),
        "atr_expansion": ParamSpec(F, 0.9, 0.5, 2.0, step=0.1)}),
    "pullback_in_trend": TemplateSpec("pullback_in_trend", "fresh pullback below the fast EMA in an uptrend", {
        "ema_fast": ParamSpec(I, 20, 5, 60, step=5), "ema_slow": ParamSpec(I, 50, 20, 200, step=10),
        "rsi_max": ParamSpec(F, 50.0, 25.0, 70.0, step=5.0), "extend_atr": ParamSpec(F, 1.0, 0.25, 4.0, step=0.25)},
        constraints=(("ema_fast", "ema_slow"),)),
    "sma_crossover": TemplateSpec("sma_crossover", "M17 SMA crossover (parity reference)", {
        "fast_window": ParamSpec(I, 20, 2, 100, step=5), "slow_window": ParamSpec(I, 50, 5, 300, step=10)},
        constraints=(("fast_window", "slow_window"),)),
}
STARTER_TEMPLATES = ("trend_confluence", "mean_reversion", "breakout_momentum", "pullback_in_trend")

_FUNCS: Dict[str, Callable[[pd.DataFrame, Dict], pd.DataFrame]] = {
    "trend_confluence": trend_confluence, "mean_reversion": mean_reversion, "breakout_momentum": breakout_momentum,
    "pullback_in_trend": pullback_in_trend, "sma_crossover": sma_crossover}


def signals(g: Genome, bars: pd.DataFrame) -> pd.DataFrame:
    out = _FUNCS[g.template](bars, g.p)
    cols = list(out.columns)
    if (cols[:len(SIGNAL_COLUMNS)] != list(SIGNAL_COLUMNS) or not set(cols[len(SIGNAL_COLUMNS):]) <= set(DIAGNOSTIC_COLUMNS)
            or len(out) != len(bars)):
        raise RuntimeError("template %s broke the signal contract" % g.template)
    out = out.copy()
    out["entry"] = out["entry"].astype(bool)
    out["exit"] = out["exit"].astype(bool)
    if "setup" in out.columns:
        out["setup"] = out["setup"].fillna(False).astype(bool) | out["entry"]
        rr = out["reject_reason"].astype(str) if "reject_reason" in out.columns else pd.Series("", index=out.index)
        out["reject_reason"] = rr.where(out["setup"] & ~out["entry"], "")
        out.loc[out["setup"] & ~out["entry"] & (out["reject_reason"] == ""), "reject_reason"] = "rule_not_met"
    if not g.exits.signal_exit:
        out["exit"] = False
    return out


__all__ = ["SIGNAL_COLUMNS", "DIAGNOSTIC_COLUMNS", "TEMPLATES", "STARTER_TEMPLATES", "signals", "trend_confluence", "mean_reversion",
           "breakout_momentum", "pullback_in_trend", "sma_crossover"]
