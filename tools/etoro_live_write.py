"""
tools/etoro_live_write.py — M13.5.B operator-only live-write CLI.

This is the SOLE construction site for EtoroLiveBroker. It is never
imported by main.py, the scanner, the strategy, or the risk manager.
The test suite test_m13_5_scanner_isolation.py asserts this.

The CLI exposes a single subcommand, `oneshot`, which performs the
whole flow inside one process so the in-memory NonceStore is retained
between nonce issuance and confirmation:

  prepare intent -> 16-gate preflight -> issue per-payload nonce ->
  operator confirms (CONFIRM <nonce>) -> exactly one POST -> bounded
  poll (5x2s) -> terminal status or 'unverified'.

Usage:
  $ python3 tools/etoro_live_write.py --help
  $ python3 tools/etoro_live_write.py oneshot \
      --instrument-id 1000 --amount 10.0 --symbol SPY \
      --market-open --close-plan "manual close via eToro web UI"
  # CLI prints a confirmation block with a NONCE; at the CONFIRM>
  # prompt type exactly: CONFIRM <nonce>
  # (or pass --confirm "CONFIRM <nonce>" non-interactively)

.env is loaded automatically at startup (see _load_env), so the
operator does not need to manually `source` it before running.

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

# Logging only — never print secrets. Defined before _load_env() so the
# import-time .env load can log via this logger.
logging.basicConfig(
    level=os.environ.get("ETORO_CLI_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [etoro_live_write] %(message)s",
)
log = logging.getLogger(__name__)


def _load_env(repo_root: Path = _REPO_ROOT) -> bool:
    """Load <repo>/.env into os.environ so the operator does not have to
    manually `source` it before running this CLI (matches the runbook).

    Mirrors bot/config.py: guarded by an existence check; existing
    environment variables are NOT overridden (load_dotenv default
    override=False), so an explicitly-exported value wins. Returns True
    if a .env file was found and loaded. Never prints secrets.
    """
    env_path = repo_root / ".env"
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path)
            log.info("[etoro_live_write] loaded .env from %s", env_path)
            return True
        except Exception as e:  # pragma: no cover - defensive
            log.warning("[etoro_live_write] could not load .env: %s", e)
            return False
    log.warning("[etoro_live_write] no .env at %s — using current "
                "environment only", env_path)
    return False


# Load .env at import time so every code path (oneshot, _read_keys) sees
# the operator's configured keys + ETORO_LIVE_ENABLED.
_load_env()


# ─────────────────────────────────────────────────────────────────────────────
# Demo mode — DISABLED in M13.5.B
# ─────────────────────────────────────────────────────────────────────────────
# Demo mode is disabled until an official eToro demo/sandbox base URL is
# verified. Re-enabling it requires ALL of the following, with NO fallback
# to real credentials and NO bypass of ETORO_LIVE_ENABLED against a real
# endpoint:
#   * ETORO_DEMO_API_KEY
#   * ETORO_DEMO_USER_KEY
#   * ETORO_DEMO_BASE_URL   (a verified sandbox URL, never the real API)
DEMO_MODE_ENABLED = False
DEMO_DISABLED_MESSAGE = (
    "Demo mode is disabled until an official eToro demo/sandbox base URL "
    "is verified. Remove --demo to run against the real endpoint (which "
    "requires ETORO_LIVE_ENABLED=true and full preflight + nonce "
    "confirmation), or wait for a verified sandbox URL."
)


def _demo_guard(demo: bool) -> None:
    """Fail closed if --demo is requested while demo mode is disabled.

    This runs BEFORE any credential read or broker construction, so a
    --demo invocation can never reach key-loading, URL selection, or the
    live flag bypass.
    """
    if demo and not DEMO_MODE_ENABLED:
        raise SystemExit(DEMO_DISABLED_MESSAGE)


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


def _read_keys(demo: bool) -> tuple[str, str, bool, str]:
    """Read api/user keys + live-flag + base URL from the environment.
    Never logged. Returns (api_key, user_key, env_live, base_url).

    REAL mode (demo=False):
      * Uses ETORO_REAL_API_KEY / ETORO_REAL_USER_KEY.
      * env_live reflects ETORO_LIVE_ENABLED (no bypass).
      * base_url is the real public API.

    DEMO mode (demo=True):
      * Reaches here ONLY if DEMO_MODE_ENABLED is True (the caller runs
        _demo_guard first). It is currently disabled, so this branch is
        defensive.
      * Uses ETORO_DEMO_API_KEY / ETORO_DEMO_USER_KEY with NO fallback
        to real credentials.
      * REQUIRES ETORO_DEMO_BASE_URL — a verified sandbox URL. We refuse
        to use the real public API base for demo, and we never bypass
        ETORO_LIVE_ENABLED against a non-verified endpoint.
    """
    if demo:
        if not DEMO_MODE_ENABLED:
            # Defensive: should be unreachable because _demo_guard runs
            # first, but never silently proceed.
            raise SystemExit(DEMO_DISABLED_MESSAGE)
        api_key = (os.environ.get("ETORO_DEMO_API_KEY") or "").strip()
        user_key = (os.environ.get("ETORO_DEMO_USER_KEY") or "").strip()
        demo_base = (os.environ.get("ETORO_DEMO_BASE_URL") or "").strip()
        if not api_key or not user_key:
            raise SystemExit(
                "Demo mode requires ETORO_DEMO_API_KEY and "
                "ETORO_DEMO_USER_KEY. There is NO fallback to real "
                "credentials. Aborting."
            )
        if not demo_base:
            raise SystemExit(
                "Demo mode requires a verified ETORO_DEMO_BASE_URL "
                "(sandbox). The real public API base is never used for "
                "demo. Aborting."
            )
        # Demo runs against an explicitly verified sandbox URL only.
        return api_key, user_key, True, demo_base

    # REAL mode.
    api_key = (os.environ.get("ETORO_REAL_API_KEY") or "").strip()
    user_key = (os.environ.get("ETORO_REAL_USER_KEY") or "").strip()
    if not api_key or not user_key:
        raise SystemExit(
            "ETORO_REAL_API_KEY / ETORO_REAL_USER_KEY must be set in .env. "
            "Aborting."
        )
    env_live = os.environ.get("ETORO_LIVE_ENABLED", "").strip().lower() == "true"
    return api_key, user_key, env_live, "https://public-api.etoro.com"

# ─────────────────────────────────────────────────────────────────────────────
# Subcommand: oneshot — prepare + interactive confirm + submit in one process
# ─────────────────────────────────────────────────────────────────────────────

def cmd_oneshot(args) -> int:
    """Prepare, ask for confirmation, then submit — all in one process
    so the NonceStore is retained. This is the recommended path for
    the first real write."""
    # Fail closed FIRST — before importing runtime modules, reading any
    # credential, or constructing any broker — if --demo is requested
    # while demo mode is disabled.
    _demo_guard(args.demo)

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

    db_path = _resolve_db_path(args.db)
    payload = _build_payload(args)

    # ─── M14.F: Risk Authority preflight ──────────────────────────────
    # MUST run before:
    #   * _read_keys() / ETORO_LIVE_ENABLED env-flag check (so the
    #     Risk Authority is testable when env_live is false/absent);
    #   * any EtoroLiveBroker construction;
    #   * any nonce mint;
    #   * any schema validate / transport.
    #
    # The preflight is the ONLY surface that calls decide_and_audit
    # in this CLI. If Risk Authority blocks, we exit 4 immediately and
    # NEVER touch credentials, transport, or broker state.
    #
    # Per the approved M14.F plan: --authority allows the operator to
    # supply the current authority level; the engine still consults
    # every gate regardless of the supplied level. Default is
    # ONE_SHOT_MANUAL (the level required for a single live trade).
    from bot.risk_authority import Authority, run_risk_preflight
    from bot.risk_authority.engine import TradeRequest as RATradeRequest
    try:
        auth_level = Authority.from_string(args.authority)
    except ValueError as e:
        print(f"ERROR: --authority {args.authority!r}: {e}")
        return 2

    preflight_request = RATradeRequest(
        symbol=args.symbol,
        amount_usd=float(args.amount),
        side=("long" if args.is_buy else "short"),
        leverage=int(args.leverage),
    )
    conn_pf = sqlite3.connect(db_path)
    try:
        from bot.flywheel import init_flywheel_tables as _ensure_schema
        _ensure_schema(conn_pf)        # ensure risk_snapshots/risk_decisions exist
        preflight = run_risk_preflight(
            conn_pf,
            broker_scope="etoro_real",
            request=preflight_request,
            current_authority=auth_level,
            actor="operator",
            audit_source="manual",
            market_open=bool(args.market_open),
            quote_age_sec=args.quote_age_sec,
            quote_max_age_sec=float(args.quote_max_age_sec),
            spread_bps=args.spread_bps,
            spread_max_bps=float(args.spread_max_bps),
        )
    finally:
        conn_pf.close()

    if not preflight.allowed:
        # Risk Authority block. Exit 4 per the approved exit-code table.
        # NEVER touch transport, credentials, nonce, or broker
        # construction beyond this point.
        print("ERROR: Risk Authority blocked this live-write request.")
        print(f"  decision_id     = {preflight.decision_id}")
        print(f"  authority_before = {preflight.authority_before.name}")
        print(f"  authority_after  = {preflight.authority_after.name}")
        print(f"  reason_codes    = {list(preflight.reason_codes)}")
        for reason, path in preflight.recovery_paths.items():
            print(f"  recovery[{reason}] = {path}")
        return 4

    # Risk Authority allowed. Proceed into the existing M13.5 envelope
    # exactly as before — no gate is removed or weakened.
    # ───────────────────────────────────────────────────────────────────

    # Load credentials, env flag, and base URL. _read_keys never falls
    # back from demo to real credentials and never returns the real API
    # base URL for demo mode.
    api_key, user_key, env_live, base_url = _read_keys(args.demo)
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

    # Construct the broker. base_url comes solely from _read_keys: the
    # fixed real public API for real mode, or a verified sandbox URL for
    # demo (currently disabled). There is deliberately NO CLI override —
    # real credentials can never be redirected to an arbitrary host.
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
    parser.add_argument(
        "--demo", action="store_true",
        help="DISABLED in M13.5.B. Demo mode fails closed until an "
             "official eToro sandbox base URL is verified; it never "
             "falls back to real credentials or the real API.")

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

    # M14.F: operator-supplied authority level for the Risk Authority
    # preflight. Default ONE_SHOT_MANUAL (the minimum level required
    # for trade_open per REQUIRED_AUTHORITY). AUTO_ALLOWED is accepted
    # for operator/testing use but does NOT bypass any Risk Authority
    # gate — every decide() call walks all 25 gates regardless of
    # supplied authority.
    o.add_argument(
        "--authority",
        type=str,
        default="ONE_SHOT_MANUAL",
        choices=("OFF", "SIGNAL_ONLY", "PAPER_ONLY",
                 "ONE_SHOT_MANUAL", "AUTO_ALLOWED"),
        help="Operator-supplied authority level for the Risk Authority "
             "preflight. Default ONE_SHOT_MANUAL.",
    )

    # Confirmation: the operator must echo the per-payload nonce. There
    # is deliberately NO "assume yes" / skip-confirmation option — the
    # nonce is unpredictable by design, so it cannot be pre-approved
    # without the operator seeing it. --confirm lets the operator supply
    # the echoed nonce non-interactively AFTER reading it from a prior
    # run is not possible (nonce is single-use); it exists for automated
    # tests and advanced flows where the nonce is captured in-process.
    o.add_argument("--confirm", type=str, default=None,
                   help="Pre-supplied 'CONFIRM <nonce>' string (avoids the "
                        "interactive stdin prompt). Must still match the "
                        "per-payload nonce issued this run.")

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
