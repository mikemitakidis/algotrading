"""M21 WP5 — truthful M19 evidence wiring and research-ranked selection.

Pipeline (read-only, offline-testable):

    scanner signal + per-timeframe bar as-of record
      + observed evidence (session calendar, completed daily bars,
        corporate-action record, price basis)        -> A01 truth contract
      -> M19 score (RESEARCH profile)                  -> research rank
      -> selection verdict (fail closed)

Every input is measured from supplied evidence or reported UNKNOWN; nothing
is defaulted. M19 never authorises an order (``execution_eligible`` is False
on every result) and the selection verdict is only a research pre-filter:
the M14 risk gate still decides every paper entry afterwards.

What may pass the PRE-RISK research selection (``ScoredCandidate.selectable``)
-----------------------------------------------------------------------------
Only these M19 outcomes are expected before M14 runs and are accepted:
  * ``missing_context_key`` limited to
      - ``risk_preview.risk_authority_status`` (M14 decides after selection),
      - ``ml_context.production_thinness_status`` (no production ML model
        exists; ML is not part of paper selection);
  * MANUAL_REVIEW ``model_readiness_failed`` / ``calibration_unavailable``
    (research profile: no model, declared), and ``short_side_manual_review``
    only when shorts are allowed.
ANY other block — stale or incomplete bars, schema mismatch, too few
timeframes, missing or low liquidity, unknown / suspected / adjusted
corporate-action state, a verified M14 block, an invalid context value —
makes the candidate NOT selectable, whatever its research rank.

Ranking is deterministic: research rank descending, then symbol, direction.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from bot.scoring_truth import TruthPolicy, parse_utc, suspect_split
from bot.scoring_truth.corporate_actions import CorporateActionRecord, PriceBasis
from bot.scoring_truth.evidence_records import (
    CalendarFit, LiquidityTruth, measure_liquidity,
)
from bot.scoring_truth.sessions import SessionCalendar
from bot.signal_scoring import ScoringProfile, default_config, score_candidate
from bot.signal_scoring.gates import _missing_keys, evaluate_hard_gates
from tools.signal_scoring.scanner_bridge import TruthfulScore, enrich_signal_with_truth

PRE_RISK_MISSING_OK = frozenset({
    "risk_preview.risk_authority_status",
    "ml_context.production_thinness_status",
})
REVIEW_OK = frozenset({"model_readiness_failed", "calibration_unavailable"})
SHORT_REVIEW = "short_side_manual_review"
LOOKBACK_SESSIONS = 60          # daily closes inspected by the split detector


@dataclass(frozen=True)
class CycleEvidence:
    """Everything observed for one scan cycle (one decision time)."""

    evaluated_at_utc: str
    calendar: Optional[SessionCalendar]
    calendar_fit: CalendarFit
    price_basis: str                                   # PriceBasis value
    price_basis_source: str
    daily_bars: Mapping[str, Sequence[Tuple[Any, Any, Any]]] = field(default_factory=dict)
    corporate_actions: Mapping[str, CorporateActionRecord] = field(default_factory=dict)
    bar_asof: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    policy: TruthPolicy = field(default_factory=TruthPolicy)
    risk_decisions: Mapping[Tuple[str, str], Any] = field(default_factory=dict)
    risk_decision_store: Any = None


@dataclass(frozen=True)
class EvidenceNote:
    item: str
    status: str            # MEASURED | UNKNOWN | DECLARED
    detail: str


@dataclass(frozen=True)
class ScoredCandidate:
    signal: Mapping[str, Any]
    truthful: TruthfulScore
    research_rank: float
    selectable: bool
    block_reasons: Tuple[str, ...]
    accepted_pre_risk: Tuple[str, ...]
    evidence: Tuple[EvidenceNote, ...]

    @property
    def symbol(self) -> str:
        return str(self.signal.get("symbol"))

    @property
    def direction(self) -> str:
        return str(self.signal.get("direction"))

    def to_report_row(self) -> dict:
        sc = self.truthful.scored
        return {"symbol": self.symbol, "direction": self.direction,
                "research_rank": round(self.research_rank, 4),
                "m19_bucket": getattr(sc.decision_bucket, "value", str(sc.decision_bucket)),
                "m19_final_score_100": sc.final_score_100,
                "selectable": self.selectable, "block_reasons": list(self.block_reasons),
                "accepted_pre_risk": list(self.accepted_pre_risk),
                "unknown_inputs": list(self.truthful.truth.unknown_fields),
                "observations": self.truthful.truth.observation_states(),
                "corporate_action": self.truthful.truth.corporate_action.state.value,
                "evidence": [n.__dict__ for n in self.evidence],
                "execution_eligible": False}


def _daily_closes_before(bars, ev, calendar, n):
    """Closes of daily bars whose session closed at or before ``ev`` (causal),
    most recent ``n``. None if the series cannot be read."""
    out = []
    try:
        for start, close, _vol in bars:
            st = parse_utc(start)
            d = calendar.local_date(st + timedelta(hours=12))
            b = calendar.session_bounds(d)
            if b is None or b[1] > ev:
                continue
            out.append((b[1], close))
    except Exception:
        return None
    out.sort(key=lambda x: x[0])
    return [c for _t, c in out[-n:]]


def _lookback_start(calendar, ev, sessions):
    d = calendar.local_date(calendar.previous_close(ev))
    seen = 0
    while True:
        b = calendar.session_bounds(d)
        if b is not None:
            seen += 1
            if seen >= sessions:
                return b[0]
        d -= timedelta(days=1)


def score_with_evidence(signal: Mapping[str, Any], ce: CycleEvidence, *,
                        allow_short: bool = False,
                        profile: ScoringProfile = ScoringProfile.RESEARCH
                        ) -> ScoredCandidate:
    sym = str(signal.get("symbol") or "").strip().upper()
    side = signal.get("direction")
    notes: List[EvidenceNote] = []
    ev = parse_utc(ce.evaluated_at_utc)
    cal = ce.calendar if ce.calendar_fit.usable else None
    notes.append(EvidenceNote("session_calendar", "MEASURED" if cal else "UNKNOWN",
                              ce.calendar_fit.reason))
    # liquidity (completed daily bars only)
    bars = ce.daily_bars.get(sym)
    if bars is None:
        liq = LiquidityTruth(None, 0, 0, "no_daily_bars_observed")
    else:
        liq = measure_liquidity(bars, evaluated_at=ev, calendar=cal)
    notes.append(EvidenceNote("avg_dollar_volume_20d",
                              "MEASURED" if liq.measured else "UNKNOWN", liq.reason))
    # split detector (causal closes) + corporate-action record
    detector = None
    lookback = None
    if cal is not None and bars is not None:
        closes = _daily_closes_before(bars, ev, cal, LOOKBACK_SESSIONS)
        detector = suspect_split(closes) if closes else None
        try:
            lookback = _lookback_start(cal, ev, LOOKBACK_SESSIONS).isoformat()
        except Exception:
            lookback = None
    notes.append(EvidenceNote("split_detector",
                              "MEASURED" if detector is not None else "UNKNOWN",
                              {True: "split_like_jump", False: "no_jump_found(not proof)",
                               None: "not_run"}[detector]))
    rec = ce.corporate_actions.get(sym)
    notes.append(EvidenceNote("corporate_action_record",
                              "MEASURED" if rec is not None and rec.verified else "UNKNOWN",
                              "verified %s" % rec.source if rec is not None and rec.verified
                              else ("unverified record" if rec is not None else "no record")))
    notes.append(EvidenceNote("price_basis",
                              "MEASURED" if ce.price_basis != PriceBasis.UNKNOWN.value
                              else "UNKNOWN", ce.price_basis_source))
    asof = ce.bar_asof.get(sym)
    notes.append(EvidenceNote("bar_asof", "MEASURED" if asof else "UNKNOWN",
                              "scanner as-of record" if asof else "no as-of record"))
    ref = ce.risk_decisions.get((sym, side))
    kwargs = dict(evaluated_at_utc=ev.isoformat(), bar_asof=asof, calendar=cal,
                  risk_decision=ref, risk_decision_store=ce.risk_decision_store,
                  price_basis=ce.price_basis, detector_suspected=detector,
                  corporate_action_record=rec, lookback_start_utc=lookback,
                  avg_dollar_volume=liq.avg_dollar_volume_20d,
                  derive_liquidity_from_signal=False, policy=ce.policy)
    ci, truth = enrich_signal_with_truth(signal, **kwargs)
    cfg = default_config(profile=profile)
    scored = score_candidate(ci, cfg)
    ts = TruthfulScore(scored=scored, truth=truth)
    if ts.execution_eligible is not False:                 # raises if not False
        raise AssertionError("M19 must never be execution eligible")
    rank = ts.research_rank_score
    if not math.isfinite(rank):
        rank = 0.0
    # selection verdict from the same gate evaluation M19 used
    gr = evaluate_hard_gates(ci, cfg)
    blocks, accepted = [], []
    for m in sorted(set(_missing_keys(ci))):
        (accepted if m in PRE_RISK_MISSING_OK else blocks).append("missing:%s" % m)
    for code in gr.block_reasons:
        if code != "missing_context_key":                  # itemised above
            blocks.append(code)
    for code in gr.manual_review_reasons:
        if code in REVIEW_OK or (code == SHORT_REVIEW and allow_short):
            accepted.append("review:%s" % code)
        elif code == SHORT_REVIEW:
            blocks.append("short_disabled")
        else:
            blocks.append("review:%s" % code)              # unknown review: not accepted
    if side == "short" and not allow_short:
        blocks.append("short_disabled")
    if ce.policy.bar_basis == "completed" and cal is not None:
        # C01 §11 first-interval rule: during a session an entry needs a
        # completed 15m bar from the CURRENT session (not yesterday's close).
        from tools.signal_scoring.completed_bars import CompletedBarPolicy
        ok, why = CompletedBarPolicy(cal, ev).first_interval_ready((asof or {}).get("15m"))
        if not ok:
            blocks.append(why)
    blocks = tuple(dict.fromkeys(blocks))
    return ScoredCandidate(signal=dict(signal), truthful=ts, research_rank=float(rank),
                           selectable=not blocks, block_reasons=blocks,
                           accepted_pre_risk=tuple(dict.fromkeys(accepted)),
                           evidence=tuple(notes))


def rank_candidates(signals: Sequence[Mapping[str, Any]], ce: CycleEvidence, *,
                    allow_short: bool = False) -> List[ScoredCandidate]:
    """Score every signal; return ALL (selectable first), deterministically
    ordered by (selectable desc, research rank desc, symbol, direction)."""
    out = [score_with_evidence(s, ce, allow_short=allow_short) for s in signals]
    out.sort(key=lambda c: (not c.selectable, -c.research_rank, c.symbol, c.direction))
    return out


def research_report(ranked: Sequence[ScoredCandidate]) -> dict:
    """Research report: every candidate, including blocked ones, with reasons."""
    rows = [c.to_report_row() for c in ranked]
    reasons: Dict[str, int] = {}
    for c in ranked:
        for r in c.block_reasons:
            reasons[r] = reasons.get(r, 0) + 1
    return {"candidates": rows, "selectable": sum(1 for c in ranked if c.selectable),
            "blocked": sum(1 for c in ranked if not c.selectable),
            "block_reason_counts": dict(sorted(reasons.items())),
            "note": "research ranking only; M14 decides every paper entry; "
                    "execution_eligible is always False"}


# ── runtime evidence assembly (fetchers injected; tests use stubs) ──────────

def runtime_price_basis(provider_name: Optional[str]) -> Tuple[str, str]:
    """The price basis the ACTIVE provider's own request establishes.

    yfinance requests ``auto_adjust=True`` (retroactively adjusted). For
    Alpaca the basis is RAW only when the provider module declares that it
    sends ``adjustment=raw`` itself (``BAR_PRICE_ADJUSTMENT``); otherwise the
    request relies on the API's default, which this code cannot verify, and
    the basis stays UNKNOWN."""
    name = str(provider_name or "").strip().lower()
    if name == "yfinance":
        return PriceBasis.TOTAL_RETURN_ADJUSTED.value, "yfinance auto_adjust=True"
    if name == "alpaca":
        try:
            from bot.providers import alpaca_provider
            adj = getattr(alpaca_provider, "BAR_PRICE_ADJUSTMENT", None)
        except Exception:                                  # pragma: no cover
            adj = None
        if adj == "raw":
            return PriceBasis.RAW.value, "alpaca_provider requests adjustment=raw"
        return (PriceBasis.UNKNOWN.value,
                "alpaca request sends no adjustment parameter (default unverified)")
    return PriceBasis.UNKNOWN.value, "unknown provider %r" % name


def _frame_rows(df) -> List[Tuple[str, float, float]]:
    rows = []
    for ts, r in df.iterrows():
        rows.append((ts.isoformat(), r.get("close"), r.get("volume")))
    return rows


def build_cycle_evidence(*, signals: Sequence[Mapping[str, Any]],
                         meta: Mapping[str, Any], now_utc, fetch_daily,
                         fetch_calendar, fetch_actions, provider_name: str,
                         policy: Optional[TruthPolicy] = None, live: bool = True,
                         calendar_days_back: int = 150, calendar_days_ahead: int = 10,
                         risk_decision_store: Any = None,
                         calendar_fetch: Any = None) -> CycleEvidence:
    """Observe the evidence for one cycle. Any fetch failure leaves the
    corresponding evidence UNKNOWN (and candidates fail closed); nothing is
    substituted. ``fetch_calendar(start, end, now_utc=)`` returns a
    ``CalendarFetch``; ``fetch_actions(symbols, start, end, now_utc=)`` a
    ``CorporateActionsFetch``; ``fetch_daily(symbols)`` a {symbol: OHLCV frame}.
    ``calendar_fetch`` reuses a snapshot already fetched for this cycle (the
    one the completed-bar scanner used) instead of fetching again."""
    from bot.scoring_truth.evidence_records import (
        CorporateActionEvent, EvidenceError, calendar_fit_for_decision,
        corporate_action_record_from_events, session_calendar_from_rows,
    )
    ev = parse_utc(now_utc)
    symbols = sorted({str(s.get("symbol")).strip().upper() for s in signals
                      if s.get("symbol")})
    today = ev.date()
    start = today - timedelta(days=calendar_days_back)
    end = today + timedelta(days=calendar_days_ahead)
    cal, fit = None, CalendarFit(False, "calendar_not_fetched")
    try:
        cf = calendar_fetch if calendar_fetch is not None else \
            fetch_calendar(start, end, now_utc=ev)
        if cf.ok:
            cal = session_calendar_from_rows(cf.rows, coverage_start=cf.start,
                                             coverage_end=cf.end, source=cf.source,
                                             fetched_at_utc=cf.fetched_at_utc)
            fit = calendar_fit_for_decision(cal, fetched_at_utc=cf.fetched_at_utc,
                                            evaluated_at=ev, live=live)
        else:
            fit = CalendarFit(False, "calendar_fetch_%s: %s" % (cf.status, cf.note))
    except EvidenceError as exc:
        cal, fit = None, CalendarFit(False, "calendar_rows_refused: %s" % exc)
    except Exception as exc:
        cal, fit = None, CalendarFit(False, "calendar_error: %s" % type(exc).__name__)
    daily: Dict[str, Sequence] = {}
    if symbols:
        try:
            frames = fetch_daily(symbols) or {}
            for sym, df in frames.items():
                daily[str(sym).upper()] = _frame_rows(df)
        except Exception:
            daily = {}
    records: Dict[str, CorporateActionRecord] = {}
    if symbols and cal is not None and fit.usable:
        try:
            lb = _lookback_start(cal, ev, LOOKBACK_SESSIONS)
            fa = fetch_actions(symbols, lb.date(), today, now_utc=ev)
            for sym in symbols:
                evs = [CorporateActionEvent(r.symbol, r.action_type, r.ex_date, r.detail)
                       for r in fa.rows_for(sym)]
                records[sym] = corporate_action_record_from_events(
                    sym, evs, window_start=lb, window_end=ev, fetch_complete=fa.complete,
                    source=fa.source, calendar=cal)
        except Exception:
            records = {}
    basis, basis_src = runtime_price_basis(provider_name)
    asof = meta.get("bar_asof") if isinstance(meta, Mapping) else None
    return CycleEvidence(evaluated_at_utc=ev.isoformat(), calendar=cal, calendar_fit=fit,
                         price_basis=basis, price_basis_source=basis_src,
                         daily_bars=daily, corporate_actions=records,
                         bar_asof=dict(asof or {}), policy=policy or TruthPolicy(),
                         risk_decision_store=risk_decision_store)


def loop_candidates(ranked: Sequence[ScoredCandidate], *, strategy_prefix: str = "scanner"):
    """Paper-loop candidates from SELECTABLE research-ranked candidates only.
    The candidate score is the research rank; M14 still decides every entry."""
    from bot.paper_loop_d2.candidates import InvalidCandidate, LoopCandidate
    out, notes = [], []
    for c in ranked:
        if not c.selectable:
            notes.append("not_selectable:%s:%s" % (c.symbol, ",".join(c.block_reasons)))
            continue
        s = c.signal
        ver = s.get("strategy_version")
        try:
            out.append(LoopCandidate.build(
                symbol=s.get("symbol"), direction=s.get("direction"), score=c.research_rank,
                strategy_id=s.get("strategy_id") or "%s_v%s" % (
                    strategy_prefix, ver if ver is not None else "unknown"),
                entry_ref_price=s.get("entry_price") or s.get("price"),
                stop_loss=s.get("stop_loss"), take_profit=s.get("target_price"),
                signal_ts_utc=s.get("timestamp")))
        except InvalidCandidate as exc:
            notes.append("invalid:%s:%s" % (c.symbol, exc))
    return out, notes
