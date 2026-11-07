"""M21 WP8 — template routing: what may become a paper order, and what stays shadow.

Hard rule for this milestone: only Template 1 (the scanner) may produce a
loop candidate. Templates 2-4 run in SHADOW: their signals are recorded
(``shadow.py``) and evaluated, never routed to a broker. ``LIVE_TEMPLATES``
is a constant, not configuration; widening it is a reviewed code change gated
by the WP9 approval record and the WP14 promotion model.

``merge_tradable`` guarantees one candidate per symbol per cycle across all
templates: opposite directions on one symbol drop BOTH (a conflict is never
resolved by guessing); same-direction duplicates keep Template 1 first, then
the higher score, then the strategy id, and report the rest.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from bot.paper_loop_d2.candidates import LoopCandidate
from bot.templates.core import APPENDIX_A, TEMPLATE_1, TemplateError, TemplateSignal, template_of

LIVE_TEMPLATES = frozenset({TEMPLATE_1})


class TemplateNotLive(TemplateError):
    pass


def is_live(template_id: str) -> bool:
    return template_id in LIVE_TEMPLATES


def to_loop_candidate(sig: TemplateSignal, *, score_100: float, candidate_id: Optional[str] = None
                      ) -> LoopCandidate:
    """A paper-loop candidate from a template signal. Refused for every
    template that is not live, and for a signal that is not an entry."""
    if not is_live(sig.template_id):
        raise TemplateNotLive("%s is shadow-only: no broker routing" % sig.template_id)
    if not sig.entry or sig.stop_loss is None or sig.take_profit is None:
        raise TemplateError("only an entry with a stop and a target can become a candidate")
    return LoopCandidate.build(symbol=sig.symbol, direction=sig.direction, score=score_100,
                               strategy_id=sig.strategy_id, entry_ref_price=sig.entry_ref_price,
                               stop_loss=sig.stop_loss, take_profit=sig.take_profit,
                               signal_ts_utc=sig.decision_ts_utc, candidate_id=candidate_id)


def route(signals: Sequence[TemplateSignal]) -> Tuple[List[TemplateSignal], List[TemplateSignal]]:
    """(live entries, shadow signals). Every non-live template, every setup
    without an entry, and every unknown template goes to shadow."""
    live, shadow = [], []
    for s in signals:
        (live if (is_live(s.template_id) and s.entry) else shadow).append(s)
    return live, shadow


def _priority(c: LoopCandidate) -> Tuple:
    t = template_of(c.strategy_id)
    rank = APPENDIX_A.index(t) if t in APPENDIX_A else len(APPENDIX_A)
    return (rank, -c.score, c.strategy_id)


def merge_tradable(cands: Sequence[LoopCandidate]) -> Tuple[List[LoopCandidate], List[str]]:
    by_sym = {}
    for c in cands:
        by_sym.setdefault(c.symbol, []).append(c)
    out, notes = [], []
    for sym in sorted(by_sym):
        cs = by_sym[sym]
        if len({c.direction for c in cs}) > 1:
            notes.append("conflict_dropped:%s:%s" % (sym, ",".join(sorted(c.strategy_id for c in cs))))
            continue
        cs = sorted(cs, key=_priority)
        out.append(cs[0])
        notes += ["duplicate_suppressed:%s:%s" % (sym, c.strategy_id) for c in cs[1:]]
    return out, notes


__all__ = ["LIVE_TEMPLATES", "TemplateNotLive", "is_live", "to_loop_candidate", "route", "merge_tradable"]
