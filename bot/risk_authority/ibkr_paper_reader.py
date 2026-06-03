"""bot/risk_authority/ibkr_paper_reader.py — M15.5 IBKR paper positions reader.

Provides the production `positions_reader` callable that the existing
M14.D `IBKRExposureAdapter` was designed to accept. The adapter
already validates, derives, and fail-closes on every position; this
module's job is the narrow one of producing a list of raw dicts from
a live IB Gateway paper session.

Hard contract (AST-asserted in test_m15_5_ibkr_exposure.py):
  * Read-only IB API session. `IB.connect(...)` MUST pass
    `readonly=True`. The argument is non-default and the test fails
    the build if any call site omits it.
  * No order methods. Forbidden symbols (`placeOrder`, `cancelOrder`,
    `modifyOrder`, `reqGlobalCancel`, `reqMktData`, `reqHistoricalData`,
    `reqOpenOrders`, `reqExecutions`) never appear in this module.
    Likewise no `Order`, `Trade`, `MarketOrder`, `LimitOrder` imports.
  * `ibkr_paper` scope only. `ibkr_live` raises NotImplementedError;
    no 4001 path exists in this module.
  * M15.4 gateway health gate. Before connecting we call
    `bot.gateway_health.assemble_health` (injectable). If
    `ready_for_ibkr_trading` is false or `mode != 'paper'`, the reader
    refuses to contact IB and raises a typed exception; the existing
    adapter converts that into a fail-closed `EXPOSURE_UNKNOWN`.
  * Snapshot-ready cross-confirm (added in the M15.5 patch after
    138df9e on operator request). An empty `portfolio()` is treated as
    KNOWN-ZERO only when:
      (a) `accountValues()` is non-empty (proves the account-update
          subscription has delivered at least one batch), AND
      (b) `positions()` is also empty (independent IB API view),
          AND its symbol set agrees with `portfolio()`'s.
    Any disagreement, any incomplete snapshot, or any read failure
    raises `IBPaperReadError`. The M14.D adapter converts that to
    EXPOSURE_UNKNOWN. We NEVER report zero exposure on incomplete data.
  * Dry-run mode. When invoked via `run_paper_dryrun`, the reader
    performs the gateway health check + `IB.connect(readonly=True)` +
    the snapshot-ready wait + a single read pass through `portfolio()`
    AND `positions()`, then disconnects. Returns a structured summary
    suitable for an operator to inspect before the real ingest run.
  * Disconnects always. `IB.disconnect()` is called in a `finally`
    branch even if `portfolio()` raises.
  * No fake exposure. We forward what IB tells us through
    `_position_dict_from_portfolio_item`. The adapter decides
    known/known-zero/partial/unknown.

Timeout semantics (made explicit in the cross-confirm patch):
  * `connect_timeout` — passed to `ib.connect(...)`. Hard upper bound
    on TCP/auth handshake. If exceeded, `connect()` raises.
  * `api_timeout` — wall-clock bound on the snapshot-ready wait loop.
    `ib.portfolio()` and `ib.positions()` are SYNCHRONOUS cache reads
    that return whatever the account-update subscription has delivered
    so far; they do NOT accept a timeout. The api_timeout is therefore
    enforced on the explicit wait-for-snapshot phase that happens
    immediately after connect. If the snapshot does not arrive within
    `api_timeout`, the reader raises `IBPaperReadError` and the
    adapter returns EXPOSURE_UNKNOWN — never an empty/fake reading.

Reserved client ID:
    M15_5_CLIENT_ID = 15
Existing IDs (verified by grep on 2026-06-02):
    11 — PAPER_CLIENT_ID  (bot/brokers/ibkr_broker.py)
    12 — LIVE_CLIENT_ID   (bot/brokers/ibkr_broker.py)
    99 — WATCHDOG_CLIENT_ID (bot/gateway_watchdog.py via env var
                               GATEWAY_WATCHDOG_CLIENT_ID, default 99)
ID 15 is non-conflicting, mnemonic for "M15.5", and well-separated
from the existing reservations.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional, Set

log = logging.getLogger(__name__)

# Reserved client ID for M15.5. Non-conflicting with the IDs already
# reserved by the IBKR broker adapter (11, 12) and the gateway watchdog
# (99). Mnemonic: "M15.5".
M15_5_CLIENT_ID = 15

# Paper port. M15.5 is paper-mode-only by design; there is no 4001
# resolution path in this module.
IBKR_PAPER_HOST = "127.0.0.1"
IBKR_PAPER_PORT = 4002

# Connect / read timeouts (per approved plan).
DEFAULT_CONNECT_TIMEOUT_SEC = 5.0
DEFAULT_API_TIMEOUT_SEC = 5.0

# Snapshot-ready poll cadence. We use ib.waitOnUpdate(timeout=POLL_SEC)
# in a loop so the total wall-clock wait is bounded by api_timeout
# without spawning extra threads.
_SNAPSHOT_POLL_SEC = 0.25


class GatewayNotReadyError(RuntimeError):
    """Raised when bot.gateway_health.assemble_health() reports the
    gateway is not ready_for_ibkr_trading, or when mode is not 'paper'.
    The IBKRExposureAdapter catches RuntimeError and converts to
    EXPOSURE_UNKNOWN — fail-closed by construction."""


class IBPaperReadError(RuntimeError):
    """Raised when the IB session itself fails (connect timeout,
    network error, etc.). Adapter converts to EXPOSURE_UNKNOWN."""


def _default_health_checker():
    """Lazy import of M15.4 helper so a test can inject a stub without
    pulling bot.gateway_health into sys.modules in scanner-isolation
    subprocess checks."""
    from bot.gateway_health import assemble_health
    return assemble_health()


def _check_gateway_ready(*, scope: str, health_checker: Optional[Callable]) -> None:
    """Run the M15.4 readiness gate. Raises GatewayNotReadyError if
    the gateway is not ready for paper IBKR trading. No IB API call
    has happened yet at this point."""
    if scope != "ibkr_paper":
        # Strict guard: this module only operates on ibkr_paper. The
        # public-facing factory enforces it too, but defense-in-depth
        # for any direct caller.
        raise NotImplementedError(
            f"ibkr_paper_reader only supports scope='ibkr_paper'; "
            f"got {scope!r}"
        )
    checker = health_checker or _default_health_checker
    h = checker()
    if not isinstance(h, dict):
        raise GatewayNotReadyError(
            f"gateway health checker returned non-dict: {type(h).__name__}"
        )
    if not h.get("ready_for_ibkr_trading"):
        raise GatewayNotReadyError(
            f"gateway_not_ready: status={h.get('status')!r} "
            f"systemd_active={h.get('systemd_active')!r} "
            f"tcp_reachable={h.get('tcp_reachable')!r} "
            f"login_error_detected={h.get('login_error_detected')!r}"
        )
    if h.get("mode") != "paper":
        raise GatewayNotReadyError(
            f"gateway_mode_not_paper: mode={h.get('mode')!r}"
        )
    if h.get("expected_port") != IBKR_PAPER_PORT:
        raise GatewayNotReadyError(
            f"gateway_unexpected_port: expected_port={h.get('expected_port')!r}"
        )


def _position_dict_from_portfolio_item(item: Any) -> Dict[str, Any]:
    """Translate one ib_insync PortfolioItem into the raw-dict shape
    the existing IBKRExposureAdapter understands.

    We pass through whatever IB tells us without inventing values:
      * exposure_usd comes from PortfolioItem.marketValue (broker-quoted USD)
      * mark_price comes from PortfolioItem.marketPrice
      * avg_price comes from PortfolioItem.averageCost
      * unrealised_pnl_usd comes from PortfolioItem.unrealizedPNL
      * currency comes from contract.currency (the adapter rejects
        non-USD without broker-provided USD notional — we don't
        invent FX).

    The adapter's _derive_position() does the rest: rejects bool,
    rejects missing fields, fails closed on non-numeric. We do NOT
    pre-validate here — duplicating validation would create drift
    between this module and the adapter.
    """
    contract = getattr(item, "contract", None)
    sym = getattr(contract, "symbol", None) if contract else None
    qty = getattr(item, "position", None)
    side: Optional[str]
    if qty is None or not isinstance(qty, (int, float)) or isinstance(qty, bool):
        side = None
    elif qty > 0:
        side = "long"
    elif qty < 0:
        side = "short"
    else:
        # qty == 0 — flat position that broker hasn't pruned yet.
        # We forward as a malformed-on-side record so the adapter
        # decides; the adapter rejects sides not in {'long','short'}
        # so this will be UNKNOWN. Operator/IB can fix by closing the
        # zombie line.
        side = None

    raw = {
        "symbol":             sym,
        "side":               side,
        "qty":                abs(float(qty)) if isinstance(qty, (int, float))
                                                and not isinstance(qty, bool) else qty,
        "currency":           getattr(contract, "currency", None) if contract else None,
        "exposure_usd":       getattr(item, "marketValue", None),
        "mark_price":         getattr(item, "marketPrice", None),
        "avg_price":          getattr(item, "averageCost", None),
        "unrealised_pnl_usd": getattr(item, "unrealizedPNL", None),
    }
    # If marketValue is provided AND currency is USD, we treat it as
    # broker-provided USD notional, the highest-trust source. The
    # adapter recognises 'broker_provided_usd_notional' as the
    # mark_source label, so we plumb it through too.
    if (raw["currency"] and isinstance(raw["currency"], str)
            and raw["currency"].upper() == "USD"
            and isinstance(raw["exposure_usd"], (int, float))
            and not isinstance(raw["exposure_usd"], bool)):
        raw["broker_provided_usd_notional"] = float(raw["exposure_usd"])
    return raw


def _wait_for_snapshot_ready(ib: Any, timeout: float) -> Dict[str, Any]:
    """Wait for the account-update subscription to deliver its first
    batch before treating any portfolio()/positions() read as
    authoritative. Returns a dict with `ready` (bool),
    `account_values_count` (int), `waited_sec` (float).

    Mechanism: `ib.accountValues()` is empty until IB delivers the
    first account-update event after connect. We poll it via
    `ib.waitOnUpdate(timeout=<poll>)` repeatedly until either it's
    non-empty or `timeout` elapses. This bounds wall-clock time
    without spawning threads. Read-only.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    waited = 0.0
    last_count = 0
    while True:
        try:
            vals = ib.accountValues() or []
        except Exception:
            vals = []
        last_count = len(vals)
        if last_count > 0:
            return {
                "ready":                 True,
                "account_values_count":  last_count,
                "waited_sec":            round(time.monotonic()
                                                - (deadline - timeout), 3),
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "ready":                 False,
                "account_values_count":  last_count,
                "waited_sec":            round(time.monotonic()
                                                - (deadline - timeout), 3),
            }
        # Pump the event loop briefly so account-update messages can be
        # processed. waitOnUpdate(timeout=...) returns whether an event
        # arrived; either way we re-check accountValues() length.
        poll = min(_SNAPSHOT_POLL_SEC, remaining)
        try:
            ib.waitOnUpdate(timeout=poll)
        except Exception:
            # If waitOnUpdate misbehaves, fall back to a plain sleep
            # so we don't spin tightly.
            time.sleep(poll)
        waited += poll


def _symbols_with_position(items: List[Any]) -> Set[str]:
    """Extract the set of symbols where position != 0 from either an
    ib_insync portfolio() result (PortfolioItem with .position attr
    and .contract.symbol) or a positions() result (Position with same
    attrs). Defensively skips items with missing/None fields."""
    out: Set[str] = set()
    for it in items or ():
        try:
            contract = getattr(it, "contract", None)
            sym = getattr(contract, "symbol", None) if contract else None
            qty = getattr(it, "position", None)
            if (isinstance(sym, str) and sym
                    and isinstance(qty, (int, float))
                    and not isinstance(qty, bool)
                    and qty != 0):
                out.add(sym)
        except Exception:
            # Defensive — a malformed item never causes a symbol set
            # to grow, so cross-confirm errs on the side of mismatch.
            continue
    return out


def _read_portfolio_via_ib(
    *,
    host: str,
    port: int,
    client_id: int,
    connect_timeout: float,
    api_timeout: float,
) -> Dict[str, Any]:
    """The single function in this codebase that talks to IB API for
    M15.5 ingestion. Read-only contract enforced by:
      * `readonly=True` keyword on connect (test asserts presence)
      * AST scan rejects placeOrder/cancelOrder/etc. anywhere here
      * `finally` block always disconnects

    Returns a dict so the caller can perform cross-confirm checks:
      {
        "portfolio_items":       list of ib_insync.PortfolioItem,
        "position_records":      list of ib_insync.Position,
        "snapshot_ready":        bool,
        "account_values_count":  int,
        "snapshot_waited_sec":   float,
      }
    """
    # Lazy import so the scanner-isolation subprocess check passes
    # without ib_insync being loaded. Imported NAMES limited to
    # IB only — order classes are explicitly NOT imported.
    from ib_insync import IB   # noqa: F401 — narrow import surface
    ib = IB()
    try:
        ib.connect(
            host, port,
            clientId=client_id,
            readonly=True,
            timeout=connect_timeout,
        )
        # api_timeout governs the snapshot-ready wait below.
        snap = _wait_for_snapshot_ready(ib, timeout=api_timeout)
        portfolio_items: List[Any] = []
        position_records: List[Any] = []
        if snap["ready"]:
            # Both reads are synchronous cache reads from the
            # account-update subscription. They do NOT accept a
            # timeout; the api_timeout above already bounded the
            # wait for the cache to be populated.
            portfolio_items = list(ib.portfolio())
            position_records = list(ib.positions())
        return {
            "portfolio_items":       portfolio_items,
            "position_records":      position_records,
            "snapshot_ready":        snap["ready"],
            "account_values_count":  snap["account_values_count"],
            "snapshot_waited_sec":   snap["waited_sec"],
        }
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:  # pragma: no cover — disconnect best-effort
            log.warning("ib.disconnect() raised; suppressing")


def make_ibkr_paper_positions_reader(
    *,
    health_checker: Optional[Callable] = None,
    ib_session_factory: Optional[Callable] = None,
    client_id: int = M15_5_CLIENT_ID,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SEC,
    api_timeout: float = DEFAULT_API_TIMEOUT_SEC,
) -> Callable[[], List[Dict[str, Any]]]:
    """Build the `positions_reader` callable the existing
    IBKRExposureAdapter expects. Returns a no-arg function.

    health_checker     — defaults to bot.gateway_health.assemble_health
    ib_session_factory — defaults to _read_portfolio_via_ib (real IB call).
                         Tests inject a mock that never touches the network.

    Cross-confirm policy (added in the post-138df9e patch):
      * snapshot_ready=False         → IBPaperReadError(snapshot_not_ready)
      * portfolio_syms != positions_syms → IBPaperReadError(disagreement)
      * snapshot_ready AND both empty AND symbol sets agree (both ∅)
                                       → return [] (adapter classifies
                                          as known-zero)
      * snapshot_ready AND both non-empty AND symbols agree
                                       → return per-item dicts (adapter
                                          classifies as fresh/partial)
    """
    factory = ib_session_factory or _read_portfolio_via_ib

    def _reader() -> List[Dict[str, Any]]:
        _check_gateway_ready(scope="ibkr_paper", health_checker=health_checker)
        try:
            result = factory(
                host=IBKR_PAPER_HOST,
                port=IBKR_PAPER_PORT,
                client_id=client_id,
                connect_timeout=connect_timeout,
                api_timeout=api_timeout,
            )
        except (GatewayNotReadyError, IBPaperReadError, NotImplementedError):
            raise
        except Exception as e:
            raise IBPaperReadError(
                f"ib_portfolio_read_failed:{type(e).__name__}:{e}"
            ) from e

        if not isinstance(result, dict):
            raise IBPaperReadError(
                f"ib_factory_returned_non_dict:type={type(result).__name__}"
            )

        if not result.get("snapshot_ready"):
            raise IBPaperReadError(
                f"account_snapshot_not_ready_within_timeout:"
                f"account_values_count={result.get('account_values_count', 0)},"
                f"waited_sec={result.get('snapshot_waited_sec', 'n/a')}"
            )

        portfolio_items  = result.get("portfolio_items")  or []
        position_records = result.get("position_records") or []

        # Cross-confirm symbol sets. portfolio() and positions() must
        # agree on which symbols have non-zero size. Disagreement means
        # the IB cache is mid-update or one source is unreliable;
        # either way we MUST NOT report known-zero or partial. Adapter
        # converts to EXPOSURE_UNKNOWN.
        portfolio_syms = _symbols_with_position(portfolio_items)
        positions_syms = _symbols_with_position(position_records)
        if portfolio_syms != positions_syms:
            raise IBPaperReadError(
                f"portfolio_positions_disagreement:"
                f"portfolio={sorted(portfolio_syms)},"
                f"positions={sorted(positions_syms)}"
            )

        return [_position_dict_from_portfolio_item(it)
                for it in portfolio_items]

    return _reader


def run_paper_dryrun(
    *,
    health_checker: Optional[Callable] = None,
    ib_session_factory: Optional[Callable] = None,
    client_id: int = M15_5_CLIENT_ID,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SEC,
    api_timeout: float = DEFAULT_API_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """M15.5 dry-run: prove the gateway is healthy, prove the IB
    connection works read-only, prove the snapshot becomes ready,
    prove portfolio() AND positions() can be read AND cross-confirm
    agree. NO DB writes. The operator runs this before the real ingest.

    Returns a structured summary:
      * gateway_ready
      * mode
      * expected_port
      * ib_connect_ok
      * snapshot_ready
      * account_values_count
      * positions_read_ok
      * portfolio_count, positions_count
      * cross_confirm_ok       (True iff symbol sets agree)
      * forbidden_calls_detected
      * error                  (None on success)

    No DB write, no order, no schema change.
    """
    summary: Dict[str, Any] = {
        "dry_run":                    True,
        "gateway_ready":              False,
        "mode":                       None,
        "expected_port":              None,
        "ib_connect_ok":              False,
        "snapshot_ready":             False,
        "account_values_count":       None,
        "positions_read_ok":          False,
        "portfolio_count":            None,
        "positions_count":            None,
        "cross_confirm_ok":           False,
        "forbidden_calls_detected":   [],
        "error":                      None,
    }
    # Gate 1: gateway health
    try:
        _check_gateway_ready(scope="ibkr_paper", health_checker=health_checker)
        summary["gateway_ready"] = True
    except (GatewayNotReadyError, NotImplementedError) as e:
        summary["error"] = f"{type(e).__name__}: {e}"
        return summary

    # Reflect the latest health snapshot for transparency.
    try:
        h = (health_checker or _default_health_checker)()
        if isinstance(h, dict):
            summary["mode"] = h.get("mode")
            summary["expected_port"] = h.get("expected_port")
    except Exception:  # pragma: no cover — health already passed gate 1
        pass

    # Gate 2: read-only IB session + snapshot wait + portfolio + positions.
    factory = ib_session_factory or _read_portfolio_via_ib
    try:
        result = factory(
            host=IBKR_PAPER_HOST,
            port=IBKR_PAPER_PORT,
            client_id=client_id,
            connect_timeout=connect_timeout,
            api_timeout=api_timeout,
        )
        summary["ib_connect_ok"] = True
        if not isinstance(result, dict):
            summary["error"] = (
                f"ib_factory_returned_non_dict:"
                f"type={type(result).__name__}"
            )
            return summary
        summary["snapshot_ready"]       = bool(result.get("snapshot_ready"))
        summary["account_values_count"] = result.get("account_values_count")
        if not summary["snapshot_ready"]:
            summary["error"] = (
                "account_snapshot_not_ready_within_timeout: "
                f"account_values_count={summary['account_values_count']}"
            )
            return summary
        portfolio_items  = result.get("portfolio_items")  or []
        position_records = result.get("position_records") or []
        summary["positions_read_ok"] = True
        summary["portfolio_count"]   = len(portfolio_items)
        summary["positions_count"]   = len(position_records)
        psyms = _symbols_with_position(portfolio_items)
        qsyms = _symbols_with_position(position_records)
        summary["cross_confirm_ok"] = (psyms == qsyms)
        if not summary["cross_confirm_ok"]:
            summary["error"] = (
                f"portfolio_positions_disagreement:"
                f"portfolio={sorted(psyms)},positions={sorted(qsyms)}"
            )
    except Exception as e:
        summary["error"] = f"{type(e).__name__}: {e}"
    return summary


__all__ = [
    "M15_5_CLIENT_ID",
    "IBKR_PAPER_HOST", "IBKR_PAPER_PORT",
    "DEFAULT_CONNECT_TIMEOUT_SEC", "DEFAULT_API_TIMEOUT_SEC",
    "GatewayNotReadyError", "IBPaperReadError",
    "make_ibkr_paper_positions_reader",
    "run_paper_dryrun",
    "_wait_for_snapshot_ready", "_symbols_with_position",
]
