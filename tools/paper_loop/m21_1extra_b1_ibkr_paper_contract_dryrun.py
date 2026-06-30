#!/usr/bin/env python3
"""M21.1extra-B1 — IBKR paper order CONTRACT, dry-run only.

This builds the contract that B2 will later submit to the IBKR *paper* gateway,
and proves its shape and safety gates WITHOUT any submission, connection, or
gateway. It is one level above the broker adapter's order-submission method:

  A-style eligible candidate
    -> strict research_paper_eligibility (reused from A, unchanged)
    -> real OrderIntent (inert data; the actual contract B2 submits)
    -> plain-data IBKR paper dry-run bracket description (no ib_insync objects)
    -> dry-run proof dict / report

HARD B1 boundaries (asserted by tests + an AST/source guard):
  * dry-run ONLY. There is no transmit flag and no submit path in this module.
  * NO IB Gateway connection; IB Gateway need NOT be running for B1.
  * does NOT construct a broker submission-result object and never invents a
    broker_order_id or fill price.
  * NEVER constructs the broker class, calls its order-submission method, or
    invokes any gateway/network routine; never imports the IB client library.
  * paper-mode only: asserts paper expectation (port 4002 / account DUP623346)
    and REFUSES live mode (BROKER=ibkr_live) / live port 4001.
  * preserves execution_eligible=False and hard_gate_passed=False.

It reads bot.brokers.ibkr_broker for CONSTANTS / MODE HELPERS only
(PAPER_PORT, _get_connection_params, _is_live_mode) — never the broker class.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from bot.signal_scoring import ScoringProfile
from bot.brokers.base import OrderIntent
# Constants / mode helpers ONLY — never the IBKRBroker class, never a submit.
from bot.brokers.ibkr_broker import (
    PAPER_PORT, LIVE_PORT, _is_live_mode, _get_connection_params,
)
from bot.runtime.m21_1extra_research_paper_decision import (
    decide_research_paper_eligibility,
)
from tools.signal_scoring.scanner_bridge import score_signal
from tools.paper_loop.m21_1extra_a_run_once import (
    fixture_signals, _load_env_for_live,
)

_EXPECTED_PAPER_ACCOUNT = "DUP623346"


class LiveModeRefused(RuntimeError):
    """Raised when B1 detects live mode / live port; B1 is paper-only."""


@dataclass
class DryRunContract:
    symbol: str
    direction: str
    route: str
    entry_price: float
    stop_loss: float
    target_price: float
    position_size: Optional[float]
    risk_usd: Optional[float]
    account: str
    port: int
    paper_mode_expected: bool = True
    dry_run_only: bool = True
    real_broker_order_attempted: bool = False
    would_transmit: bool = False              # NEVER True in B1
    future_submit_blocked_by_kill_switch: bool = False
    # bracket leg description as PLAIN DATA (mirrors _make_bracket shape only)
    bracket: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = dict(vars(self))
        # broker_order_id is intentionally ABSENT (no real submission in B1).
        return d


@dataclass
class B1Summary:
    candidates_in: int = 0
    eligible_count: int = 0
    dry_run_contracts_built: int = 0
    submit_ready_count: int = 0               # eligible AND kill switch clear
    dry_run_only: bool = True
    real_broker_order_attempted: bool = False
    ib_gateway_connection_attempted: bool = False
    paper_port_expected: int = PAPER_PORT
    paper_account_expected: str = _EXPECTED_PAPER_ACCOUNT
    contracts: List[Dict[str, Any]] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return dict(vars(self))


def _assert_paper_mode():
    """Refuse live mode. Reads mode helpers only; opens no connection."""
    if _is_live_mode():
        raise LiveModeRefused(
            "B1 is paper-only: BROKER=ibkr_live detected, refusing.")
    host, port, account, _client_id = _get_connection_params()
    if int(port) == int(LIVE_PORT):
        raise LiveModeRefused(
            "B1 is paper-only: live port %s detected, refusing." % LIVE_PORT)
    if int(port) != int(PAPER_PORT):
        raise LiveModeRefused(
            "B1 expects paper port %s, got %s." % (PAPER_PORT, port))
    return host, port, account


def build_order_intent(signal: Mapping[str, Any], *, position_size: float,
                       risk_usd: float) -> OrderIntent:
    """Construct the REAL (inert) OrderIntent that B2 will later submit.
    Building it transmits nothing."""
    return OrderIntent(
        signal_id=int(signal.get("signal_id", 0) or 0),
        symbol=str(signal["symbol"]),
        direction=str(signal.get("direction", "long")),
        route="IBKR",
        entry_price=float(signal["entry_price"]),
        stop_loss=float(signal["stop_loss"]),
        target_price=float(signal["target_price"]),
        valid_count=int(signal.get("valid_count", 0) or 0),
        strategy_version=int(signal.get("strategy_version", 1) or 1),
        position_size=position_size,
        risk_usd=risk_usd,
    )


def _bracket_description(intent: OrderIntent, account: str) -> Dict[str, Any]:
    """PLAIN-DATA description of the paper bracket shape (mirrors the adapter's
    _make_bracket shape for review) — no ib_insync objects, no transmit=True."""
    qty = max(1, round(intent.position_size or 1))
    action = "BUY" if intent.direction == "long" else "SELL"
    close_action = "SELL" if intent.direction == "long" else "BUY"
    return {
        "parent": {"type": "MARKET", "action": action, "qty": qty,
                   "account": account, "tif": "DAY", "would_transmit": False},
        "take_profit": {"type": "LIMIT", "action": close_action, "qty": qty,
                        "limit_price": round(intent.target_price, 2),
                        "account": account, "tif": "GTC",
                        "would_transmit": False},
        "stop_loss": {"type": "STOP", "action": close_action, "qty": qty,
                      "stop_price": round(intent.stop_loss, 2),
                      "account": account, "tif": "GTC",
                      "would_transmit": False},
    }


def build_dry_run_contract(signal: Mapping[str, Any], *,
                           position_size: float = 10.0,
                           risk_usd: float = 50.0,
                           kill_switch_active: bool) -> DryRunContract:
    """Build a single dry-run contract for an already-eligible candidate.
    Asserts paper mode; never submits or connects."""
    host, port, account = _assert_paper_mode()
    intent = build_order_intent(signal, position_size=position_size,
                                risk_usd=risk_usd)
    return DryRunContract(
        symbol=intent.symbol, direction=intent.direction, route=intent.route,
        entry_price=intent.entry_price, stop_loss=intent.stop_loss,
        target_price=intent.target_price, position_size=intent.position_size,
        risk_usd=intent.risk_usd, account=account, port=int(port),
        paper_mode_expected=True, dry_run_only=True,
        real_broker_order_attempted=False, would_transmit=False,
        future_submit_blocked_by_kill_switch=bool(kill_switch_active),
        bracket=_bracket_description(intent, account))


def run_once(signals: List[Mapping[str, Any]], *,
             liquidity_by_symbol: Optional[Mapping[str, float]] = None,
             kill_switch_active: Optional[bool] = None) -> B1Summary:
    """Score + eligibility + dry-run contract build over candidates. No submit,
    no connect. kill_switch_active=None reads the real state (pre-submit gate)."""
    if kill_switch_active is None:
        from bot.kill_switch import is_kill_switch_active
        kill_switch_active = bool(is_kill_switch_active())
    liquidity_by_symbol = dict(liquidity_by_symbol or {})

    summary = B1Summary(candidates_in=len(signals))
    seen = set()
    for sig in signals:
        symbol = str((sig or {}).get("symbol", "?"))
        key = (symbol, str((sig or {}).get("timestamp")))
        if key in seen:
            summary.rejected.append(
                {"symbol": symbol, "reason": "duplicate_signal"})
            continue
        seen.add(key)

        adv = liquidity_by_symbol.get(symbol)
        scored = score_signal(sig, profile=ScoringProfile.RESEARCH,
                            avg_dollar_volume=adv)
        elig = decide_research_paper_eligibility(scored)
        if not elig.research_paper_eligible:
            summary.rejected.append(
                {"symbol": symbol, "reason": elig.reason,
                 "rejected_blocks": elig.rejected_blocks,
                 "execution_eligible": bool(scored.execution_eligible),
                 "hard_gate_passed": bool(scored.hard_gate_passed)})
            continue
        summary.eligible_count += 1

        contract = build_dry_run_contract(
            sig, kill_switch_active=bool(kill_switch_active))
        rec = contract.to_dict()
        rec["execution_eligible"] = bool(scored.execution_eligible)
        rec["hard_gate_passed"] = bool(scored.hard_gate_passed)
        summary.contracts.append(rec)
        summary.dry_run_contracts_built += 1
        # submit-ready ONLY if the kill switch is clear (B1 never submits;
        # this only records readiness the future B2 path would require).
        if not kill_switch_active:
            summary.submit_ready_count += 1

    return summary


def run_live(focus_size=150):
    """Real Alpaca scan + score + dry-run contract build. /tmp use only.
    Self-loads .env first (so BROKER=ibkr_live, if set, is honoured and
    refused). No submit, no connect."""
    _load_env_for_live()
    _assert_paper_mode()  # refuse live mode up front
    from bot.scanner import scan_cycle
    from bot.universe.active_selection import get_scan_ready_symbols
    from tools.signal_scoring.score_rank_harness import _live_liquidity_map
    focus = get_scan_ready_symbols()[:focus_size]
    # Minimal config: the scanner applies its own routing defaults
    # (etoro/ibkr min-timeframes), so B1 names none of them here.
    config = {"strategy": "default"}
    signals, _meta = scan_cycle(focus, config, conn=None, cycle_id=0)
    if not signals:
        return run_once([], liquidity_by_symbol={})
    liq = _live_liquidity_map(sorted({s["symbol"] for s in signals}))
    return run_once(signals, liquidity_by_symbol=liq)


def _render(d: Dict[str, Any], data_source: str) -> str:
    L = ["# M21.1extra-B1 — IBKR paper contract DRY-RUN proof", ""]
    L.append("- data_source: **%s**" % data_source)
    L.append("- dry_run_only: **true**")
    L.append("- real_broker_order_attempted: **false**")
    L.append("- ib_gateway_connection_attempted: **false**")
    L.append("- paper_port_expected: **%s**" % d["paper_port_expected"])
    L.append("- paper_account_expected: **%s**" % d["paper_account_expected"])
    L.append("")
    L.append("> **B1 is dry-run only. No real IBKR paper order was submitted. "
             "No IB Gateway connection was attempted. No broker_order_id "
             "exists. This proves contract construction only, not real "
             "submission.**")
    L.append(">")
    L.append("> B2 remains required for single real IBKR paper submission. "
             "B2 must include an explicit cleanup/cancel/flatten plan before "
             "approval.")
    L.append("")
    L.append("## Summary")
    L.append("")
    for k in ("candidates_in", "eligible_count", "dry_run_contracts_built",
              "submit_ready_count"):
        L.append("- %s: **%s**" % (k, d[k]))
    L.append("")
    L.append("## Dry-run contracts")
    L.append("")
    L.append("| symbol | dir | route | entry | stop | target | qty | account | "
             "port | would_transmit | exec_elig | gate_passed |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for c in d["contracts"]:
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
                 % (c["symbol"], c["direction"], c["route"], c["entry_price"],
                    c["stop_loss"], c["target_price"], c["position_size"],
                    c["account"], c["port"], str(c["would_transmit"]).lower(),
                    str(c["execution_eligible"]).lower(),
                    str(c["hard_gate_passed"]).lower()))
    L.append("")
    return "\n".join(L)


def fixture_summary() -> B1Summary:
    # Deterministic: reuse A's fixture signals, kill switch explicitly clear.
    return run_once(fixture_signals(), kill_switch_active=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("fixture", "live"), default="fixture")
    ap.add_argument("--focus-size", type=int, default=150)
    ap.add_argument("--report",
                    default="/tmp/m21_1extra_b1_dryrun.md")
    ap.add_argument("--json-out",
                    default="/tmp/m21_1extra_b1_dryrun.json")
    args = ap.parse_args()

    # Operator runs write ONLY under /tmp (never the repo).
    for p in (args.report, args.json_out):
        if p and not str(Path(p).resolve()).startswith("/tmp/"):
            raise SystemExit(
                "M21.1extra-B1 writes only under /tmp/ (got %r)" % p)

    if args.mode == "live":
        summary = run_live(focus_size=args.focus_size)
        data_source = "live_alpaca_scan_cycle"
    else:
        summary = fixture_summary()
        data_source = "simulated_fixture"

    d = summary.to_dict()
    d["data_source"] = data_source
    Path(args.report).write_text(_render(d, data_source), encoding="utf-8")
    Path(args.json_out).write_text(json.dumps(d, indent=2), encoding="utf-8")
    print("wrote %s" % args.report)
    print("wrote %s" % args.json_out)
    print("mode=%s candidates_in=%d eligible=%d contracts=%d submit_ready=%d "
          "dry_run_only=%s broker_attempt=%s"
          % (args.mode, d["candidates_in"], d["eligible_count"],
             d["dry_run_contracts_built"], d["submit_ready_count"],
             d["dry_run_only"], d["real_broker_order_attempted"]))


if __name__ == "__main__":
    main()
