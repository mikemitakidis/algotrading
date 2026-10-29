"""M21 WP8 — run templates 2-4 in shadow on completed daily bars.

For each symbol, the WP6 completed-bar policy selects the newest COMPLETED
daily bar (a forming bar is never used). Each shadow template judges that
bar; the verdict (entry, or a setup its own filters rejected) is recorded in
the ``ShadowStore`` with the bar's real session close as the decision time.
Re-running on the same completed bar records nothing new.

Shadow only: live templates are refused here, nothing becomes a loop
candidate, and nothing is sent to a broker.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Sequence

from bot.bar_completion import select_completed
from bot.templates.core import TemplateError, evaluate_last
from bot.templates.routing import is_live
from bot.templates.shadow import ShadowStore

SHADOW_TEMPLATES = ("mean_reversion", "breakout_momentum", "pullback_in_trend")


def run_template_shadow(store: ShadowStore, daily_by_symbol: Mapping[str, Any], completion, *, now: datetime,
                        templates: Sequence[str] = SHADOW_TEMPLATES,
                        params: Optional[Mapping[str, Mapping[str, Any]]] = None) -> Dict[str, Any]:
    """``completion``: a ``CompletedBarPolicy`` (classify + calendar)."""
    live = [t for t in templates if is_live(t)]
    if live:
        raise ValueError("live templates cannot run in the shadow runner: %s" % live)
    out = {"recorded": 0, "already_recorded": 0, "no_verdict": 0, "skipped": []}
    for sym in sorted(daily_by_symbol):
        df = daily_by_symbol[sym]
        sel, why = select_completed(df, "1D", completion.classify)
        if sel is None:
            out["skipped"].append("%s:%s" % (sym, why.reason))
            continue
        verdict, close = completion.classify("1D", sel.index[-1])
        decision = close.isoformat() if hasattr(close, "isoformat") else str(close)
        for tid in templates:
            try:
                sig = evaluate_last(tid, sel, symbol=sym, decision_ts_utc=decision,
                                    params=(params or {}).get(tid))
            except (TemplateError, KeyError, ValueError) as exc:
                out["skipped"].append("%s:%s:%s" % (sym, tid, type(exc).__name__))
                continue
            if sig is None:
                out["no_verdict"] += 1
            elif store.record(sig, now=now):
                out["recorded"] += 1
            else:
                out["already_recorded"] += 1
    return out


__all__ = ["SHADOW_TEMPLATES", "run_template_shadow"]
