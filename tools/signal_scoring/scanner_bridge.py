#!/usr/bin/env python3
"""M21.1 — Scoring bridge (read-only): scanner signal -> truthful M19 input.

A01 contract (bot/scoring_truth): every M19 gate field is either MEASURED from
evidence the caller actually has, DECLARED (a true fact about this pipeline,
e.g. "no calibration exists"), or UNKNOWN — and UNKNOWN keys are REMOVED from
the M19 input, so M19 fails closed with ``missing_context_key``. Nothing is
defaulted to a passing value. In particular (replacing the pre-A01 defaults):

  - data freshness / stale flag: measured from the scanner's per-timeframe
    bar as-of record (``scan_cycle`` meta ``bar_asof``) against an injected
    exchange session calendar at an explicit evaluation time; else UNKNOWN
  - schema_match: measured (signal fields + bar records); else UNKNOWN
  - available_timeframes: per-symbol count of VALID observations (not the
    scanner's cycle-global count); else UNKNOWN
  - risk_authority_status: 'ok' ONLY from a validated, matching, unexpired
    approval (``RiskDecisionRef``) from an accepted source; any matching
    block is 'blocked'; otherwise UNKNOWN. Never 'ok' by default.
  - risk_preview_available: as computed by the M19 adapter (reward/risk
    derivable from the signal's levels); never forced True
  - price basis / corporate actions: from the provider's price basis plus
    verified corporate-action evidence; a split detector that finds nothing
    is NOT proof of safety (UNVERIFIED -> UNKNOWN)
  - production_thinness_status: only from a supplied registry check
  - calibration: none exists (DECLARED False / None)

Consequence: without a risk decision, a calendar, a bar as-of record and
corporate-action evidence, every candidate is M19 BLOCKED (final score 0).
For research ranking the transparent composite M19 computes even when
blocked is exposed as ``TruthfulScore.research_rank_score`` together with the
truth report that says exactly why the candidate is blocked. That ranking is
never an execution signal: ``execution_eligible`` is False on every result.

This module is a tool: nothing in bot/ imports it. It constructs no orders and
touches no broker / live / paper / DB / Telegram code.
"""
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from bot.signal_scoring import (
    adapter_from_scanner_signal,
    default_config,
    score_candidate,
    ScoringProfile,
)
from bot.scoring_truth import TruthReport, apply_truth, build_truth


def _derive_avg_dollar_volume(signal: Mapping) -> Optional[float]:
    """Genuinely derive 20d average dollar volume from scanner-provided fields
    if present (price * volume). Returns None if the inputs are absent — the
    caller then marks liquidity as unavailable rather than inventing a number."""
    price = signal.get("entry_price") or signal.get("price")
    advol = signal.get("avg_volume_20d") or signal.get("avg_volume")
    if price is None or advol is None:
        return None
    try:
        return float(price) * float(advol)
    except (TypeError, ValueError):
        return None


def enrich_signal_with_truth(
    signal: Mapping,
    *,
    evaluated_at_utc: Any = None,
    bar_asof: Optional[Mapping[str, Any]] = None,
    calendar=None,
    risk_decision=None,
    price_basis=None,
    detector_suspected: Optional[bool] = None,
    corporate_action_record=None,
    lookback_start_utc: Any = None,
    avg_dollar_volume: Optional[float] = None,
    model_readiness_passed: bool = False,
    production_thinness_status: Optional[str] = None,
    policy=None,
):
    """Build the truthful M19 input and its TruthReport. Pure: no I/O."""
    adv_dollar = (avg_dollar_volume if avg_dollar_volume is not None
                  else _derive_avg_dollar_volume(signal))
    price = signal.get("entry_price") or signal.get("price")
    liquidity = {"price": price}
    if adv_dollar is not None:
        liquidity["avg_dollar_volume_20d"] = adv_dollar

    ci = adapter_from_scanner_signal(signal, liquidity=liquidity)
    truth = build_truth(
        signal, evaluated_at=evaluated_at_utc, bar_asof=bar_asof,
        calendar=calendar, risk_decision=risk_decision, price_basis=price_basis,
        detector_suspected=detector_suspected,
        corporate_action_record=corporate_action_record,
        lookback_start=lookback_start_utc,
        model_readiness_passed=model_readiness_passed,
        production_thinness_status=production_thinness_status, policy=policy)
    return apply_truth(ci, truth), truth


def enrich_signal(signal: Mapping, **truth_kwargs):
    """Truthful M19 SignalCandidateInput (see ``enrich_signal_with_truth``)."""
    return enrich_signal_with_truth(signal, **truth_kwargs)[0]


@dataclass(frozen=True)
class TruthfulScore:
    """An M19 score plus the evidence behind its inputs."""

    scored: Any               # bot.signal_scoring ScoredSignalCandidate
    truth: TruthReport

    @property
    def research_rank_score(self) -> float:
        """M19's transparent composite (computed even when gate-blocked),
        clamped to 0..100. A research ordering only — never execution."""
        raw = float((self.scored.provenance or {}).get("pre_clamp_final_raw", 0.0))
        return max(0.0, min(100.0, raw))

    @property
    def execution_eligible(self) -> bool:
        if self.scored.execution_eligible is not False:
            raise AssertionError("M19 execution_eligible must be False")
        return False


def score_signal_with_truth(signal, *, profile=ScoringProfile.RESEARCH,
                            **truth_kwargs) -> TruthfulScore:
    ci, truth = enrich_signal_with_truth(signal, **truth_kwargs)
    scored = score_candidate(ci, default_config(profile=profile))
    return TruthfulScore(scored=scored, truth=truth)


def score_signal(signal, *, profile=ScoringProfile.RESEARCH, **truth_kwargs):
    """Truthful M19 score (RESEARCH by default). Returns the M19
    ScoredSignalCandidate. Pure: no I/O."""
    return score_signal_with_truth(signal, profile=profile, **truth_kwargs).scored
