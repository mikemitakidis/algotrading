#!/usr/bin/env python3
"""M21.1extra-B2b — one tiny controlled IBKR paper-order lifecycle.

Proves the full production-style lifecycle end to end on the real paper gateway:
  build one tiny OrderIntent -> IBKRBroker.submit() (real bracket path) ->
  capture the REAL broker order id -> observe fill/position truthfully ->
  clean up via the merged B2flat flatten_paper_position(symbol, confirm=True) ->
  confirm the account ends FLAT -> write a truthful JSON/report proof.

B2b consumes the already-merged, already-proven adapter methods submit() and
flatten_paper_position(); it does NOT edit bot/brokers/*. It NEVER runs a
scanner, scheduler, dashboard, or persistence layer. It originates AT MOST ONE
entry per confirmed invocation, behind an explicit ugly confirmation flag and a
hard qty/notional cap, and only after proving the account has no pre-existing
position or open orders for the target symbol.

lifecycle_confirmed=true only if the entry was originated, a fill/position was
observed, cleanup called the B2flat primitive, flatten_confirmed=true, and the
final positions/open orders for the symbol are empty. Otherwise it reports
false loudly with the truthful residual state — never a fake clean.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Optional

from bot.brokers.ibkr_broker import (
    PAPER_PORT, LIVE_PORT, _is_live_mode, _get_connection_params,
)
from bot.brokers.base import OrderIntent
from tools.paper_loop.m21_1extra_a_run_once import _load_env_for_live

_EXPECTED_PAPER_ACCOUNT = "DUP623346"

# Hard caps: "tiny" enforced in code, not by trust.
_MAX_QTY = 1
_MAX_NOTIONAL_USD = 5000.0   # qty 1 * price must stay under this ceiling


class PaperModeRefused(RuntimeError):
    pass


class SizeCapExceeded(RuntimeError):
    pass


def _assert_paper_mode():
    if _is_live_mode():
        raise PaperModeRefused("B2b is paper-only: live mode, refusing.")
    _host, port, account, _cid = _get_connection_params()
    if int(port) == int(LIVE_PORT):
        raise PaperModeRefused(
            "B2b is paper-only: live port %s, refusing." % LIVE_PORT)
    if int(port) != int(PAPER_PORT):
        raise PaperModeRefused(
            "B2b expects paper port %s, got %s." % (PAPER_PORT, port))
    if str(account).strip() != _EXPECTED_PAPER_ACCOUNT:
        raise PaperModeRefused(
            "B2b expects paper account %s, got %r."
            % (_EXPECTED_PAPER_ACCOUNT, account))
    return int(port), account


def _build_tiny_intent(symbol: str, *, entry_price: float,
                       stop_loss: float, target_price: float) -> OrderIntent:
    """One tiny long OrderIntent with qty 1 (position_size=1). Prices are
    passed through for the bracket; the entry is a market order in submit()."""
    return OrderIntent(
        signal_id=0,                 # controlled B2b lifecycle, not a real signal
        symbol=symbol,
        direction="long",
        route="IBKR",
        entry_price=float(entry_price),
        stop_loss=float(stop_loss),
        target_price=float(target_price),
        valid_count=1,
        strategy_version=0,
        position_size=1.0,           # -> qty 1 in submit()
    )


def run_lifecycle(symbol: str, *, confirm: bool,
                  entry_price: float, stop_loss: float, target_price: float,
                  kill_switch_active: Optional[bool] = None,
                  broker=None) -> Dict[str, Any]:
    """Full one-shot lifecycle. broker injectable for tests."""
    port, account = _assert_paper_mode()
    result: Dict[str, Any] = {
        "symbol": symbol,
        "paper_asserted": True,
        "account_verified": False,
        "account": account,
        "port": port,
        "kill_switch_active": False,
        "confirmed_flag": bool(confirm),
        "pre_existing_position": False,
        "pre_existing_open_orders": False,
        "size_cap_qty": _MAX_QTY,
        "size_cap_notional_usd": _MAX_NOTIONAL_USD,
        "entry_order_originated": False,
        "entry_order_id": None,
        "entry_result_status": None,
        "entry_result_recorded": False,
        "entry_filled": False,
        "position_observed": False,
        "flatten_called": False,
        "flatten_confirmed": False,
        "close_order_placed": False,
        "remaining_positions": [],
        "remaining_open_orders": [],
        "lifecycle_confirmed": False,
        "warnings": [],
    }

    # explicit confirmation required
    if not confirm:
        result["warnings"].append(
            "confirmation flag required: refusing to originate an entry")
        return result

    # hard size/notional cap (qty is fixed at 1; notional guarded by price)
    if float(entry_price) * _MAX_QTY > _MAX_NOTIONAL_USD:
        result["warnings"].append(
            "notional cap exceeded: %.2f * %d > %.2f"
            % (entry_price, _MAX_QTY, _MAX_NOTIONAL_USD))
        return result

    # kill switch before any broker action
    if kill_switch_active is None:
        from bot.kill_switch import is_kill_switch_active
        kill_switch_active = bool(is_kill_switch_active())
    result["kill_switch_active"] = bool(kill_switch_active)
    if kill_switch_active:
        result["warnings"].append(
            "kill switch active: no broker action taken")
        return result

    if broker is None:
        from bot.brokers.ibkr_broker import IBKRBroker
        broker = IBKRBroker()

    # readiness + account verification + pre-existing state via reconcile.
    # (connection_status proves account; reconcile lists orders/positions.)
    status = broker.connection_status()
    result["account_verified"] = bool(status.get("account_verified", False))
    if not result["account_verified"]:
        result["warnings"].append(
            "account not verified: refusing to submit (%s)"
            % status.get("error", status.get("account_msg", "no detail")))
        return result

    recon = broker.reconcile()
    if any(str(w).startswith("reconcile failed:")
           for w in recon.get("warnings", [])):
        result["warnings"].append(
            "pre-submit reconcile failed: refusing to submit")
        return result
    pre_pos = [p for p in recon.get("positions", [])
               if p.get("symbol") == symbol
               and float(p.get("position", 0)) != 0]
    pre_ord = [o for o in recon.get("open_orders", [])
               if o.get("symbol") == symbol]
    result["pre_existing_position"] = bool(pre_pos)
    result["pre_existing_open_orders"] = bool(pre_ord)
    if pre_pos or pre_ord:
        result["warnings"].append(
            "pre-existing target position/orders: refusing to submit a new "
            "entry (B2b requires a clean start for the symbol)")
        return result

    # ── originate exactly ONE entry via the real submit() path ──────────────
    intent = _build_tiny_intent(
        symbol, entry_price=entry_price, stop_loss=stop_loss,
        target_price=target_price)
    order_result = broker.submit(intent)
    result["entry_order_originated"] = True
    result["entry_result_recorded"] = True
    result["entry_result_status"] = getattr(order_result, "status", None)
    result["entry_order_id"] = getattr(order_result, "broker_order_id", None)
    if getattr(order_result, "status", None) != "accepted" \
            or not result["entry_order_id"]:
        result["warnings"].append(
            "entry not accepted (status=%s): attempting cleanup anyway"
            % result["entry_result_status"])
        # fall through to cleanup — we must still flatten anything that landed

    # ── observe fill / position before cleanup ──────────────────────────────
    recon2 = broker.reconcile()
    obs_pos = [p for p in recon2.get("positions", [])
               if p.get("symbol") == symbol
               and float(p.get("position", 0)) != 0]
    result["position_observed"] = bool(obs_pos)
    result["entry_filled"] = bool(obs_pos)
    if getattr(order_result, "filled_price", None):
        result["entry_filled"] = True

    # ── cleanup via the merged, proven B2flat primitive ─────────────────────
    result["flatten_called"] = True
    flat = broker.flatten_paper_position(symbol, confirm=True)
    result["flatten_confirmed"] = bool(flat.get("flatten_confirmed", False))
    result["close_order_placed"] = bool(flat.get("close_order_placed", False))
    result["remaining_positions"] = flat.get("remaining_positions", [])
    result["remaining_open_orders"] = flat.get("remaining_open_orders", [])
    for w in flat.get("warnings", []):
        result["warnings"].append("flatten: %s" % w)

    # ── final lifecycle verdict (truthful, fail-closed) ─────────────────────
    result["lifecycle_confirmed"] = (
        result["entry_order_originated"]
        and result["position_observed"]
        and result["flatten_called"]
        and result["flatten_confirmed"]
        and not result["remaining_positions"]
        and not result["remaining_open_orders"]
    )
    if not result["lifecycle_confirmed"]:
        result["warnings"].append(
            "lifecycle NOT confirmed: see fields for the failing condition")
    return result


def _render(d: Dict[str, Any], data_source: str) -> str:
    L = ["# M21.1extra-B2b — one tiny paper-order lifecycle", ""]
    L.append("- data_source: **%s**" % data_source)
    for k in ("symbol", "account", "port", "paper_asserted", "account_verified",
              "kill_switch_active", "pre_existing_position",
              "pre_existing_open_orders", "entry_order_originated",
              "entry_order_id", "entry_result_status", "entry_result_recorded",
              "entry_filled", "position_observed", "flatten_called",
              "flatten_confirmed", "close_order_placed", "lifecycle_confirmed"):
        L.append("- %s: **%s**" % (k, d.get(k)))
    L.append("")
    L.append("> **B2b originates exactly one tiny paper entry via the real "
             "submit() path, observes the fill/position, then cleans up with "
             "the merged B2flat flatten primitive. lifecycle_confirmed is true "
             "only if the entry was originated, a position was observed, "
             "flatten_confirmed=true, and no residual positions/orders remain. "
             "It runs no scanner, scheduler, dashboard, or persistence.**")
    L.append("")
    if d.get("warnings"):
        L.append("## Warnings")
        for w in d["warnings"]:
            L.append("- %s" % w)
        L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--entry-price", type=float, required=True,
                    help="Reference entry price (also used for the notional cap "
                         "check); the entry is a market order in submit().")
    ap.add_argument("--stop-loss", type=float, required=True)
    ap.add_argument("--target-price", type=float, required=True)
    ap.add_argument("--i-understand-this-places-a-real-paper-order",
                    dest="confirm", action="store_true")
    ap.add_argument("--report", default="/tmp/m21_1extra_b2b.md")
    ap.add_argument("--json-out", default="/tmp/m21_1extra_b2b.json")
    args = ap.parse_args()

    _load_env_for_live()

    for p in (args.report, args.json_out):
        if p and not str(Path(p).resolve()).startswith("/tmp/"):
            raise SystemExit(
                "M21.1extra-B2b writes only under /tmp/ (got %r)" % p)

    d = run_lifecycle(
        args.symbol, confirm=bool(args.confirm),
        entry_price=args.entry_price, stop_loss=args.stop_loss,
        target_price=args.target_price)
    d["data_source"] = "real_ibkr_paper_gateway"
    Path(args.report).write_text(
        _render(d, "real_ibkr_paper_gateway"), encoding="utf-8")
    Path(args.json_out).write_text(json.dumps(d, indent=2), encoding="utf-8")
    print("wrote %s" % args.report)
    print("wrote %s" % args.json_out)
    print("symbol=%s entry_order_id=%s lifecycle_confirmed=%s "
          "flatten_confirmed=%s"
          % (d.get("symbol"), d.get("entry_order_id"),
             d.get("lifecycle_confirmed"), d.get("flatten_confirmed")))


if __name__ == "__main__":
    main()
