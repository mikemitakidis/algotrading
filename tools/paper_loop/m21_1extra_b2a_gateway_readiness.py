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
"available_but_not_proven" / "not_available_in_current_adapter" /
"not_attempted". B2a never outputs "available_and_proven" — that value is
reserved for a future branch that actually exercises and verifies a flatten
primitive. Given the current adapter (cancel() cancels open ORDERS only, no
close-position primitive), the truthful result is
"not_available_in_current_adapter".
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
    connection_status_checked: bool = False
    reconcile_succeeded: bool = False
    positions_read_attempted: bool = False
    post_cancel_reconcile_succeeded: Optional[bool] = None
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
    """Probe (never assume, never overclaim) whether a paper-only flatten/
    close-position primitive EXISTS on the adapter. cancel() cancels open ORDERS
    only and does NOT count as flatten.

    B2a does not EXERCISE any flatten primitive (it never opens a position), so
    it can never honestly say a primitive is *proven*. Semantics:
      * a method exists but is not exercised here -> "available_but_not_proven"
      * no such method exists                     -> "not_available_in_current_adapter"
    "available_and_proven" is reserved for a future branch that actually flattens
    a real paper position and verifies it; B2a must never claim it.
    """
    for name in _FLATTEN_PRIMITIVE_NAMES:
        if hasattr(broker, name) and callable(getattr(broker, name)):
            return "available_but_not_proven"
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

    # 3a) REAL connection + account proof via connection_status().
    #     reconcile()/get_positions() swallow exceptions and return empty
    #     containers, so they are NOT reliable connection/account signals.
    #     connection_status() returns connected=False (+error) on failure.
    report.connection_status_checked = True
    try:
        status = broker.connection_status()
    except Exception as e:  # pragma: no cover - defensive; adapter catches
        status = {"connected": False, "error": str(e)[:120]}
    report.connected = bool(status.get("connected", False))
    if status.get("account") is not None:
        report.account = status.get("account")
    if status.get("port") is not None:
        report.port = int(status.get("port"))
    report.account_verified = bool(status.get("account_verified", False))
    if not report.connected:
        report.warnings.append(
            "connection_status: not connected (%s)"
            % status.get("error", "no error detail"))
        return report          # do not claim readiness, do not run cancel
    if not report.account_verified:
        report.warnings.append(
            "connection_status: account not verified (%s)"
            % status.get("account_msg", "no detail"))
        return report          # do not run cancel on an unverified account

    # 3b) paper open orders + positions via reconcile() (connection_status
    #     skips reconcile in paper mode). reconcile() never raises; it reports
    #     failure as a 'reconcile failed: ...' warning, which we interpret
    #     truthfully rather than treating an empty dict as success.
    recon = broker.reconcile()
    recon_warnings = list(recon.get("warnings", []))
    reconcile_failed = any(
        str(w).startswith("reconcile failed:") for w in recon_warnings)
    report.reconcile_succeeded = not reconcile_failed
    report.open_orders = list(recon.get("open_orders", []))
    report.positions = list(recon.get("positions", []))
    report.warnings.extend(recon_warnings)
    if reconcile_failed:
        report.warnings.append(
            "readiness incomplete: reconcile did not succeed")
        return report          # do not run cancel if we could not reconcile

    # positions read: get_positions() is non-raising in the adapter (it
    # swallows errors and returns []), so its RETURN is not a success proof.
    # We only record that the read was ATTEMPTED; reconcile_succeeded above is
    # the authoritative readiness signal for open-orders/positions state.
    report.positions_read_attempted = True
    try:
        _ = broker.get_positions()
    except Exception as e:  # pragma: no cover - adapter normally swallows
        report.warnings.append("get_positions raised: %s" % e)

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
            # re-reconcile after the cancel to record remaining state.
            # reconcile() is non-raising; it reports failure via a
            # 'reconcile failed: ...' warning, so we must inspect warnings
            # rather than rely on try/except, and must NOT imply the cancel/
            # cleanup state is verified if the post-cancel reconcile failed.
            recon2 = broker.reconcile()
            recon2_warnings = list(recon2.get("warnings", []))
            post_failed = any(
                str(w).startswith("reconcile failed:") for w in recon2_warnings)
            report.post_cancel_reconcile_succeeded = not post_failed
            report.open_orders = list(recon2.get("open_orders", []))
            report.positions = list(recon2.get("positions", []))
            report.warnings.extend(recon2_warnings)
            if post_failed:
                report.warnings.append(
                    "post-cancel reconcile did not succeed: remaining "
                    "order/position state is NOT verified")
    elif cancel_confirmed_flag:
        # confirmation but no id: nothing to cancel; record clearly.
        report.warnings.append(
            "cancel confirmation supplied without an order id: no cancel "
            "attempted")

    return report


def _render(d: Dict[str, Any], data_source: str = "real_ibkr_paper_gateway") -> str:
    is_mock = data_source == "mock_broker_structural_proof"
    L = ["# M21.1extra-B2a — IBKR PAPER gateway readiness", ""]
    L.append("- data_source: **%s**" % data_source)
    L.append("- real_ibkr_gateway_connected: **%s**"
             % str(bool(d["connected"]) and not is_mock).lower())
    L.append("- vps_gateway_proof_required: **%s**" % str(is_mock).lower())
    L.append("- mode: **%s**" % d["mode"])
    L.append("- paper_mode_asserted: **%s**" % str(d["paper_mode_asserted"]).lower())
    L.append("- connected: **%s**" % str(d["connected"]).lower())
    L.append("- account_verified: **%s**" % str(d["account_verified"]).lower())
    L.append("- connection_status_checked: **%s**"
             % str(d["connection_status_checked"]).lower())
    L.append("- reconcile_succeeded: **%s**"
             % str(d["reconcile_succeeded"]).lower())
    L.append("- positions_read_attempted: **%s**"
             % str(d["positions_read_attempted"]).lower())
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
    L.append("- post_cancel_reconcile_succeeded: **%s**"
             % d["post_cancel_reconcile_succeeded"])
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
    d["data_source"] = "real_ibkr_paper_gateway"
    d["real_ibkr_gateway_connected"] = bool(d["connected"])
    d["vps_gateway_proof_required"] = False
    Path(args.report).write_text(
        _render(d, data_source="real_ibkr_paper_gateway"), encoding="utf-8")
    Path(args.json_out).write_text(json.dumps(d, indent=2), encoding="utf-8")
    print("wrote %s" % args.report)
    print("wrote %s" % args.json_out)
    print("mode=%s connected=%s account=%s port=%s flatten_capability=%s "
          "order_origination_attempted=%s"
          % (d["mode"], d["connected"], d["account"], d["port"],
             d["flatten_capability"], d["order_origination_attempted"]))


if __name__ == "__main__":
    main()
