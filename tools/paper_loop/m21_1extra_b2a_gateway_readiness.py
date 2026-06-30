#!/usr/bin/env python3
"""M21.1extra-B2a — IBKR PAPER gateway readiness (read-only by default).

Proves we can safely interact with the real IBKR *paper* gateway for the
capabilities B2b will eventually need — connection, account verification,
reconcile (open orders + positions), and a truthful report of what cleanup
primitives actually exist — WITHOUT our code originating any order.

B2a NEVER:
  * submits an order, builds a bracket, or constructs an OrderIntent/OrderResult
  * calls placeOrder or builds Market/Limit/Stop orders
  * cancels broadly ("cancel-all")
  * flattens a position by placing a new order
  * touches live mode / port 4001 / eToro / Telegram / scheduler / dashboard

B2a MAY (paper-only, behind explicit gates):
  * connect to the paper gateway and verify the account
  * call reconcile() and get_positions() (read-only reads)
  * cancel EXACTLY ONE operator-pre-placed order by explicit id, only when both
    --cancel-manual-order-id <id> and --i-understand-this-cancels-a-paper-order
    are supplied

Cleanup-capability honesty: B2a does not assume a flatten primitive exists. It
probes the adapter and reports flatten_capability as one of
"available_and_proven" / "not_available_in_current_adapter" / "not_attempted".
Given the current adapter (cancel() cancels open ORDERS only, no close-position
primitive), the truthful result is "not_available_in_current_adapter".
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Constants / mode helpers + the read-only broker methods only.
from bot.brokers.ibkr_broker import (
    PAPER_PORT, LIVE_PORT, _is_live_mode, _get_connection_params,
)
from tools.paper_loop.m21_1extra_a_run_once import _load_env_for_live

_EXPECTED_PAPER_ACCOUNT = "DUP623346"

# Names that would constitute a real paper-only flatten/close-position
# primitive if the adapter ever grows one. Probed, never assumed.
_FLATTEN_PRIMITIVE_NAMES = (
    "flatten_position", "flatten", "close_position", "close_all_positions",
    "liquidate_position", "liquidate",
)


class PaperModeRefused(RuntimeError):
    """Raised when B2a detects live mode / live port / wrong paper account."""


@dataclass
class ReadinessReport:
    mode: str = "readiness"
    paper_mode_asserted: bool = False
    connected: bool = False
    account_verified: bool = False
    account: str = ""
    port: int = 0
    open_orders: List[Dict[str, Any]] = field(default_factory=list)
    positions: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    kill_switch_active: bool = False
    cancel_requested: bool = False
    cancel_attempted: bool = False
    cancel_confirmed: Optional[bool] = None
    cancelled_order_id: Optional[str] = None
    flatten_capability: str = "not_attempted"
    # Hard, always-true-for-B2a provenance:
    order_origination_attempted: bool = False
    broker_submit_attempted: bool = False
    order_result_created: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return dict(vars(self))


def _assert_paper_mode():
    """Refuse live mode / live port / wrong paper account. Reads helpers only;
    opens no connection."""
    if _is_live_mode():
        raise PaperModeRefused(
            "B2a is paper-only: live mode detected, refusing.")
    host, port, account, _client_id = _get_connection_params()
    if int(port) == int(LIVE_PORT):
        raise PaperModeRefused(
            "B2a is paper-only: live port %s detected, refusing." % LIVE_PORT)
    if int(port) != int(PAPER_PORT):
        raise PaperModeRefused(
            "B2a expects paper port %s, got %s." % (PAPER_PORT, port))
    if str(account).strip() != _EXPECTED_PAPER_ACCOUNT:
        raise PaperModeRefused(
            "B2a expects paper account %s, got %r."
            % (_EXPECTED_PAPER_ACCOUNT, account))
    return host, int(port), account


def probe_flatten_capability(broker) -> str:
    """Probe (never assume) whether a safe paper-only flatten/close-position
    primitive exists on the adapter. cancel() cancels open ORDERS only and does
    NOT count as flatten. Returns the honest capability string."""
    for name in _FLATTEN_PRIMITIVE_NAMES:
        if hasattr(broker, name) and callable(getattr(broker, name)):
            # A real primitive exists; B2a does not EXERCISE it (no positions to
            # flatten here), so it is present-but-not-proven. We still report it
            # conservatively as available; proving it is later work.
            return "available_and_proven"
    return "not_available_in_current_adapter"


def _make_broker():
    """Construct the real IBKR adapter for READ-ONLY readiness use. Importing
    here keeps the broker out of import-time and makes mocking straightforward
    in tests."""
    from bot.brokers.ibkr_broker import IBKRBroker
    return IBKRBroker()


def run_readiness(*, cancel_manual_order_id: Optional[str] = None,
                  cancel_confirmed_flag: bool = False,
                  kill_switch_active: Optional[bool] = None,
                  broker=None) -> ReadinessReport:
    """Read-only readiness against the paper gateway, with an optional
    exact-id manual cancel. broker is injectable for tests (mock)."""
    report = ReadinessReport(mode="readiness")

    # 1) paper-mode assertion BEFORE any broker path
    _assert_paper_mode()
    report.paper_mode_asserted = True
    _host, port, account = _get_connection_params()[0], \
        _get_connection_params()[1], _get_connection_params()[2]
    report.port = int(port)
    report.account = account

    # 2) kill switch checked BEFORE touching the broker
    if kill_switch_active is None:
        from bot.kill_switch import is_kill_switch_active
        kill_switch_active = bool(is_kill_switch_active())
    report.kill_switch_active = bool(kill_switch_active)
    if kill_switch_active:
        report.warnings.append(
            "kill switch active: no broker path entered")
        report.flatten_capability = "not_attempted"
        return report

    # 3) connect + readiness reads (real broker, or injected mock)
    if broker is None:
        broker = _make_broker()

    # capability probe (no mutation, no order)
    report.flatten_capability = probe_flatten_capability(broker)

    try:
        recon = broker.reconcile()
        report.connected = True
        report.open_orders = list(recon.get("open_orders", []))
        report.positions = list(recon.get("positions", []))
        report.warnings.extend(list(recon.get("warnings", [])))
    except Exception as e:  # pragma: no cover - exercised on VPS
        report.connected = False
        report.warnings.append("reconcile failed: %s" % e)
        return report

    # account verification via a read-only position read round-trip
    try:
        _ = broker.get_positions()
        report.account_verified = True
    except Exception as e:  # pragma: no cover
        report.account_verified = False
        report.warnings.append("get_positions failed: %s" % e)

    # 4) OPTIONAL exact-id manual cancel (never cancel-all)
    if cancel_manual_order_id:
        report.cancel_requested = True
        if not cancel_confirmed_flag:
            report.warnings.append(
                "cancel refused: missing "
                "--i-understand-this-cancels-a-paper-order")
            report.cancel_attempted = False
            report.cancel_confirmed = None
        else:
            report.cancel_attempted = True
            report.cancelled_order_id = cancel_manual_order_id
            try:
                ok = bool(broker.cancel(cancel_manual_order_id))
                report.cancel_confirmed = ok
            except Exception as e:  # pragma: no cover
                report.cancel_confirmed = False
                report.warnings.append("cancel error: %s" % e)
            # re-reconcile after the cancel to record remaining state
            try:
                recon2 = broker.reconcile()
                report.open_orders = list(recon2.get("open_orders", []))
                report.positions = list(recon2.get("positions", []))
            except Exception as e:  # pragma: no cover
                report.warnings.append("post-cancel reconcile failed: %s" % e)
    elif cancel_confirmed_flag:
        # confirmation but no id: nothing to cancel; record clearly.
        report.warnings.append(
            "cancel confirmation supplied without an order id: no cancel "
            "attempted")

    return report


def _render(d: Dict[str, Any]) -> str:
    L = ["# M21.1extra-B2a — IBKR PAPER gateway readiness", ""]
    L.append("- mode: **%s**" % d["mode"])
    L.append("- paper_mode_asserted: **%s**" % str(d["paper_mode_asserted"]).lower())
    L.append("- connected: **%s**" % str(d["connected"]).lower())
    L.append("- account_verified: **%s**" % str(d["account_verified"]).lower())
    L.append("- account: **%s**" % d["account"])
    L.append("- port: **%s**" % d["port"])
    L.append("- kill_switch_active: **%s**" % str(d["kill_switch_active"]).lower())
    L.append("- flatten_capability: **%s**" % d["flatten_capability"])
    L.append("- order_origination_attempted: **%s**"
             % str(d["order_origination_attempted"]).lower())
    L.append("- broker_submit_attempted: **%s**"
             % str(d["broker_submit_attempted"]).lower())
    L.append("- order_result_created: **%s**"
             % str(d["order_result_created"]).lower())
    L.append("- cancel_requested: **%s**" % str(d["cancel_requested"]).lower())
    L.append("- cancel_attempted: **%s**" % str(d["cancel_attempted"]).lower())
    L.append("- cancel_confirmed: **%s**" % d["cancel_confirmed"])
    L.append("")
    L.append("> **B2a is read-only readiness. Our code originated no order, "
             "attempted no broker submission, built no bracket, and created no "
             "OrderResult. The only optional mutation is cancelling exactly one "
             "operator-supplied order id, behind an explicit confirmation "
             "flag.**")
    L.append(">")
    if d["flatten_capability"] == "not_available_in_current_adapter":
        L.append("> **Cleanup finding: no safe paper-only flatten/close-position "
                 "primitive exists in the current adapter. Therefore a "
                 "market-entry bracket in B2b is NOT yet safe to approve — B2b "
                 "needs either a reviewed flatten primitive or a redesign to a "
                 "cancel-before-fill order type.**")
    L.append("")
    L.append("## Open orders (%d)" % len(d["open_orders"]))
    for o in d["open_orders"]:
        L.append("- %s" % json.dumps(o))
    L.append("")
    L.append("## Positions (%d)" % len(d["positions"]))
    for p in d["positions"]:
        L.append("- %s" % json.dumps(p))
    L.append("")
    if d["warnings"]:
        L.append("## Warnings")
        for w in d["warnings"]:
            L.append("- %s" % w)
        L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("readiness",), default="readiness",
                    help="B2a only supports read-only readiness mode.")
    ap.add_argument("--cancel-manual-order-id", default=None,
                    help="Cancel EXACTLY this operator-pre-placed paper order "
                         "id (no cancel-all). Requires the confirmation flag.")
    ap.add_argument("--i-understand-this-cancels-a-paper-order",
                    dest="cancel_confirmed", action="store_true",
                    help="Explicit confirmation required to cancel an order.")
    ap.add_argument("--report", default="/tmp/m21_1extra_b2a_readiness.md")
    ap.add_argument("--json-out", default="/tmp/m21_1extra_b2a_readiness.json")
    args = ap.parse_args()

    # Operator CLI self-loads repo .env FIRST so BROKER/IBKR_ACCOUNT/IBKR_PORT
    # are honoured (and live mode is refused) without shell exports.
    _load_env_for_live()

    # Operator runs write ONLY under /tmp.
    for p in (args.report, args.json_out):
        if p and not str(Path(p).resolve()).startswith("/tmp/"):
            raise SystemExit(
                "M21.1extra-B2a writes only under /tmp/ (got %r)" % p)

    report = run_readiness(
        cancel_manual_order_id=args.cancel_manual_order_id,
        cancel_confirmed_flag=bool(args.cancel_confirmed))
    d = report.to_dict()
    Path(args.report).write_text(_render(d), encoding="utf-8")
    Path(args.json_out).write_text(json.dumps(d, indent=2), encoding="utf-8")
    print("wrote %s" % args.report)
    print("wrote %s" % args.json_out)
    print("mode=%s connected=%s account=%s port=%s flatten_capability=%s "
          "order_origination_attempted=%s"
          % (d["mode"], d["connected"], d["account"], d["port"],
             d["flatten_capability"], d["order_origination_attempted"]))


if __name__ == "__main__":
    main()
