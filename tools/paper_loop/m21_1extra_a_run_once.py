#!/usr/bin/env python3
"""M21.1extra-A — run-once, operator-triggered, SIMULATION-ONLY paper loop.

Proves end to end, with NO real broker and NO real money:

  scanner-shaped signal
    -> M21.1 RESEARCH score/rank (scanner_bridge.score_signal)
    -> research_paper_eligible decision (strict, simulation-only)
    -> M20 sizing -> build_paper_order -> simulate_paper_fill
    -> build_paper_position -> open_position_in_account
    -> simulated close at SL or TP -> recorded outcome (win/loss, PnL, R)
    -> aggregate summary

Hard safety properties (asserted by tests):
  * imports NO broker module (ibkr_broker / paper_broker), NO eToro, NO Telegram,
    NO scheduler, NO main.py, NO dashboard.
  * places NO real order; fills are simulated by the frozen M20 simulate_paper_fill.
  * kill switch active -> opens nothing.
  * idempotent: a signal already processed this run is not opened/recorded twice.
  * never sets execution_eligible / hard_gate_passed; never edits M19/M20 truth.
  * writes nothing unless the caller passes an explicit /tmp dry-run path.

Run-once only: no schedule, no unattended mode. Fixture mode is deterministic;
live mode reuses the M21.1 harness's real-bar liquidity derivation and must
write only under /tmp.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from bot.signal_scoring import ScoringProfile
from bot.paper import (
    PaperRoutingDecision, PaperSide, compute_paper_sizing, build_paper_order,
    simulate_paper_fill, build_paper_position, open_position_in_account,
    close_paper_position, PaperAccountState,
)
from bot.runtime.m21_1extra_research_paper_decision import (
    decide_research_paper_eligibility,
)
from tools.signal_scoring.scanner_bridge import score_signal

_STARTING_EQUITY = 100_000.0


@dataclass
class SimOutcome:
    symbol: str
    stage: str                       # scored|eligible|sized|ordered|filled|opened|closed|rejected
    research_paper_eligible: bool = False
    rejection_reason: Optional[str] = None
    exit_reason: Optional[str] = None   # SL|TP
    realized_pnl: Optional[float] = None
    r_multiple: Optional[float] = None
    execution_eligible: bool = False
    hard_gate_passed: bool = False


@dataclass
class SimSummary:
    signals_in: int = 0
    scored_count: int = 0
    research_paper_eligible_count: int = 0
    simulated_orders: int = 0
    simulated_fills: int = 0
    opened_positions: int = 0
    closed_positions: int = 0
    wins: int = 0
    losses: int = 0
    average_win: float = 0.0
    average_loss: float = 0.0
    win_loss_ratio: Any = "not_available_in_A"
    max_drawdown: Any = "not_available_in_A"
    outcomes: List[SimOutcome] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signals_in": self.signals_in,
            "scored_count": self.scored_count,
            "research_paper_eligible_count": self.research_paper_eligible_count,
            "simulated_orders": self.simulated_orders,
            "simulated_fills": self.simulated_fills,
            "opened_positions": self.opened_positions,
            "closed_positions": self.closed_positions,
            "wins": self.wins,
            "losses": self.losses,
            "average_win": self.average_win,
            "average_loss": self.average_loss,
            "win_loss_ratio": self.win_loss_ratio,
            "max_drawdown": self.max_drawdown,
            "outcomes": [vars(o) for o in self.outcomes],
        }


def _exit_price_for(sig: Mapping, outcome_kind: str) -> float:
    """Deterministic simulated exit: 'TP' -> target_price, 'SL' -> stop_loss."""
    return float(sig["target_price"] if outcome_kind == "TP" else sig["stop_loss"])


def run_once(
    signals: List[Mapping[str, Any]],
    *,
    evaluated_at_utc: str = "2026-06-26T15:00:00+00:00",
    exit_plan: Optional[Mapping[str, str]] = None,
    liquidity_by_symbol: Optional[Mapping[str, float]] = None,
    kill_switch_active: Optional[bool] = None,
) -> SimSummary:
    """Run the simulation once over scanner-shaped signals. Pure/in-memory.

    exit_plan: optional {symbol: 'SL'|'TP'} to drive the deterministic close in
      fixture mode. Default 'TP' for eligible longs (so a win is demonstrable).
    kill_switch_active: if None, reads bot.kill_switch.is_kill_switch_active();
      tests pass an explicit bool to avoid touching the real state file.
    """
    if kill_switch_active is None:
        from bot.kill_switch import is_kill_switch_active
        kill_switch_active = bool(is_kill_switch_active())

    exit_plan = dict(exit_plan or {})
    liquidity_by_symbol = dict(liquidity_by_symbol or {})
    summary = SimSummary(signals_in=len(signals))
    account = PaperAccountState(
        starting_equity=_STARTING_EQUITY,
        available_paper_cash=_STARTING_EQUITY,
        as_of_utc=evaluated_at_utc)
    wins_pnl: List[float] = []
    losses_pnl: List[float] = []
    seen_keys = set()

    for sig in signals:
        symbol = str((sig or {}).get("symbol", "?"))
        # idempotency: dedup by (symbol, timestamp)
        key = (symbol, str((sig or {}).get("timestamp")))
        if key in seen_keys:
            summary.outcomes.append(SimOutcome(
                symbol, "rejected", rejection_reason="duplicate_signal"))
            continue
        seen_keys.add(key)

        # 1) M21.1 RESEARCH score
        adv = liquidity_by_symbol.get(symbol)
        scored = score_signal(sig, profile=ScoringProfile.RESEARCH,
                             avg_dollar_volume=adv)
        summary.scored_count += 1
        oc = SimOutcome(symbol, "scored",
                        execution_eligible=bool(scored.execution_eligible),
                        hard_gate_passed=bool(scored.hard_gate_passed))

        # 2) strict research-paper eligibility
        elig = decide_research_paper_eligibility(scored)
        oc.research_paper_eligible = elig.research_paper_eligible
        if not elig.research_paper_eligible:
            oc.stage = "rejected"
            oc.rejection_reason = elig.reason
            summary.outcomes.append(oc)
            continue
        summary.research_paper_eligible_count += 1
        oc.stage = "eligible"

        # 3) kill switch blocks any open
        if kill_switch_active:
            oc.stage = "rejected"
            oc.rejection_reason = "kill_switch_active"
            summary.outcomes.append(oc)
            continue

        # 4) build a research-sim routing decision (eligibility already proven
        #    by our strict gate above; we never touched M19 truth fields).
        ref_price = float(sig["entry_price"])
        stop_price = float(sig["stop_loss"])
        decision = PaperRoutingDecision(
            m19_candidate_id=scored.candidate_id,
            symbol=symbol,
            side=PaperSide.LONG,
            decision_bucket=getattr(scored.decision_bucket, "value",
                                    str(scored.decision_bucket)),
            confidence_bucket=getattr(scored.confidence_bucket, "value",
                                      str(scored.confidence_bucket)),
            paper_routing_eligible=True,
            evaluated_at_utc=evaluated_at_utc,
            calibration_applied=False,
            reason_codes=["m21_1extra_research_paper_sim"],
            warnings=[])

        # 5) M20 sizing
        sizing = compute_paper_sizing(
            decision, paper_equity=account.available_paper_cash,
            available_paper_cash=account.available_paper_cash,
            reference_price=ref_price, evaluated_at_utc=evaluated_at_utc,
            stop_loss_price=stop_price)
        if getattr(sizing, "sizing_eligible", None) is not True:
            oc.stage = "rejected"
            oc.rejection_reason = "sizing_not_eligible:%s" % (
                sizing.sizing_rejection_reason or "?")
            summary.outcomes.append(oc)
            continue
        oc.stage = "sized"

        # 6) M20 order + simulated fill
        order_res = build_paper_order(
            decision, sizing, reference_price=ref_price,
            created_at_utc=evaluated_at_utc)
        if not order_res.ok or order_res.order is None:
            oc.stage = "rejected"
            oc.rejection_reason = order_res.rejection_reason or "order_rejected"
            summary.outcomes.append(oc)
            continue
        summary.simulated_orders += 1
        oc.stage = "ordered"

        fill_res = simulate_paper_fill(
            order_res.order, simulated_market_price=ref_price,
            fill_time_utc=evaluated_at_utc)
        if not fill_res.ok or fill_res.fill is None:
            oc.stage = "rejected"
            oc.rejection_reason = fill_res.rejection_reason or "fill_rejected"
            summary.outcomes.append(oc)
            continue
        summary.simulated_fills += 1
        oc.stage = "filled"

        # 7) position + open in account
        pos_res = build_paper_position(
            order_res.order, fill_res.fill, opened_at_utc=evaluated_at_utc)
        if not pos_res.ok or pos_res.position is None:
            oc.stage = "rejected"
            oc.rejection_reason = pos_res.rejection_reason or "position_rejected"
            summary.outcomes.append(oc)
            continue
        fill_notional = float(fill_res.fill.fill_price) * \
            float(fill_res.fill.fill_quantity)
        acct_res = open_position_in_account(
            account, pos_res.position, fill_notional=fill_notional,
            event_time_utc=evaluated_at_utc)
        if not acct_res.ok or acct_res.account_state is None:
            oc.stage = "rejected"
            oc.rejection_reason = acct_res.rejection_reason or "open_rejected"
            summary.outcomes.append(oc)
            continue
        account = acct_res.account_state
        summary.opened_positions += 1
        oc.stage = "opened"

        # 8) deterministic simulated close at SL or TP
        kind = exit_plan.get(symbol, "TP")
        exit_price = _exit_price_for(sig, kind)
        close_res = close_paper_position(
            pos_res.position, exit_price=exit_price,
            closed_at_utc=evaluated_at_utc)
        if not close_res.ok:
            oc.stage = "rejected"
            oc.rejection_reason = close_res.rejection_reason or "close_rejected"
            summary.outcomes.append(oc)
            continue
        summary.closed_positions += 1
        oc.stage = "closed"
        oc.exit_reason = kind

        dm = close_res.derived_metrics or {}
        net = float(dm.get("net_realized_pnl"))
        oc.realized_pnl = net
        # R-multiple = realized PnL / initial risk amount (entry - stop) * qty
        risk_amt = float(sizing.paper_risk_amount) if \
            sizing.paper_risk_amount else 0.0
        oc.r_multiple = round(net / risk_amt, 4) if risk_amt > 0 else None
        if net > 0:
            summary.wins += 1
            wins_pnl.append(net)
        elif net < 0:
            summary.losses += 1
            losses_pnl.append(net)
        summary.outcomes.append(oc)

    summary.average_win = round(sum(wins_pnl) / len(wins_pnl), 4) \
        if wins_pnl else 0.0
    summary.average_loss = round(sum(losses_pnl) / len(losses_pnl), 4) \
        if losses_pnl else 0.0
    if wins_pnl and losses_pnl and summary.average_loss != 0:
        summary.win_loss_ratio = round(
            abs(summary.average_win / summary.average_loss), 4)
    else:
        summary.win_loss_ratio = "not_available_in_A"
    return summary


def fixture_signals():
    ts = "2026-06-26T15:00:00+00:00"
    base = lambda **kw: dict(timestamp=ts, available_tfs=4,  # noqa: E731
                             avg_volume_20d=500000, **kw)
    return [
        base(symbol="WINNER", direction="long", entry_price=100.0,
             stop_loss=95.0, target_price=115.0, rsi=62.0, macd_hist=0.9,
             vol_ratio=1.4, valid_count=4, atr=2.0),
        base(symbol="LOSER", direction="long", entry_price=50.0,
             stop_loss=48.0, target_price=56.0, rsi=58.0, macd_hist=0.5,
             vol_ratio=1.2, valid_count=4, atr=1.2),
    ]


def _fixture_exit_plan():
    return {"WINNER": "TP", "LOSER": "SL"}


def run_live(focus_size=150):
    """Real Alpaca scan + M21.1 score + simulated paper loop. /tmp use only.
    Derives liquidity from real bars (same as the M21.1 harness)."""
    from bot.scanner import scan_cycle
    from bot.universe.active_selection import get_scan_ready_symbols
    from tools.signal_scoring.score_rank_harness import _live_liquidity_map
    focus = get_scan_ready_symbols()[:focus_size]
    config = {"strategy": "default",
              "routing": {"etoro_min_tfs": 4, "ibkr_min_tfs": 2,
                          "min_valid_tfs": 1}}
    signals, _meta = scan_cycle(focus, config, conn=None, cycle_id=0)
    if not signals:
        # No scanner setups this cycle. This is a valid no-op simulation
        # result, NOT an error. Do not call _live_liquidity_map([]), because
        # the Alpaca provider requires a non-empty symbols list and would
        # otherwise error. run_once([]) returns a zero-everything summary,
        # which the live-proof gate still (correctly) treats as "not proven".
        return run_once([], liquidity_by_symbol={})
    liq = _live_liquidity_map(sorted({s["symbol"] for s in signals}))
    # Kill switch is NOT forced here: run_once reads the real
    # is_kill_switch_active() when kill_switch_active is None, so an active
    # kill switch blocks even the simulation-only live run-once path.
    return run_once(signals, liquidity_by_symbol=liq)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("fixture", "live"), default="fixture")
    ap.add_argument("--focus-size", type=int, default=150)
    ap.add_argument("--report", default="/tmp/m21_1extra_a_run_once.md")
    ap.add_argument("--json-out", default="/tmp/m21_1extra_a_run_once.json")
    args = ap.parse_args()

    # A writes ONLY under /tmp (never the repo).
    for p in (args.report, args.json_out):
        if p and not str(Path(p).resolve()).startswith("/tmp/"):
            raise SystemExit(
                "M21.1extra-A writes only under /tmp/ (got %r)" % p)

    if args.mode == "live":
        summary = run_live(focus_size=args.focus_size)
        data_source = "live_alpaca_scan_cycle"
    else:
        summary = run_once(fixture_signals(), exit_plan=_fixture_exit_plan(),
                          kill_switch_active=False)
        data_source = "simulated_fixture"

    d = summary.to_dict()
    d["data_source"] = data_source
    d["simulation_only"] = True
    d["real_broker_order_attempted"] = False
    Path(args.report).write_text(_render(d, data_source), encoding="utf-8")
    Path(args.json_out).write_text(json.dumps(d, indent=2), encoding="utf-8")
    print("wrote %s" % args.report)
    print("wrote %s" % args.json_out)
    print("mode=%s signals_in=%d eligible=%d opened=%d closed=%d wins=%d "
          "losses=%d" % (args.mode, d["signals_in"],
                         d["research_paper_eligible_count"],
                         d["opened_positions"], d["closed_positions"],
                         d["wins"], d["losses"]))


def _render(d, data_source):
    L = ["# M21.1extra-A — run-once simulation paper loop (read-only proof)", ""]
    L.append("- data_source: **%s**" % data_source)
    L.append("- simulation_only: **true**")
    L.append("- real_broker_order_attempted: **false**")
    L.append("- profile: **RESEARCH** (execution_eligible False, gate not passed)")
    L.append("")
    L.append("> Simulation only. No real broker, no real money, no eToro, no "
             "Telegram, no scheduler. Orders/fills are produced by the frozen "
             "M20 simulate_paper_fill. Research-paper eligibility never sets "
             "execution_eligible or hard_gate_passed and never edits M19/M20 "
             "truth fields.")
    L.append("")
    L.append("## Summary")
    L.append("")
    for k in ("signals_in", "scored_count", "research_paper_eligible_count",
              "simulated_orders", "simulated_fills", "opened_positions",
              "closed_positions", "wins", "losses", "average_win",
              "average_loss", "win_loss_ratio", "max_drawdown"):
        L.append("- %s: **%s**" % (k, d[k]))
    L.append("")
    L.append("## Per-signal outcomes")
    L.append("")
    L.append("| symbol | stage | eligible | exit | realized_pnl | r_multiple |")
    L.append("|---|---|---|---|---|---|")
    for o in d["outcomes"]:
        L.append("| %s | %s | %s | %s | %s | %s |" % (
            o["symbol"], o["stage"], str(o["research_paper_eligible"]).lower(),
            o["exit_reason"] or "-", o["realized_pnl"] if o["realized_pnl"]
            is not None else "-", o["r_multiple"] if o["r_multiple"]
            is not None else "-"))
    L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    main()
