"""M21 WP8 (canonical A15) — the strategy-template contract for the paper path.

One interface for all four Appendix A templates:

  Template 1  ``scanner``            the existing multi-timeframe scanner
                                     (``bot/scanner.py``); identity only here,
                                     its behaviour is unchanged
  Template 2  ``mean_reversion``     RSI oversold below the lower Bollinger band
  Template 3  ``breakout_momentum``  close breakout on volume with ATR expansion
  Template 4  ``pullback_in_trend``  fresh pullback below the fast EMA in an uptrend

``trend_confluence`` (the research genome's single-timeframe stand-in for the
scanner) is registered too, so the research and paper registries match one
to one; it is not Template 1.

Every template has an identity (id + version), typed parameters with bounds
(unknown, out-of-range or inconsistent values are refused, never clamped),
and a deterministic signal function on COMPLETED bars. The signal functions
reproduce ``research/genome/templates.py`` exactly (same M17 indicators, same
rules); ``test_m21_1_wp8_templates`` proves parity bar by bar.

A template signal carries its ``strategy_id`` (``tpl:<id>@v<version>#<params
digest>``), so per-template attribution survives into candidates, journal
orders, outcomes and the D3 metrics. Stops and targets come from one common
exit policy (ATR multiples, long only, stop < entry < target).

Nothing here places, routes or sizes an order. Routing is ``routing.py``:
templates 2-4 are SHADOW ONLY in this milestone.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

import numpy as np
import pandas as pd

from bot.backtesting import indicators as ind
from bot.templates.ids import TEMPLATE_1, template_of

SIGNAL_COLUMNS = ("entry", "exit", "atr", "score", "setup", "reject_reason")


class TemplateError(ValueError):
    pass


@dataclass(frozen=True)
class Param:
    kind: str                       # int | float | choice
    default: Any
    lo: Optional[float] = None
    hi: Optional[float] = None
    choices: Tuple[Any, ...] = ()

    def check(self, name: str, v: Any) -> Optional[str]:
        if self.kind == "int":
            if isinstance(v, bool) or not isinstance(v, (int, np.integer)):
                return "%s must be an int" % name
        elif self.kind == "float":
            if isinstance(v, bool) or not isinstance(v, (int, float, np.integer, np.floating)) \
                    or not math.isfinite(float(v)):
                return "%s must be a finite number" % name
        else:
            return None if v in self.choices else "%s must be one of %s" % (name, self.choices)
        if (self.lo is not None and v < self.lo) or (self.hi is not None and v > self.hi):
            return "%s=%r outside [%s, %s]" % (name, v, self.lo, self.hi)
        return None


@dataclass(frozen=True)
class TemplateDef:
    template_id: str
    version: int
    number: Optional[int]           # Appendix A number (1-4); None for the research stand-in
    description: str
    params: Mapping[str, Param]
    constraints: Tuple[Tuple[str, str], ...] = ()      # params[a] < params[b]
    func: Optional[Callable[[pd.DataFrame, Dict], pd.DataFrame]] = None   # None: runs elsewhere (scanner)


# ── signal functions (parity with research/genome/templates.py) ─────────────
def _reasons(index, *pairs) -> pd.Series:
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
    entry = _fresh(on)
    near = _fresh((votes == p["min_votes"] - 1) & ready)
    return pd.DataFrame({"entry": entry, "exit": ready & (c < es), "atr": _atr(bars),
                         "score": votes / 5.0 + (c / es - 1.0).fillna(0.0), "setup": entry | near,
                         "reject_reason": _reasons(bars.index, (near & ~entry, "one_vote_short"))}, index=bars.index)


def mean_reversion(bars: pd.DataFrame, p: Dict) -> pd.DataFrame:
    c = bars["close"].astype(float)
    r = ind.rsi(c, p["rsi_period"], mode=p["rsi_mode"])
    bb = ind.bollinger(c, p["bb_window"], p["bb_std"])
    oversold = (r < p["rsi_entry"]).fillna(False)
    entry = oversold & (c < bb["lower"])
    ex = (r > p["rsi_exit"]) | (c > bb["middle"])
    return pd.DataFrame({"entry": entry.fillna(False), "exit": ex.fillna(False), "atr": _atr(bars),
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


I, F, C = "int", "float", "choice"
TEMPLATES: Dict[str, TemplateDef] = {
    TEMPLATE_1: TemplateDef(TEMPLATE_1, 1, 1, "multi-timeframe scanner confluence (bot/scanner.py)", {}),
    "mean_reversion": TemplateDef("mean_reversion", 1, 2, "RSI oversold below the lower Bollinger band", {
        "rsi_period": Param(I, 14, 2, 21), "rsi_entry": Param(F, 30.0, 10.0, 45.0),
        "rsi_exit": Param(F, 55.0, 45.0, 75.0), "bb_window": Param(I, 20, 10, 40),
        "bb_std": Param(F, 2.0, 1.0, 3.0), "rsi_mode": Param(C, "sma_gain_loss", choices=("sma_gain_loss", "wilder"))},
        (("rsi_entry", "rsi_exit"),), mean_reversion),
    "breakout_momentum": TemplateDef("breakout_momentum", 1, 3, "close breakout on volume with ATR expansion", {
        "lookback": Param(I, 20, 5, 60), "exit_lookback": Param(I, 10, 3, 30),
        "vol_ratio_min": Param(F, 1.5, 1.0, 3.0), "atr_expansion": Param(F, 0.9, 0.5, 2.0)}, (), breakout_momentum),
    "pullback_in_trend": TemplateDef("pullback_in_trend", 1, 4, "fresh pullback below the fast EMA in an uptrend", {
        "ema_fast": Param(I, 20, 5, 60), "ema_slow": Param(I, 50, 20, 200),
        "rsi_max": Param(F, 50.0, 25.0, 70.0), "extend_atr": Param(F, 1.0, 0.25, 4.0)},
        (("ema_fast", "ema_slow"),), pullback_in_trend),
    "trend_confluence": TemplateDef("trend_confluence", 1, None, "single-timeframe trend confluence (research "
                                    "stand-in for the scanner)", {
        "ema_fast": Param(I, 20, 5, 60), "ema_slow": Param(I, 50, 20, 200), "htf_window": Param(I, 150, 50, 250),
        "rsi_floor": Param(F, 50.0, 40.0, 60.0), "min_votes": Param(I, 4, 3, 5)},
        (("ema_fast", "ema_slow"),), trend_confluence),
}
APPENDIX_A = (TEMPLATE_1, "mean_reversion", "breakout_momentum", "pullback_in_trend")


def get(template_id: str) -> TemplateDef:
    t = TEMPLATES.get(template_id)
    if t is None:
        raise TemplateError("unknown template %r" % template_id)
    return t


def validate_params(template_id: str, params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Defaults filled in; every problem reported together; never clamps."""
    t = get(template_id)
    params = dict(params or {})
    errors = ["unknown parameter %r" % k for k in sorted(params) if k not in t.params]
    out = {k: params.get(k, spec.default) for k, spec in t.params.items()}
    errors += [e for e in (spec.check(k, out[k]) for k, spec in t.params.items()) if e]
    if not errors:
        errors += ["%s must be < %s" % (a, b) for a, b in t.constraints if not out[a] < out[b]]
    if errors:
        raise TemplateError("; ".join(errors))
    return out


def params_digest(params: Mapping[str, Any]) -> str:
    blob = json.dumps({k: (float(v) if isinstance(v, (float, np.floating)) else v) for k, v in sorted(params.items())},
                      sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def strategy_id(template_id: str, params: Optional[Mapping[str, Any]] = None) -> str:
    t = get(template_id)
    return "tpl:%s@v%d#%s" % (t.template_id, t.version, params_digest(validate_params(template_id, params))[:8])


def signals(template_id: str, bars: pd.DataFrame, params: Optional[Mapping[str, Any]] = None) -> pd.DataFrame:
    t = get(template_id)
    if t.func is None:
        raise TemplateError("%s runs in its own module (bot/scanner.py), not here" % template_id)
    p = validate_params(template_id, params)
    out = t.func(bars, p)
    if list(out.columns) != list(SIGNAL_COLUMNS) or len(out) != len(bars):
        raise TemplateError("template %s broke the signal contract" % template_id)
    out = out.copy()
    out["entry"] = out["entry"].astype(bool)
    out["exit"] = out["exit"].astype(bool)
    out["setup"] = out["setup"].fillna(False).astype(bool) | out["entry"]
    rr = out["reject_reason"].astype(str).where(out["setup"] & ~out["entry"], "")
    rr[out["setup"] & ~out["entry"] & (rr == "")] = "rule_not_met"
    out["reject_reason"] = rr
    return out


# ── the common exit / risk policy ───────────────────────────────────────────
@dataclass(frozen=True)
class ExitPolicy:
    stop_atr: float = 2.0
    target_atr: float = 3.0

    def __post_init__(self):
        if not (self.stop_atr > 0 and self.target_atr > 0):
            raise TemplateError("stop_atr and target_atr must be > 0")

    def bracket(self, entry_ref: float, atr: float) -> Tuple[float, float]:
        if not (math.isfinite(entry_ref) and entry_ref > 0 and math.isfinite(atr) and atr > 0):
            raise TemplateError("entry and ATR must be finite and > 0")
        stop, target = entry_ref - self.stop_atr * atr, entry_ref + self.target_atr * atr
        if not (0 < stop < entry_ref < target):
            raise TemplateError("bracket invalid: stop %.4f entry %.4f target %.4f" % (stop, entry_ref, target))
        return stop, target


@dataclass(frozen=True)
class TemplateSignal:
    template_id: str
    template_version: int
    strategy_id: str
    params_digest: str
    symbol: str
    direction: str
    decision_ts_utc: str            # close of the signal bar (a completed bar)
    entry: bool                     # the rule fired (a tradable setup)
    setup: bool
    reject_reason: str
    entry_ref_price: float          # signal bar close; the fill is the next open
    stop_loss: Optional[float]
    take_profit: Optional[float]
    atr: float
    score: float
    digest: str = field(default="", compare=False)


def evaluate_last(template_id: str, completed_bars: pd.DataFrame, *, symbol: str, decision_ts_utc: str,
                  params: Optional[Mapping[str, Any]] = None,
                  exit_policy: ExitPolicy = ExitPolicy()) -> Optional[TemplateSignal]:
    """The template's verdict on the LAST row of ``completed_bars`` (the caller
    passes completed bars only; WP6 ``bar_completion.select_completed``).
    Returns None when the last bar is neither an entry nor a setup."""
    if completed_bars is None or len(completed_bars) == 0:
        return None
    p = validate_params(template_id, params)
    sig = signals(template_id, completed_bars, p).iloc[-1]
    if not (bool(sig["entry"]) or bool(sig["setup"])):
        return None
    t = get(template_id)
    close = float(completed_bars["close"].iloc[-1])
    atr = float(sig["atr"])
    stop = target = None
    reason = str(sig["reject_reason"])
    entry = bool(sig["entry"])
    if entry:
        try:
            stop, target = exit_policy.bracket(close, atr)
        except TemplateError:
            entry, reason = False, "bracket_unavailable"
    d = params_digest(p)
    out = TemplateSignal(t.template_id, t.version, "tpl:%s@v%d#%s" % (t.template_id, t.version, d[:8]), d,
                         symbol, "long", decision_ts_utc, entry, True, "" if entry else reason or "rule_not_met",
                         close, stop, target, atr, float(sig["score"]))
    body = {k: v for k, v in asdict(out).items() if k != "digest"}
    return TemplateSignal(**{**body, "digest": hashlib.sha256(json.dumps(body, sort_keys=True, default=str)
                                                                     .encode()).hexdigest()[:16]})


__all__ = ["SIGNAL_COLUMNS", "TEMPLATE_1", "APPENDIX_A", "TemplateError", "Param", "TemplateDef", "TEMPLATES",
           "get", "validate_params", "params_digest", "strategy_id", "template_of", "signals", "ExitPolicy",
           "TemplateSignal", "evaluate_last", "trend_confluence", "mean_reversion", "breakout_momentum",
           "pullback_in_trend"]
