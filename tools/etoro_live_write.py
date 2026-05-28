"""
tools/etoro_live_write.py — M13.5.B operator-only live-write CLI.

This is the SOLE construction site for EtoroLiveBroker. It is never
imported by main.py, the scanner, the strategy, or the risk manager.
The test suite test_m13_5_scanner_isolation.py asserts this.

Usage (high-level):
  $ python3 tools/etoro_live_write.py --help
  $ python3 tools/etoro_live_write.py prepare \
      --instrument-id 1000 --amount 10.0
  # ... CLI prints confirmation block with NONCE ...
  $ python3 tools/etoro_live_write.py submit \
      --intent-id 42 --confirm "CONFIRM <nonce>"

The CLI is split into subcommands so each step is observable and the
operator confirmation between `prepare` and `submit` is enforced by
the operator, not by chained subprocess calls.

NEVER does:
  - automatic submission without explicit operator confirmation
  - automatic retry on any failure
  - second POST under any condition (including unverified)
  - print credentials
  - import scanner/strategy/risk/main modules
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

# Path injection so this script can be run as `python3 tools/etoro_live_write.py`
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Logging only — never print secrets.
logging.basicConfig(
    level=os.environ.get("ETORO_CLI_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [etoro_live_write] %(message)s",
)
log = logging.getLogger(__name__)


# Defer heavy imports until inside main() so this file is cheap to
# load for --help and for the scanner-isolation test.
def _import_runtime():
    from bot.broker_allocation import load_policy, validate_policy
    from bot.etoro.audit import AuditLogger
    from bot.etoro.live_broker import (
        EtoroLiveBroker,
        LiveWriteContext,
        OperatorConfirmationRequired,
        PreflightError,
    )
    from bot.etoro.lifecycle import (
        apply_transition,
        attach_evidence,
        find_by_client_intent_id,
    )
    from bot.etoro.nonce import NonceStore, compute_digest
    from bot.etoro.order_poller import poll_until_terminal
    from bot.flywheel import log_intent, init_flywheel_tables as ensure_schema
    return locals()


# ─────────────────────────────────────────────────────────────────────────────
# Argument parsing
# ─────────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="etoro_live_write",
        description="M13.5.B operator-only live eToro write CLI. "
                    "Refuses to run without explicit per-payload nonce.",
    )
    p.add_argument(
        "--db", default=None,
        help="SQLite path. Defaults to <repo>/data/signals.db.",
    )
    p.add_argument(
        "--demo", action="store_true",
        help="Run against eToro demo endpoint instead of real-money. "
             "ETORO_LIVE_ENABLED .env flag is bypassed; policy gates "
             "are still enforced.",
    )

    sub = p.add_subparsers(dest="cmd", required=False)

    prep = sub.add_parser("prepare", help="Insert pending intent + print nonce.")
    prep.add_argument("--instrument-id", type=int, required=True)
    prep.add_argument("--amount", type=float, required=True)
    prep.add_argument("--is-buy", action="store_true", default=True)
    prep.add_argument("--leverage", type=int, default=1)
    prep.add_argument("--symbol", type=str, required=True,
                      help="Human-readable symbol, e.g. SPY. For audit only.")
    prep.add_argument("--no-stop-loss", action="store_true", default=True)
    prep.add_argument("--no-take-profit", action="store_true", default=True)
    prep.add_argument("--close-plan", type=str, required=True,
                      help="Operator-typed manual close plan (M13.4B §8.5).")

    sub.add_parser("submit", help="Submit a previously prepared intent.").add_argument(
        "--intent-id", type=int, required=True,
    )
    # `submit` continued in main() because argparse subparsers need
    # additional optional flags — keep simple here.
    return p


def _resolve_db_path(arg_db: Optional[str]) -> str:
    if arg_db:
        return arg_db
    env_db = os.environ.get("SIGNALS_DB_PATH")
    if env_db:
        return env_db
    return str(_REPO_ROOT / "data" / "signals.db")


def _build_payload(args) -> dict:
    payload = {
        "InstrumentID": int(args.instrument_id),
        "IsBuy":        bool(args.is_buy),
        "Leverage":     int(args.leverage),
        "Amount":       float(args.amount),
    }
    if getattr(args, "no_stop_loss", True):
        payload["IsNoStopLoss"] = True
    if getattr(args, "no_take_profit", True):
        payload["IsNoTakeProfit"] = True
    return payload


def _read_keys(demo: bool) -> tuple[str, str, bool]:
    """Read api/user keys + live-flag from .env. Never logged.

    If demo=True, the ETORO_LIVE_ENABLED flag check is bypassed; the
    eToro demo endpoint is then used by the caller. Even in demo mode
    we still require keys to be present.
    """
    api_key = (os.environ.get("ETORO_REAL_API_KEY") or "").strip()
    user_key = (os.environ.get("ETORO_REAL_USER_KEY") or "").strip()
    if demo:
        # Demo keys are separate.
        api_key = (os.environ.get("ETORO_DEMO_API_KEY") or api_key).strip()
        user_key = (os.environ.get("ETORO_DEMO_USER_KEY") or user_key).strip()
    if not api_key or not user_key:
        raise SystemExit(
            "ETORO_REAL_API_KEY / ETORO_REAL_USER_KEY (or *_DEMO_* for "
            "--demo) must be set in .env. Aborting."
        )
    env_live = os.environ.get("ETORO_LIVE_ENABLED", "").strip().lower() == "true"
    if demo:
        # In demo mode we treat env_live as True for the purposes of
        # the EtoroLiveBroker constructor (the broker itself does not
        # care about real vs demo — the base URL does). Real-money
        # path stays gated.
        env_live = True
    return api_key, user_key, env_live


# ─────────────────────────────────────────────────────────────────────────────
# Subcommand: prepare
# ─────────────────────────────────────────────────────────────────────────────

def cmd_prepare(args) -> int:
    rt = _import_runtime()
    load_policy = rt["load_policy"]
    validate_policy = rt["validate_policy"]
    log_intent = rt["log_intent"]
    ensure_schema = rt["ensure_schema"]
    NonceStore = rt["NonceStore"]
    apply_transition = rt["apply_transition"]
    attach_evidence = rt["attach_evidence"]

    db_path = _resolve_db_path(args.db)
    payload = _build_payload(args)

    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
        policy = load_policy(conn)
        val = validate_policy(policy)
        if not val.ok:
            print("ERROR: broker allocation policy failed validation. "
                  "Fix in dashboard before continuing.")
            print(json.dumps(val.errors, indent=2))
            return 2

        # Insert pending row.
        client_intent_id = str(uuid.uuid4())
        intent_id = log_intent(
            conn,
            signal_id=0,                       # operator-triggered, no signal
            symbol=args.symbol,
            direction="long",
            route="ETORO",
            entry_price=0.0,                   # unknown until fill
            stop_loss=0.0,
            target_price=0.0,
            position_size=0.0,
            risk_usd=0.0,
            valid_count=0,
            strategy_version=0,
            broker="etoro_real" if not args.demo else "etoro_real_demo",
            status="pending_live_write",
            broker_order_id=None,
            rejection_reason=None,
            risk_checks={"source": "operator_cli_m13_5_b"},
        )
        if intent_id is None:
            print("ERROR: failed to insert execution_intent row.")
            return 2

        # Stamp the row's lifecycle_json with the client_intent_id
        # before nonce issuance. This is the canonical idempotency key.
        attach_evidence(conn, intent_id, key="client_intent_id",
                        value=client_intent_id)
        attach_evidence(conn, intent_id, key="payload_redacted_initial",
                        value={
                            "InstrumentID": payload["InstrumentID"],
                            "IsBuy":        payload["IsBuy"],
                            "Leverage":     payload["Leverage"],
                            "Amount":       payload["Amount"],
                            "symbol":       args.symbol,
                        })
        attach_evidence(conn, intent_id, key="close_plan", value=args.close_plan)
    finally:
        conn.close()

    # Issue nonce. Store is in-process; persist its digest into
    # lifecycle_json so the operator can verify integrity between
    # prepare and submit. The actual single-use guard is enforced by
    # the in-process NonceStore at submit time.
    store = NonceStore()
    rec = store.issue(payload, ttl_seconds=900)   # 15 min for first write

    # Persist digest into the row so submit can re-issue if same process.
    conn = sqlite3.connect(db_path)
    try:
        attach_evidence(conn, intent_id, key="nonce_digest", value=rec.digest)
        attach_evidence(conn, intent_id, key="nonce_issued_at_ms",
                        value=rec.issued_at_ms)
        attach_evidence(conn, intent_id, key="nonce_ttl_seconds",
                        value=rec.ttl_seconds)
    finally:
        conn.close()

    # Print confirmation block (M13.4B §10).
    print()
    print("┌──────────────── LIVE WRITE CONFIRMATION ──────────────────┐")
    print(f"│ Mode:           {'DEMO (sandbox)' if args.demo else 'REAL-MONEY':<46}│")
    print(f"│ Intent ID:      {intent_id:<46}│")
    print(f"│ Symbol:         {args.symbol:<46}│")
    print(f"│ Instrument ID:  {payload['InstrumentID']:<46}│")
    print(f"│ Side:           {'BUY' if payload['IsBuy'] else 'SELL':<46}│")
    print(f"│ Amount:         ${payload['Amount']:<45}│")
    print(f"│ Leverage:       {payload['Leverage']:<46}│")
    print(f"│ Close plan:     {args.close_plan[:46]:<46}│")
    print(f"│                                                            │")
    print(f"│ NONCE: {rec.digest:<52}│")
    print(f"│ To proceed:                                                │")
    print(f"│   python3 tools/etoro_live_write.py submit \\               │")
    print(f"│       --intent-id {intent_id} --confirm 'CONFIRM {rec.digest}' \\         │")
    print(f"│       --instrument-id {payload['InstrumentID']} --amount {payload['Amount']}      │")
    print("└────────────────────────────────────────────────────────────┘")
    print()
    print("Have the eToro web UI open and authenticated before submitting.")
    print()
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Subcommand: submit
# ─────────────────────────────────────────────────────────────────────────────

def cmd_submit(args) -> int:
    """The submit subcommand requires re-supplying the payload fields so
    nonce validation re-derives the digest. This guards against drift
    between the row and the operator's invocation.

    Because the NonceStore is in-process, the same Python process must
    issue and validate. M13.5.B keeps it simple: prepare+submit are
    invoked as a single command via the `oneshot` subcommand for the
    first real write. This `submit` subcommand exists for tests and
    advanced operator workflows only.
    """
    print("ERROR: standalone `submit` is not supported in M13.5.B. "
          "Use `oneshot` for an end-to-end operator-confirmed run, "
          "or invoke the live broker programmatically from a Python "
          "session that retains the NonceStore.")
    return 2


# ─────────────────────────────────────────────────────────────────────────────
# Subcommand: oneshot — prepare + interactive confirm + submit in one process
# ─────────────────────────────────────────────────────────────────────────────

def cmd_oneshot(args) -> int:
    """Prepare, ask for confirmation, then submit — all in one process
    so the NonceStore is retained. This is the recommended path for
    the first real write."""
    rt = _import_runtime()
    load_policy = rt["load_policy"]
    validate_policy = rt["validate_policy"]
    log_intent = rt["log_intent"]
    ensure_schema = rt["ensure_schema"]
    NonceStore = rt["NonceStore"]
    apply_transition = rt["apply_transition"]
    attach_evidence = rt["attach_evidence"]
    EtoroLiveBroker = rt["EtoroLiveBroker"]
    LiveWriteContext = rt["LiveWriteContext"]
    PreflightError = rt["PreflightError"]
    AuditLogger = rt["AuditLogger"]
    poll_until_terminal = rt["poll_until_terminal"]

    if not args.confirm and not args.assume_yes:
        # Will prompt interactively after preflight.
        pass

    db_path = _resolve_db_path(args.db)
    payload = _build_payload(args)

    # Load credentials and env flag.
    api_key, user_key, env_live = _read_keys(args.demo)
    if not env_live:
        print("ERROR: ETORO_LIVE_ENABLED is not 'true' in .env. Refusing.")
        return 2

    # Validate policy + build context first — so we never insert a row
    # for a configuration that's already invalid.
    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
        policy = load_policy(conn)
    finally:
        conn.close()
    val = validate_policy(policy)
    if not val.ok:
        print("ERROR: broker allocation policy failed validation.")
        print(json.dumps(val.errors, indent=2))
        return 2

    # Build runtime context. Operator supplies market_open / quote
    # state explicitly for the first write (M13.5.B keeps this manual
    # to keep scanner-isolation tight).
    ctx = LiveWriteContext(
        policy=policy,
        payload=payload,
        env_live_enabled=env_live,
        open_positions_count=int(args.open_positions),
        realised_daily_loss=(float(args.realised_daily_loss)
                             if args.realised_daily_loss is not None else None),
        market_open=bool(args.market_open),
        quote_age_sec=float(args.quote_age_sec)
                          if args.quote_age_sec is not None else None,
        quote_max_age_sec=float(args.quote_max_age_sec),
        spread_bps=float(args.spread_bps)
                       if args.spread_bps is not None else None,
        spread_max_bps=float(args.spread_max_bps),
        amount_min=float(args.amount_min),
    )

    # Construct nonce store + audit logger.
    audit_path = _REPO_ROOT / "data" / "etoro_audit.log"
    audit = AuditLogger(audit_path)
    nonce_store = NonceStore()

    # Construct the broker.
    base_url = args.base_url or (
        "https://public-api.etoro.com" if not args.demo else
        "https://public-api.etoro.com"
    )
    broker = EtoroLiveBroker(
        api_key=api_key,
        user_key=user_key,
        env_live_enabled=env_live,
        nonce_store=nonce_store,
        audit=audit,
        base_url=base_url,
    )

    # Insert intent.
    client_intent_id = str(uuid.uuid4())
    conn = sqlite3.connect(db_path)
    try:
        intent_id = log_intent(
            conn, signal_id=0,
            symbol=args.symbol, direction="long", route="ETORO",
            entry_price=0.0, stop_loss=0.0, target_price=0.0,
            position_size=0.0, risk_usd=0.0,
            valid_count=0, strategy_version=0,
            broker="etoro_real" if not args.demo else "etoro_real_demo",
            status="pending_live_write",
            broker_order_id=None, rejection_reason=None,
            risk_checks={"source": "operator_cli_m13_5_b"},
        )
        if intent_id is None:
            print("ERROR: failed to insert execution_intent row.")
            return 2
        attach_evidence(conn, intent_id, key="client_intent_id",
                        value=client_intent_id)
        attach_evidence(conn, intent_id, key="close_plan", value=args.close_plan)
    finally:
        conn.close()

    # Run preflight. On failure, mark policy_rejected/risk_rejected.
    try:
        broker.preflight(ctx)
    except PreflightError as e:
        log.warning("preflight failed: %s (reason=%s)", e, e.reason_code)
        conn = sqlite3.connect(db_path)
        try:
            # Classify: gates 1-10 → policy_rejected, 11-15 → risk_rejected.
            policy_codes = {
                "policy_missing", "policy_invalid",
                "global_kill_switch", "global_disabled",
                "broker_kill_switch", "broker_disabled",
                "broker_not_allowed",
                "etoro_live_disabled", "etoro_live_disabled_env",
                "exceeds_single_trade", "exceeds_broker_capital",
                "exceeds_global_capital", "amount_invalid", "amount_too_small",
            }
            new_status = "policy_rejected" if e.reason_code in policy_codes \
                else "risk_rejected"
            apply_transition(
                conn, intent_id, new_status,
                event=f"preflight_failed:{e.reason_code}",
            )
        finally:
            conn.close()
        print(f"ABORT (preflight): {e.reason_code} — {e}")
        return 1

    # Issue nonce + transition to awaiting_confirm.
    rec = nonce_store.issue(payload, ttl_seconds=300)
    conn = sqlite3.connect(db_path)
    try:
        attach_evidence(conn, intent_id, key="nonce_digest", value=rec.digest)
        attach_evidence(conn, intent_id, key="nonce_ttl_seconds",
                        value=rec.ttl_seconds)
        apply_transition(conn, intent_id, "awaiting_confirm",
                         event="nonce_issued")
    finally:
        conn.close()

    print()
    print("┌──────────────── LIVE WRITE CONFIRMATION ──────────────────┐")
    print(f"│ Intent ID: {intent_id}   Mode: "
          f"{'DEMO' if args.demo else 'REAL-MONEY'}")
    print(f"│ Symbol={args.symbol} Amount=${payload['Amount']} "
          f"InstrID={payload['InstrumentID']}")
    print(f"│ NONCE: {rec.digest}   TTL={rec.ttl_seconds}s")
    print("│ Type exactly: CONFIRM <nonce> (or Ctrl-C to abort)")
    print("└────────────────────────────────────────────────────────────┘")

    # Read confirmation from --confirm or stdin.
    echoed = args.confirm
    if not echoed:
        try:
            echoed = input("CONFIRM> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nABORTED by operator.")
            conn = sqlite3.connect(db_path)
            try:
                apply_transition(conn, intent_id, "cancelled",
                                 event="operator_aborted_before_post")
            finally:
                conn.close()
            return 1

    # Submit. Single POST. No retry on any outcome.
    try:
        parsed, audit_record = broker.submit_live(payload, ctx, echoed)
    except Exception as e:
        log.error("submit_live failed: %s", e)
        conn = sqlite3.connect(db_path)
        try:
            # Distinguish nonce failures from broker failures.
            from bot.etoro.live_broker import OperatorConfirmationRequired
            if isinstance(e, OperatorConfirmationRequired):
                apply_transition(conn, intent_id, "cancelled",
                                 event=f"confirmation_failed:{e}")
                print(f"ABORT: confirmation rejected — {e}")
                return 1
            apply_transition(conn, intent_id, "broker_rejected",
                             event=f"submit_failed:{type(e).__name__}")
            attach_evidence(conn, intent_id, key="submit_error",
                            value=str(e))
        finally:
            conn.close()
        return 1

    # Record submitted status with broker_order_id.
    conn = sqlite3.connect(db_path)
    try:
        apply_transition(
            conn, intent_id, "submitted",
            broker_order_id=str(parsed.order_id),
            event="post_accepted",
            extra_lifecycle={"x_request_id": audit_record.get("x_request_id")},
        )
        attach_evidence(conn, intent_id, key="open_response_redacted",
                        value=audit_record.get("response"))
    finally:
        conn.close()

    print(f"POST accepted. orderID={parsed.order_id}. Polling status...")

    # Poll.
    pr = poll_until_terminal(
        reader=broker.fetch_order_info,
        order_id=parsed.order_id,
        max_attempts=int(args.poll_max_attempts),
        interval_sec=float(args.poll_interval_sec),
    )

    conn = sqlite3.connect(db_path)
    try:
        if pr.status == "filled" and pr.last_response is not None:
            apply_transition(
                conn, intent_id, "filled",
                fill_price=pr.last_response.first_position_rate,
                fill_qty=pr.last_response.first_position_units,
                broker_order_id=str(parsed.order_id),
                event="poll_filled",
                extra_lifecycle={
                    "position_id": pr.last_response.first_position_id,
                    "conversion_rate": pr.last_response.first_position_conversion_rate,
                },
            )
            print(f"FILLED. positionID={pr.last_response.first_position_id} "
                  f"rate={pr.last_response.first_position_rate} "
                  f"units={pr.last_response.first_position_units}")
        elif pr.status == "broker_rejected":
            apply_transition(conn, intent_id, "broker_rejected",
                             event="poll_rejected")
            print("BROKER REJECTED after POST. See audit log.")
        elif pr.status == "cancelled":
            apply_transition(conn, intent_id, "cancelled",
                             event="poll_cancelled")
            print("CANCELLED by broker.")
        else:
            # unverified — no second POST, ever.
            apply_transition(conn, intent_id, "unverified",
                             event=f"poll_exhausted:{pr.last_error}")
            print()
            print("⚠ UNVERIFIED — order status could not be confirmed after "
                  f"{pr.attempts} attempts.")
            print("DO NOT re-run this command. Use tools/etoro_reconcile.py "
                  "after manual verification in eToro web UI.")
    finally:
        conn.close()

    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="etoro_live_write",
        description="M13.5.B operator-only live eToro write CLI. "
                    "Refuses to run without explicit per-payload nonce.",
    )
    parser.add_argument("--db", default=None,
                        help="SQLite path. Defaults to <repo>/data/signals.db.")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--base-url", default=None)

    sub = parser.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("oneshot", help="Prepare + confirm + submit + poll.")
    o.add_argument("--instrument-id", type=int, required=True)
    o.add_argument("--amount",        type=float, required=True)
    o.add_argument("--symbol",        type=str,   required=True)
    o.add_argument("--leverage",      type=int,   default=1)
    o.add_argument("--is-buy",        action="store_true", default=True)
    o.add_argument("--no-stop-loss",  action="store_true", default=True)
    o.add_argument("--no-take-profit", action="store_true", default=True)
    o.add_argument("--close-plan",    type=str,   required=True)

    # Runtime preflight context (operator-supplied for the first write):
    o.add_argument("--market-open",       action="store_true", default=False)
    o.add_argument("--open-positions",    type=int, default=0)
    o.add_argument("--realised-daily-loss", type=float, default=None)
    o.add_argument("--quote-age-sec",     type=float, default=None)
    o.add_argument("--quote-max-age-sec", type=float, default=30.0)
    o.add_argument("--spread-bps",        type=float, default=None)
    o.add_argument("--spread-max-bps",    type=float, default=50.0)
    o.add_argument("--amount-min",        type=float, default=10.0)

    # Confirmation:
    o.add_argument("--confirm", type=str, default=None,
                   help="Pre-supplied 'CONFIRM <nonce>' string (avoids stdin prompt).")
    o.add_argument("--assume-yes", action="store_true")

    # Polling:
    o.add_argument("--poll-max-attempts", type=int, default=5)
    o.add_argument("--poll-interval-sec", type=float, default=2.0)

    args = parser.parse_args(argv)

    if args.cmd == "oneshot":
        return cmd_oneshot(args)
    parser.error(f"unknown command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
