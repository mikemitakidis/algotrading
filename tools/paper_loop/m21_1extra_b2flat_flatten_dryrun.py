#!/usr/bin/env python3
"""M21.1extra-B2flat — operator harness for the paper-only flatten primitive.

Exercises IBKRBroker.flatten_paper_position(symbol, confirm=True) against a
paper position the OPERATOR pre-placed, and records a truthful proof. This
harness NEVER originates an entry order; the only broker action reachable is
the adapter's paper-only cancel+close cleanup.

Safety: self-loads .env (so BROKER=ibkr_live is honoured and refused), asserts
paper mode / port 4002 / account DUP623346, checks the kill switch before any
broker action (the adapter also re-checks), requires an explicit --symbol and
an explicit --i-understand-this-closes-a-paper-position confirmation, and
writes only under /tmp. flatten_confirmed is reported exactly as the adapter
returns it — never assumed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Optional

from bot.brokers.ibkr_broker import (
    PAPER_PORT, LIVE_PORT, _is_live_mode, _get_connection_params,
)
from tools.paper_loop.m21_1extra_a_run_once import _load_env_for_live

_EXPECTED_PAPER_ACCOUNT = "DUP623346"


class PaperModeRefused(RuntimeError):
    pass


def _assert_paper_mode():
    if _is_live_mode():
        raise PaperModeRefused("B2flat is paper-only: live mode, refusing.")
    _host, port, account, _cid = _get_connection_params()
    if int(port) == int(LIVE_PORT):
        raise PaperModeRefused(
            "B2flat is paper-only: live port %s, refusing." % LIVE_PORT)
    if int(port) != int(PAPER_PORT):
        raise PaperModeRefused(
            "B2flat expects paper port %s, got %s." % (PAPER_PORT, port))
    if str(account).strip() != _EXPECTED_PAPER_ACCOUNT:
        raise PaperModeRefused(
            "B2flat expects paper account %s, got %r."
            % (_EXPECTED_PAPER_ACCOUNT, account))
    return int(port), account


def run_flatten(symbol: str, *, confirm: bool,
                kill_switch_active: Optional[bool] = None,
                broker=None) -> Dict[str, Any]:
    """Assert paper mode + kill switch, then delegate to the adapter's
    paper-only flatten primitive. broker injectable for tests."""
    port, account = _assert_paper_mode()

    if kill_switch_active is None:
        from bot.kill_switch import is_kill_switch_active
        kill_switch_active = bool(is_kill_switch_active())
    if kill_switch_active:
        return {
            "symbol": symbol, "flatten_confirmed": False,
            "kill_switch_active": True, "account": account, "port": port,
            "paper_asserted": True, "account_verified": False,
            "post_cancel_open_orders_cleared": None, "already_flat": False,
            "warnings": ["kill switch active: no broker action taken"],
            "dry_run_only": False, "entry_order_originated": False,
        }

    if broker is None:
        from bot.brokers.ibkr_broker import IBKRBroker
        broker = IBKRBroker()

    res = broker.flatten_paper_position(symbol, confirm=confirm)
    res.setdefault("entry_order_originated", False)
    res.setdefault("account", account)
    res.setdefault("port", port)
    return res


def _render(d: Dict[str, Any], data_source: str) -> str:
    L = ["# M21.1extra-B2flat — paper-only flatten proof", ""]
    L.append("- data_source: **%s**" % data_source)
    L.append("- symbol: **%s**" % d.get("symbol"))
    L.append("- account: **%s**" % d.get("account"))
    L.append("- port: **%s**" % d.get("port"))
    L.append("- paper_asserted: **%s**"
             % str(d.get("paper_asserted", False)).lower())
    L.append("- account_verified: **%s**"
             % str(d.get("account_verified", False)).lower())
    L.append("- flatten_confirmed: **%s**"
             % str(d.get("flatten_confirmed")).lower())
    L.append("- already_flat: **%s**"
             % str(d.get("already_flat", False)).lower())
    L.append("- post_cancel_open_orders_cleared: **%s**"
             % d.get("post_cancel_open_orders_cleared"))
    L.append("- kill_switch_active: **%s**"
             % str(d.get("kill_switch_active", False)).lower())
    L.append("- close_order_placed: **%s**"
             % str(d.get("close_order_placed", False)).lower())
    L.append("- cancelled_order_ids: **%s**" % d.get("cancelled_order_ids", []))
    L.append("- entry_order_originated: **false**")
    L.append("")
    L.append("> **B2flat performs ONLY paper cleanup: it cancels the target "
             "symbol's open orders and places a single offsetting close for an "
             "existing paper position. It NEVER originates an entry order. "
             "flatten_confirmed is true only when the post-action reconcile "
             "shows the symbol genuinely flat (no position AND no open "
             "orders).**")
    L.append("")
    if d.get("warnings"):
        L.append("## Warnings")
        for w in d["warnings"]:
            L.append("- %s" % w)
        L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True,
                    help="Explicit target symbol to flatten (required).")
    ap.add_argument("--i-understand-this-closes-a-paper-position",
                    dest="confirm", action="store_true",
                    help="Explicit confirmation required to place the close.")
    ap.add_argument("--report", default="/tmp/m21_1extra_b2flat.md")
    ap.add_argument("--json-out", default="/tmp/m21_1extra_b2flat.json")
    args = ap.parse_args()

    _load_env_for_live()  # honour .env (refuse live) before anything

    for p in (args.report, args.json_out):
        if p and not str(Path(p).resolve()).startswith("/tmp/"):
            raise SystemExit(
                "M21.1extra-B2flat writes only under /tmp/ (got %r)" % p)

    d = run_flatten(args.symbol, confirm=bool(args.confirm))
    d["data_source"] = "real_ibkr_paper_gateway"
    Path(args.report).write_text(
        _render(d, "real_ibkr_paper_gateway"), encoding="utf-8")
    Path(args.json_out).write_text(json.dumps(d, indent=2), encoding="utf-8")
    print("wrote %s" % args.report)
    print("wrote %s" % args.json_out)
    print("symbol=%s flatten_confirmed=%s close_order_placed=%s "
          "entry_order_originated=%s"
          % (d.get("symbol"), d.get("flatten_confirmed"),
             d.get("close_order_placed", False),
             d.get("entry_order_originated", False)))


if __name__ == "__main__":
    main()
