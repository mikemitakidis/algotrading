"""
tools/ingest_risk_state.py — M14.C operator-only ingestion CLI.

Read-only. NEVER writes to a broker. Per ChatGPT M14.C correction #6:
  * no --demo
  * no --base-url
  * no --override-realised-pnl
  * --dry-run makes no DB writes
  * --all continues through every scope; exits non-zero if any required
    scope reported UNKNOWN
  * no scheduler / cron / systemd timer in M14.C

Mirrors the safety surface of tools/etoro_live_write.py:
  * loads <repo>/.env via load_dotenv (existing values not overridden);
  * never prints secrets;
  * never constructs EtoroLiveBroker (live writer remains exclusively
    operator-confirmed via tools/etoro_live_write.py);
  * importing this module does NOT contact any broker.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(
    level=os.environ.get("RISK_INGEST_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [ingest_risk_state] %(message)s",
)
log = logging.getLogger(__name__)


def _load_env(repo_root: Path = _REPO_ROOT) -> bool:
    """Mirror tools/etoro_live_write.py: read <repo>/.env into os.environ
    without overriding exported values. Never prints secrets."""
    env_path = repo_root / ".env"
    if not env_path.exists():
        log.warning("[ingest_risk_state] no .env at %s — using current "
                    "environment only", env_path)
        return False
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
        log.info("[ingest_risk_state] loaded .env from %s", env_path)
        return True
    except Exception as e:                              # pragma: no cover
        log.warning("[ingest_risk_state] could not load .env: %s", e)
        return False


_load_env()


def _resolve_db_path(arg_db: Optional[str]) -> str:
    if arg_db:
        return arg_db
    return os.environ.get("SIGNALS_DB_PATH") or \
        str(_REPO_ROOT / "data" / "signals.db")


# ─── Production adapter wiring ────────────────────────────────────────────
# Each builder returns an adapter that returns honest readings. If the
# underlying broker is not reachable / credentials absent, the adapter
# returns UNKNOWN — never raises.

def _build_etoro_adapter(scope: str):
    """Wire an EtoroPnLAdapter against the existing M13.2 read surface.

    Returns an adapter whose .read(today=…) call uses
    EtoroReadAdapter.get_trade_history. If keys are absent, returns an
    adapter that yields UNKNOWN(keys_absent) without contacting the API.
    """
    from bot.risk_authority.ingest_etoro import EtoroPnLAdapter

    api_key  = (os.environ.get("ETORO_REAL_API_KEY")  or "").strip()
    user_key = (os.environ.get("ETORO_REAL_USER_KEY") or "").strip()
    if not api_key or not user_key:
        # Reader raises a recognisable "keys_absent" error; adapter
        # translates to UNKNOWN(keys_absent).
        def _reader(min_date: str):
            raise RuntimeError("keys missing for etoro: keys_absent")
        return EtoroPnLAdapter(broker_scope=scope, history_reader=_reader)

    # Lazy imports — only touched when keys are actually present.
    from bot.etoro.client import EtoroClient
    from bot.etoro.read_adapter import EtoroReadAdapter
    client = EtoroClient(api_key=api_key, user_key=user_key)
    read = EtoroReadAdapter(client)
    def _reader(min_date: str):
        return read.get_trade_history(min_date=min_date)
    return EtoroPnLAdapter(broker_scope=scope, history_reader=_reader)


def _build_ibkr_adapter(scope: str):
    """Wire an IBKRPnLAdapter.

    M14.C ships the adapter shape, but the production executions-reader
    that bridges to the existing M11/M12 Gateway connection is NOT wired
    here — that bridging belongs to a follow-up (the operator can supply
    one via dependency injection in code). For CLI runs against the
    bare scope, the adapter returns UNKNOWN(executions_reader_failed:
    NotImplementedError) honestly, rather than fabricating zeros.
    """
    from bot.risk_authority.ingest_ibkr import IBKRPnLAdapter

    def _reader(today: str):
        raise NotImplementedError(
            "IBKR executions reader not yet wired to the live Gateway "
            "connection in M14.C. Pass an explicit reader from a Python "
            "session, or wait for the follow-up wiring."
        )
    return IBKRPnLAdapter(broker_scope=scope, executions_reader=_reader)


def _resolve_adapter(scope: str):
    if scope in ("etoro_real", "etoro_paper"):
        return _build_etoro_adapter(scope)
    if scope in ("ibkr_live", "ibkr_paper"):
        return _build_ibkr_adapter(scope)
    raise ValueError(f"unsupported scope {scope!r}")


# ─── CLI ──────────────────────────────────────────────────────────────────

def _redact_result(r: dict) -> dict:
    """Strip anything not safe to print to console."""
    out = dict(r)
    out.pop("error_full", None)
    return out


def cmd_run(args) -> int:
    from bot.risk_authority.ingest import (
        INGESTIBLE_SCOPES,
        ingest_all_scopes,
        ingest_once,
    )
    from bot.risk_authority.ingest_audit import get_ingest_audit_logger

    audit = get_ingest_audit_logger()
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
            log.error("scope %r not in %s", args.scope, sorted(INGESTIBLE_SCOPES))
            return 2
        scopes_list = [args.scope]

    if args.dry_run:
        # Dry-run still opens the DB read-only enough to surface errors,
        # but the orchestrator skips all UPSERTs.
        log.info("[ingest_risk_state] DRY RUN — no DB writes will occur")

    overall_unknown = False
    overall_db_error = False
    for scope in scopes_list:
        try:
            adapter = _resolve_adapter(scope)
        except Exception as e:
            log.error("[ingest_risk_state] adapter resolution failed for %s: "
                      "%s", scope, type(e).__name__)
            overall_unknown = True
            continue

        if args.dry_run:
            # Use a throwaway in-memory conn so any accidental write is
            # invisible to the real DB. The orchestrator's dry_run=True
            # also short-circuits UPSERTs internally.
            conn = sqlite3.connect(":memory:")
        else:
            conn = sqlite3.connect(db_path)
        try:
            result = ingest_once(
                conn, scope=scope, today=args.today,
                adapter=adapter, dry_run=args.dry_run, audit_logger=audit,
            )
        finally:
            conn.close()

        print(f"[{scope}] {_redact_result(result)}")
        if result.get("quality") == "unknown":
            overall_unknown = True
        if result.get("status") == "db_error":
            overall_db_error = True

    if overall_db_error:
        return 2
    if overall_unknown and args.fail_on_unknown:
        return 1
    if overall_unknown:
        log.warning("[ingest_risk_state] one or more scopes returned UNKNOWN")
        return 1   # default: unknowns are exit-1 so wrappers notice
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ingest_risk_state",
        description="M14.C read-only realised-PnL ingestion. Writes to "
                    "daily_state_per_broker only. NEVER contacts a broker "
                    "write endpoint; NEVER places an order.",
    )
    parser.add_argument("--db", default=None,
                        help="SQLite path. Defaults to <repo>/data/signals.db.")
    parser.add_argument("--scope", default=None,
                        help="ibkr_live|ibkr_paper|etoro_real|etoro_paper")
    parser.add_argument("--all", dest="all_scopes", action="store_true",
                        help="Ingest every supported scope; continues through "
                             "all scopes and exits non-zero if any required "
                             "scope returned UNKNOWN.")
    parser.add_argument("--today", default=None,
                        help="YYYY-MM-DD (UTC); defaults to today.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run adapters but make NO DB writes.")
    parser.add_argument("--fail-on-unknown", action="store_true", default=True,
                        help="(default) Exit non-zero if any scope is UNKNOWN.")
    # Deliberately ABSENT flags (do NOT add):
    #   --demo            (M13.5.B blocker fix; demo stays disabled)
    #   --base-url        (M13.5.B blocker fix; real endpoint pinned)
    #   --override-realised-pnl  (M14.C correction #6; manual override
    #                              remains on the live-write CLI only,
    #                              until M14.F)
    args = parser.parse_args(argv)
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
