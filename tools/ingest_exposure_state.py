"""
tools/ingest_exposure_state.py — M14.D operator-only exposure CLI.

Read-only. NEVER writes to a broker. Per ChatGPT M14.D correction
constraints:
  * no --demo
  * no --base-url
  * no --override-*
  * --dry-run makes no real-DB writes (uses an in-memory throwaway
    conn AND the orchestrator's dry_run flag).
  * --all continues through every scope; exits non-zero if any
    required scope returned EXPOSURE_UNKNOWN.
  * no scheduler / cron / systemd timer.

Mirrors the safety surface of tools/etoro_live_write.py and
tools/ingest_risk_state.py:
  * loads <repo>/.env via load_dotenv (no override of exported vars);
  * never prints secrets;
  * never instantiates a writer (no live-broker construction).
  * importing this module does NOT contact any broker.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(
    level=os.environ.get("EXPOSURE_INGEST_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [ingest_exposure_state] %(message)s",
)
log = logging.getLogger(__name__)


def _load_env(repo_root: Path = _REPO_ROOT) -> bool:
    env_path = repo_root / ".env"
    if not env_path.exists():
        log.warning("[ingest_exposure_state] no .env at %s — using current "
                    "environment only", env_path)
        return False
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
        log.info("[ingest_exposure_state] loaded .env from %s", env_path)
        return True
    except Exception as e:
        log.warning("[ingest_exposure_state] could not load .env: %s", e)
        return False


_load_env()


def _resolve_db_path(arg_db: Optional[str]) -> str:
    if arg_db:
        return arg_db
    return os.environ.get("SIGNALS_DB_PATH") or \
        str(_REPO_ROOT / "data" / "signals.db")


def _build_etoro_exposure_adapter(scope: str):
    """Wire EtoroExposureAdapter against the existing M13.2 read
    surface. If keys are absent, return an adapter that produces
    UNKNOWN('keys_absent') without contacting the API."""
    from bot.risk_authority.ingest_etoro_exposure import EtoroExposureAdapter

    api_key  = (os.environ.get("ETORO_REAL_API_KEY")  or "").strip()
    user_key = (os.environ.get("ETORO_REAL_USER_KEY") or "").strip()
    if not api_key or not user_key:
        def _reader():
            raise RuntimeError("keys missing for etoro: keys_absent")
        return EtoroExposureAdapter(broker_scope=scope,
                                    portfolio_reader=_reader)
    from bot.etoro.client import EtoroClient
    from bot.etoro.read_adapter import EtoroReadAdapter
    client = EtoroClient(api_key=api_key, user_key=user_key)
    read = EtoroReadAdapter(client)
    def _reader():
        return read.get_portfolio()
    return EtoroExposureAdapter(broker_scope=scope,
                                portfolio_reader=_reader)


def _build_ibkr_exposure_adapter(scope: str):
    """Wire IBKRExposureAdapter.

    M15.5 (commit chain after `d73a04a`) wires the `ibkr_paper`
    positions reader to a read-only IB API session via
    `bot.risk_authority.ibkr_paper_reader.make_ibkr_paper_positions_reader`.
    The session is gated behind the M15.4 `bot.gateway_health` readiness
    check and connects with `readonly=True`; AST-asserted no order
    methods (placeOrder/cancelOrder/modifyOrder/reqGlobalCancel) appear
    anywhere in the reader module.

    `ibkr_live` REMAINS unwired by design — paper mode only in M15.5.
    Any future live wiring requires a separately approved milestone
    and is NOT in scope here.
    """
    from bot.risk_authority.ingest_ibkr_exposure import IBKRExposureAdapter

    if scope == "ibkr_live":
        def _reader_live():
            raise NotImplementedError(
                "ibkr_live exposure reader is intentionally not wired in "
                "M15.5; paper mode only. Live wiring requires a separately "
                "approved milestone."
            )
        return IBKRExposureAdapter(broker_scope=scope,
                                    positions_reader=_reader_live)

    if scope == "ibkr_paper":
        from bot.risk_authority.ibkr_paper_reader import (
            make_ibkr_paper_positions_reader,
        )
        _reader_paper = make_ibkr_paper_positions_reader()
        return IBKRExposureAdapter(broker_scope=scope,
                                    positions_reader=_reader_paper)

    raise ValueError(f"unsupported IBKR scope {scope!r}")


def _resolve_adapter(scope: str):
    if scope in ("etoro_real", "etoro_paper"):
        return _build_etoro_exposure_adapter(scope)
    if scope in ("ibkr_live", "ibkr_paper"):
        return _build_ibkr_exposure_adapter(scope)
    raise ValueError(f"unsupported scope {scope!r}")


def cmd_run(args) -> int:
    from bot.risk_authority.ingest_exposure import (
        INGESTIBLE_SCOPES,
        ingest_exposure_once,
    )
    from bot.flywheel import init_flywheel_tables
    from bot.risk_authority.ingest_audit import get_ingest_audit_logger

    # Same factory M14.C's CLI uses. We pass a distinct path so M14.D's
    # exposure audit history lands in data/risk_exposure.log instead of
    # M14.C's data/risk_ingest.log. Returns a bot.etoro.audit.AuditLogger
    # with the same redaction guarantees.
    audit = get_ingest_audit_logger(_REPO_ROOT / "data" / "risk_exposure.log")
    db_path = _resolve_db_path(args.db)

    if args.scope and args.all_scopes:
        log.error("--scope and --all are mutually exclusive")
        return 2

    if args.all_scopes:
        scopes_list = sorted(INGESTIBLE_SCOPES)
    else:
        if not args.scope:
            log.error("must specify --scope <name> or --all")
            return 2
        if args.scope not in INGESTIBLE_SCOPES:
            log.error("scope %r not in %s",
                      args.scope, sorted(INGESTIBLE_SCOPES))
            return 2
        scopes_list = [args.scope]

    if args.dry_run:
        log.info("[ingest_exposure_state] DRY RUN — no DB writes will occur")

    overall_unknown = False
    for scope in scopes_list:
        try:
            adapter = _resolve_adapter(scope)
        except Exception as e:
            log.error("[ingest_exposure_state] adapter resolution failed "
                      "for %s: %s", scope, type(e).__name__)
            overall_unknown = True
            continue

        if args.dry_run:
            # Per ChatGPT M14.D correction #6 — even though dry-run
            # uses :memory:, we MUST call init_flywheel_tables(conn)
            # first so missing tables don't mask real dry-run behaviour.
            conn = sqlite3.connect(":memory:")
            try:
                init_flywheel_tables(conn)
            except Exception as e:
                log.error("[ingest_exposure_state] dry-run init failed: %s",
                          e)
                conn.close()
                overall_unknown = True
                continue
        else:
            conn = sqlite3.connect(db_path)

        try:
            result = ingest_exposure_once(
                conn, scope=scope, today=args.today,
                adapter=adapter, dry_run=args.dry_run,
                audit_logger=audit,
            )
        finally:
            conn.close()

        print(f"[{scope}] {result}")
        if result.get("quality") == "exposure_unknown":
            overall_unknown = True

    if overall_unknown and args.fail_on_unknown:
        return 1
    if overall_unknown:
        log.warning("[ingest_exposure_state] one or more scopes returned "
                    "EXPOSURE_UNKNOWN")
        return 1
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ingest_exposure_state",
        description="M14.D read-only exposure ingestion. Writes to "
                    "daily_state_per_broker and broker_positions only. "
                    "NEVER contacts a broker write endpoint; NEVER "
                    "places an order.",
    )
    parser.add_argument("--db", default=None,
                        help="SQLite path. Defaults to "
                             "<repo>/data/signals.db.")
    parser.add_argument("--scope", default=None,
                        help="ibkr_live|ibkr_paper|etoro_real|etoro_paper")
    parser.add_argument("--all", dest="all_scopes", action="store_true",
                        help="Ingest every supported scope; exits "
                             "non-zero if any returns EXPOSURE_UNKNOWN.")
    parser.add_argument("--today", default=None,
                        help="YYYY-MM-DD (UTC); defaults to today.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run adapters but make NO real-DB writes.")
    parser.add_argument("--fail-on-unknown", action="store_true",
                        default=True,
                        help="(default) Exit non-zero on any UNKNOWN.")
    # Deliberately ABSENT (do NOT add):
    #   --demo
    #   --base-url
    #   --override-*
    args = parser.parse_args(argv)
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
