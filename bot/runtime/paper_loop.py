"""M20.I — runtime paper loop.

Orchestrates the existing, frozen building blocks into one simulation-only
pipeline:

    scanner signal dict
      -> adapter_from_scanner_signal  (M19)
      -> score_candidate              (M19)  -> ScoredSignalCandidate
      -> decide_paper_routing         (M20.B) -> PaperRoutingDecision
      -> compute_paper_sizing         (M20.C)
      -> build_paper_order            (M20.D)
      -> simulate_paper_fill          (M20.D)
      -> build_paper_position         (M20.E)
      -> open_position_in_account     (M20.G)

This module adds NO trading logic of its own — it only composes existing
functions. It is SIMULATION ONLY:
  * never imports brokers / live / providers,
  * never writes to the live execution-intents table,
  * never calls the live intent-logging path,
  * never marks a candidate live-executable (M19 candidates carry the
    not-executable flag; the paper routing ingest guard enforces it).

Pure and in-memory: callers pass in the account state and decide what (if
anything) to persist via the M20.H storage functions. No wall-clock is used
for decisions; timestamps come from the caller / the signal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from bot.signal_scoring import (
    adapter_from_scanner_signal, score_candidate, default_config,
    SignalScoringConfig)
from bot.paper import (
    decide_paper_routing, compute_paper_sizing, build_paper_order,
    simulate_paper_fill, build_paper_position, open_position_in_account,
    PaperAccountState)


@dataclass
class PaperLoopOutcome:
    """Per-signal result of running the paper loop on one scanner signal."""
    symbol: str
    stage_reached: str            # scored|routed|sized|ordered|filled|positioned|opened|rejected
    paper_routing_eligible: bool = False
    routed: bool = False
    opened: bool = False
    rejection_reason: Optional[str] = None
    reason_codes: List[str] = field(default_factory=list)


@dataclass
class PaperLoopResult:
    """Aggregate result of one paper-loop run over a batch of signals."""
    account: PaperAccountState
    outcomes: List[PaperLoopOutcome] = field(default_factory=list)
    signals_in: int = 0
    routed_count: int = 0
    opened_count: int = 0
    skipped_ineligible: int = 0
    errors: List[str] = field(default_factory=list)


def run_paper_loop(
    signals: List[Mapping[str, Any]],
    account: PaperAccountState,
    *,
    evaluated_at_utc: str,
    scoring_config: Optional[SignalScoringConfig] = None,
    max_risk_pct: float = 1.0,
    max_position_notional_pct: float = 0.20,
    slippage_bps: float = 0.0,
    commission_bps: float = 0.0,
) -> PaperLoopResult:
    """Run the simulation-only paper loop over scanner signal dicts.

    Returns a PaperLoopResult with the (possibly updated) account state and a
    per-signal outcome list. Never raises for normal bad input — each signal is
    handled independently and rejections are recorded. No live execution, no
    broker calls, no live-intent writes, no live intent logging.
    """
    cfg = scoring_config if scoring_config is not None else default_config()
    result = PaperLoopResult(account=account, signals_in=len(signals))

    for sig in signals:
        symbol = str((sig or {}).get("symbol", "?"))
        try:
            # ── M19: scanner dict -> input -> scored candidate ──
            cand_input = adapter_from_scanner_signal(sig)
            scored = score_candidate(cand_input, cfg)

            # ── M20.B: paper routing decision (eligibility) ──
            decision = decide_paper_routing(
                scored, evaluated_at_utc=evaluated_at_utc)
            outcome = PaperLoopOutcome(
                symbol=symbol, stage_reached="routed",
                paper_routing_eligible=bool(
                    getattr(decision, "paper_routing_eligible", False)),
                reason_codes=list(getattr(decision, "reason_codes", []) or []))

            if not outcome.paper_routing_eligible:
                outcome.stage_reached = "rejected"
                outcome.rejection_reason = "not_paper_routing_eligible"
                result.skipped_ineligible += 1
                result.outcomes.append(outcome)
                continue
            result.routed_count += 1

            # ── M20.C: sizing (uses current account cash as the opening
            # equity/cash basis; no open marks at decision time) ──
            ref_price = (sig or {}).get("entry_price")
            stop_price = (sig or {}).get("stop_loss")
            sizing = compute_paper_sizing(
                decision,
                paper_equity=account.available_paper_cash,
                available_paper_cash=account.available_paper_cash,
                reference_price=ref_price,
                evaluated_at_utc=evaluated_at_utc,
                stop_loss_price=stop_price,
                max_risk_pct=max_risk_pct,
                max_position_notional_pct=max_position_notional_pct)
            if getattr(sizing, "sizing_eligible", None) is not True:
                outcome.stage_reached = "sized"
                outcome.rejection_reason = "sizing_not_eligible"
                result.outcomes.append(outcome)
                continue

            # ── M20.D: order + simulated fill ──
            order_res = build_paper_order(
                decision, sizing, reference_price=ref_price,
                created_at_utc=evaluated_at_utc)
            if not order_res.ok or order_res.order is None:
                outcome.stage_reached = "ordered"
                outcome.rejection_reason = order_res.rejection_reason \
                    or "order_rejected"
                result.outcomes.append(outcome)
                continue

            fill_res = simulate_paper_fill(
                order_res.order, simulated_market_price=ref_price,
                fill_time_utc=evaluated_at_utc, slippage_bps=slippage_bps,
                commission_bps=commission_bps)
            if not fill_res.ok or fill_res.fill is None:
                outcome.stage_reached = "filled"
                outcome.rejection_reason = fill_res.rejection_reason \
                    or "fill_rejected"
                result.outcomes.append(outcome)
                continue

            # ── M20.E: position ──
            pos_res = build_paper_position(
                order_res.order, fill_res.fill, opened_at_utc=evaluated_at_utc)
            if not pos_res.ok or pos_res.position is None:
                outcome.stage_reached = "positioned"
                outcome.rejection_reason = pos_res.rejection_reason \
                    or "position_rejected"
                result.outcomes.append(outcome)
                continue

            # ── M20.G: open in account (advances account state) ──
            fill_notional = fill_res.fill.fill_quantity * fill_res.fill.fill_price
            acct_res = open_position_in_account(
                account, pos_res.position,
                fill_notional=fill_notional,
                entry_commission=getattr(
                    fill_res.fill, "assumed_commission", 0.0) or 0.0,
                event_time_utc=evaluated_at_utc)
            if not acct_res.ok or acct_res.account_state is None:
                outcome.stage_reached = "opened"
                outcome.rejection_reason = acct_res.rejection_reason \
                    or "account_open_rejected"
                result.outcomes.append(outcome)
                continue

            account = acct_res.account_state
            result.account = account
            outcome.stage_reached = "opened"
            outcome.routed = True
            outcome.opened = True
            result.opened_count += 1
            result.outcomes.append(outcome)

        except Exception as e:  # noqa: BLE001 — one bad signal never aborts the batch
            result.errors.append(f"{symbol}:{type(e).__name__}:{e}")
            result.outcomes.append(PaperLoopOutcome(
                symbol=symbol, stage_reached="error",
                rejection_reason=f"exception:{type(e).__name__}"))

    return result
