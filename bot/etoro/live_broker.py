"""
bot/etoro/live_broker.py — M13.5.B EtoroLiveBroker.

The real-money eToro adapter. Hard contract:

  * NOT importable into the scanner / strategy / main path.
  * NOT constructible from bot.brokers.__init__.get_broker(). The
    only constructor of this class is tools/etoro_live_write.py.
  * .submit() — the BrokerAdapter interface — is intentionally NOT
    a live entry point. It refuses to submit unless a per-payload
    nonce is presented via submit_live().
  * preflight() runs the 16 gates documented in M13.4B §7 and
    M13.5.A §3, in order. First failure aborts.
  * Validates loaded policy via validate_policy() before consulting
    any field (ChatGPT audit finding).
  * Reads ETORO_LIVE_ENABLED env flag in addition to policy flag
    (double live flag, M13.5.A §3).
  * Generates fresh UUIDv4 for x-request-id; never reused.
  * POST runs against a transport callable; production transport
    issues exactly one POST and never retries.
  * No exponential backoff. No automatic retry on 429 or 5xx.
  * Audit log written before and after the POST; secrets redacted.

This module imports bot.etoro.audit, bot.etoro.response_parser,
bot.etoro.order_poller, bot.etoro.nonce. It does NOT import the
read adapter, paper broker, or anything from the scanner path.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

from bot.brokers.base import BrokerAdapter, OrderIntent, OrderResult
from bot.broker_allocation import validate_policy
from .audit import AuditLogger, redact_body, redact_headers, redact_payload
from .errors import (
    EtoroAuthError,
    EtoroError,
    EtoroRateLimitError,
    EtoroRouteError,
    EtoroTransientError,
    EtoroValidationError,
)
from .nonce import NonceStore
from .response_parser import (
    OpenOrderResponse,
    parse_error,
    parse_open_response,
)

log = logging.getLogger(__name__)


# Default eToro real-money endpoint base.
DEFAULT_BASE_URL = "https://public-api.etoro.com"
OPEN_BY_AMOUNT_PATH = "/api/v1/trading/execution/market-open-orders/by-amount"
ORDER_INFO_PATH_TPL = "/api/v1/trading/info/real/orders/{order_id}"


# --- Exception hierarchy specific to the live broker preflight -------------

class PreflightError(EtoroError):
    """Base for all preflight failures. Carries a stable `reason_code`."""

    def __init__(self, reason_code: str, message: str = ""):
        super().__init__(message or reason_code)
        self.reason_code = reason_code


class OperatorConfirmationRequired(EtoroError):
    """submit() without a confirmed live nonce was attempted.

    Defends the scanner-isolation invariant: if any non-operator code
    path ever calls EtoroLiveBroker.submit(), this is raised.
    """


# Specific named preflight errors (one per gate).
class PolicyMissing(PreflightError):           pass
class PolicyInvalid(PreflightError):           pass
class GlobalKillSwitch(PreflightError):        pass
class GlobalDisabled(PreflightError):          pass
class BrokerKillSwitch(PreflightError):        pass
class BrokerDisabled(PreflightError):          pass
class BrokerNotAllowed(PreflightError):        pass
class EtoroLiveDisabled(PreflightError):       pass
class EtoroLiveDisabledEnv(PreflightError):    pass
class ExceedsSingleTrade(PreflightError):      pass
class ExceedsBrokerCapital(PreflightError):    pass
class ExceedsGlobalCapital(PreflightError):    pass
class ExceedsOpenPositions(PreflightError):    pass
class DailyLossUnknown(PreflightError):        pass
class DailyLossBreached(PreflightError):       pass
class MarketClosed(PreflightError):            pass
class StaleQuote(PreflightError):              pass
class SpreadTooWide(PreflightError):           pass
class AmountTooSmall(PreflightError):          pass


# --- Transport ------------------------------------------------------------

# Signature: (url, method, headers, body_bytes, timeout) -> (status, headers, body_bytes)
Transport = Callable[[str, str, Dict[str, str], Optional[bytes], float],
                     Tuple[int, Dict[str, str], bytes]]


def _real_post_transport(
    url: str, method: str, headers: Dict[str, str],
    body: Optional[bytes], timeout: float,
) -> Tuple[int, Dict[str, str], bytes]:
    """Production transport. Issues a single HTTP request.

    Used only by tools/etoro_live_write.py at run time. Tests inject
    a fake transport and never touch the network.
    """
    if method != "POST" and method != "GET":
        raise ValueError(f"unsupported method {method!r}")
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return resp.getcode(), hdrs, data
    except urllib.error.HTTPError as e:
        data = e.read() if hasattr(e, "read") else b""
        hdrs = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        return e.code, hdrs, data
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise EtoroTransientError(
            f"network error: {type(e).__name__}: {e}"
        ) from e


# --- Context object passed into preflight ---------------------------------

@dataclass
class LiveWriteContext:
    """All runtime inputs preflight() needs to make a decision.

    Centralising these in one object keeps preflight() pure and
    testable. The CLI populates this; the scanner does not.
    """
    policy: dict
    payload: dict                         # eToro request payload
    env_live_enabled: bool                # ETORO_LIVE_ENABLED .env flag
    open_positions_count: int             # from read adapter
    realised_daily_loss: Optional[float]  # None if unknown -> fail closed
    market_open: bool
    quote_age_sec: Optional[float]
    quote_max_age_sec: float
    spread_bps: Optional[float]
    spread_max_bps: float
    amount_min: float                     # API-confirmed minimum (M13.5.B demo)


@dataclass
class PreflightOk:
    """Marker that preflight passed; carries the policy snapshot the
    CLI should record in lifecycle_json."""
    policy_snapshot: dict = field(default_factory=dict)


# --- EtoroLiveBroker -------------------------------------------------------

class EtoroLiveBroker(BrokerAdapter):
    """Real-money eToro adapter. Constructed only by the operator CLI."""

    def __init__(
        self,
        *,
        api_key: str,
        user_key: str,
        env_live_enabled: bool,
        nonce_store: Optional[NonceStore] = None,
        audit: Optional[AuditLogger] = None,
        transport: Transport = _real_post_transport,
        base_url: str = DEFAULT_BASE_URL,
        timeout_sec: float = 15.0,
    ):
        if not api_key or not isinstance(api_key, str):
            raise ValueError("api_key is required")
        if not user_key or not isinstance(user_key, str):
            raise ValueError("user_key is required")
        if env_live_enabled is not True:
            # Double-flag: env must be True. Caller is the CLI; we still
            # enforce here so misuse fails loudly.
            raise EtoroLiveDisabledEnv(
                "etoro_live_disabled_env",
                "ETORO_LIVE_ENABLED env flag must be true to construct "
                "EtoroLiveBroker. The CLI must verify this before "
                "instantiation.",
            )
        self._api_key = api_key
        self._user_key = user_key
        self._env_live_enabled = True
        self._nonce_store = nonce_store
        self._audit = audit
        self._transport = transport
        self._base_url = base_url.rstrip("/")
        self._timeout = float(timeout_sec)

    # BrokerAdapter interface --------------------------------------------------

    @property
    def name(self) -> str:
        return "etoro_real"

    @property
    def is_live(self) -> bool:
        return True

    def submit(self, intent: OrderIntent) -> OrderResult:
        """Standard BrokerAdapter.submit() is NEVER a live entry point.

        It must not be reachable from the scanner. If any code path
        outside the operator CLI calls this method, raise
        OperatorConfirmationRequired so the failure is loud and
        visible in stack traces. The CLI uses submit_live() instead.
        """
        raise OperatorConfirmationRequired(
            "EtoroLiveBroker.submit() is not a live entry point. "
            "Operator CLI must call submit_live(intent, ctx, "
            "echoed_confirmation) and pass through preflight first."
        )

    # Preflight ----------------------------------------------------------------

    def preflight(self, ctx: LiveWriteContext) -> PreflightOk:
        """Run the 16 ordered safety gates. Raise PreflightError subclass
        on the first failure. Return PreflightOk on success.

        Order matches M13.4B §7 and M13.5.A §3.
        """
        # ChatGPT audit: validate loaded policy before any use.
        if not isinstance(ctx.policy, dict):
            raise PolicyMissing("policy_missing", "policy not loaded")
        val = validate_policy(ctx.policy)
        if not val.ok:
            raise PolicyInvalid(
                "policy_invalid",
                f"policy failed validation: {val.errors}",
            )
        policy = ctx.policy

        # 2-3. Global gates
        g = policy.get("global", {})
        if g.get("kill_switch") is True:
            raise GlobalKillSwitch("global_kill_switch", "global kill switch active")
        if g.get("auto_trading_enabled") is not True:
            raise GlobalDisabled("global_disabled", "global auto-trading disabled")

        # 4-5. Broker gates
        etoro_block = policy.get("etoro", {})
        if etoro_block.get("kill_switch") is True:
            raise BrokerKillSwitch("broker_kill_switch", "etoro kill switch active")
        if etoro_block.get("auto_trading_enabled") is not True:
            raise BrokerDisabled("broker_disabled", "etoro auto-trading disabled")

        # 6. allowed_brokers
        routing = policy.get("routing", {})
        allowed = routing.get("allowed_brokers", [])
        if "etoro_real" not in allowed:
            raise BrokerNotAllowed("broker_not_allowed",
                                   "etoro_real not in routing.allowed_brokers")

        # 7. policy etoro_live_enabled (strict identity check)
        if routing.get("etoro_live_enabled") is not True:
            raise EtoroLiveDisabled("etoro_live_disabled",
                                    "routing.etoro_live_enabled is not True")

        # 8. env ETORO_LIVE_ENABLED
        if ctx.env_live_enabled is not True:
            raise EtoroLiveDisabledEnv("etoro_live_disabled_env",
                                       "ETORO_LIVE_ENABLED env flag is not true")

        # Sizing gates ---------------------------------------------------------
        amount = ctx.payload.get("Amount")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool):
            raise ExceedsSingleTrade("amount_invalid",
                                     f"payload Amount must be a number, got {amount!r}")
        amount = float(amount)
        if amount <= 0:
            raise ExceedsSingleTrade("amount_invalid",
                                     "payload Amount must be > 0")
        if amount < ctx.amount_min:
            raise AmountTooSmall(
                "amount_too_small",
                f"Amount {amount} below API minimum {ctx.amount_min}",
            )

        single_cap = float(etoro_block.get("max_single_trade_amount", 0) or 0)
        if single_cap <= 0 or amount > single_cap:
            raise ExceedsSingleTrade(
                "exceeds_single_trade",
                f"Amount {amount} exceeds etoro.max_single_trade_amount {single_cap}",
            )

        broker_cap = float(etoro_block.get("max_auto_trading_capital", 0) or 0)
        if broker_cap <= 0 or amount > broker_cap:
            raise ExceedsBrokerCapital(
                "exceeds_broker_capital",
                f"Amount {amount} exceeds etoro.max_auto_trading_capital {broker_cap}",
            )

        global_cap = float(g.get("max_auto_trading_capital", 0) or 0)
        if global_cap > 0 and amount > global_cap:
            raise ExceedsGlobalCapital(
                "exceeds_global_capital",
                f"Amount {amount} exceeds global.max_auto_trading_capital {global_cap}",
            )

        # Open positions cap
        max_open = int(etoro_block.get("max_open_positions", 0) or 0)
        if max_open <= 0 or ctx.open_positions_count >= max_open:
            raise ExceedsOpenPositions(
                "exceeds_open_positions",
                f"open positions {ctx.open_positions_count} >= cap {max_open}",
            )

        # Daily loss
        max_daily_loss = float(etoro_block.get("max_daily_loss", 0) or 0)
        if max_daily_loss > 0:
            if ctx.realised_daily_loss is None:
                # Fail closed.
                raise DailyLossUnknown(
                    "daily_loss_unknown",
                    "realised_daily_loss not available; cannot verify cap",
                )
            if float(ctx.realised_daily_loss) >= max_daily_loss:
                raise DailyLossBreached(
                    "daily_loss_breached",
                    f"realised_daily_loss {ctx.realised_daily_loss} >= "
                    f"cap {max_daily_loss}",
                )

        # Market state
        if ctx.market_open is not True:
            raise MarketClosed("market_closed", "market is not open")
        if ctx.quote_age_sec is None or ctx.quote_age_sec > ctx.quote_max_age_sec:
            raise StaleQuote(
                "stale_quote",
                f"quote_age_sec={ctx.quote_age_sec} > max {ctx.quote_max_age_sec}",
            )
        if ctx.spread_bps is None or ctx.spread_bps > ctx.spread_max_bps:
            raise SpreadTooWide(
                "spread_too_wide",
                f"spread_bps={ctx.spread_bps} > max {ctx.spread_max_bps}",
            )

        return PreflightOk(policy_snapshot=_snapshot_policy(policy))

    # The actual live POST ----------------------------------------------------

    def submit_live(
        self,
        payload: dict,
        ctx: LiveWriteContext,
        echoed_confirmation: str,
    ) -> Tuple[OpenOrderResponse, Dict[str, Any]]:
        """Operator-only live entry point. The CLI is the only caller.

        Steps:
          1. preflight(ctx) — raises on any gate failure.
          2. nonce.validate(echoed, payload) — raises on mismatch/expiry.
          3. Build headers, fresh x-request-id.
          4. Single POST via the injected transport.
          5. Parse response. On HTTP 200 with a valid orderForOpen,
             return (OpenOrderResponse, audit_record). On any error,
             raise the appropriate EtoroError subclass.

        Returns:
          (parsed_response, audit_record)
          audit_record is a redacted dict suitable for storing in
          lifecycle_json (no secrets, no full account IDs).
        """
        # Gate chain.
        self.preflight(ctx)

        # Nonce.
        if self._nonce_store is None:
            raise OperatorConfirmationRequired(
                "no NonceStore configured; refusing to submit_live without "
                "a per-payload nonce check"
            )
        ok, reason = self._nonce_store.validate(echoed_confirmation, payload)
        if not ok:
            raise OperatorConfirmationRequired(
                f"confirmation rejected: {reason}"
            )

        # Build request.
        x_request_id = str(uuid.uuid4())
        url = self._base_url + OPEN_BY_AMOUNT_PATH
        headers = {
            "Content-Type": "application/json",
            "Accept":       "application/json",
            "x-request-id": x_request_id,
            "x-api-key":    self._api_key,
            "x-user-key":   self._user_key,
        }
        body_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")

        # Audit: pre-POST.
        if self._audit:
            self._audit.event(
                "live_post_attempt",
                url=url,
                payload=redact_payload(payload),
                headers=redact_headers(headers),
                x_request_id=x_request_id,
            )

        # Single POST. No retry.
        status, resp_headers, resp_body = self._transport(
            url, "POST", headers, body_bytes, self._timeout,
        )

        # Parse body to JSON if possible.
        try:
            parsed_body: Any = json.loads(resp_body.decode("utf-8")) if resp_body else None
        except (ValueError, UnicodeDecodeError):
            parsed_body = None

        audit_record: Dict[str, Any] = {
            "x_request_id": x_request_id,
            "http_status":  status,
            "response":     redact_body(parsed_body) if parsed_body is not None else None,
        }

        if status == 401 or status == 403:
            err = parse_error(status, parsed_body)
            if self._audit:
                self._audit.event("live_post_auth_error", **err)
            raise EtoroAuthError(f"auth error {status}: {err}")
        if status == 404:
            err = parse_error(status, parsed_body)
            if self._audit:
                self._audit.event("live_post_route_error", **err)
            raise EtoroRouteError(f"route error {status}: {err}")
        if status == 429:
            err = parse_error(status, parsed_body)
            retry_after = 0.0
            try:
                retry_after = float(resp_headers.get("retry-after", "0") or 0)
            except (TypeError, ValueError):
                retry_after = 0.0
            if self._audit:
                self._audit.event("live_post_rate_limited", retry_after=retry_after,
                                  **err)
            raise EtoroRateLimitError(f"rate limited: {err}",
                                      retry_after=retry_after)
        if 400 <= status < 500:
            err = parse_error(status, parsed_body)
            if self._audit:
                self._audit.event("live_post_validation_error", **err)
            raise EtoroValidationError(f"validation error {status}: {err}")
        if status >= 500:
            err = parse_error(status, parsed_body)
            if self._audit:
                self._audit.event("live_post_transient_error", **err)
            raise EtoroTransientError(f"transient error {status}: {err}")

        # 200 OK — parse and return.
        if not isinstance(parsed_body, dict):
            err = parse_error(status, parsed_body)
            if self._audit:
                self._audit.event("live_post_unexpected_body", **err)
            raise EtoroValidationError(
                f"unexpected response body type: {type(parsed_body).__name__}",
            )
        parsed_resp = parse_open_response(parsed_body)
        if self._audit:
            self._audit.event(
                "live_post_accepted",
                order_id=parsed_resp.order_id,
                status_id=parsed_resp.status_id,
                instrument_id=parsed_resp.instrument_id,
                amount=parsed_resp.amount,
                is_buy=parsed_resp.is_buy,
                leverage=parsed_resp.leverage,
                x_request_id=x_request_id,
            )
        return parsed_resp, audit_record

    # Read-only helper used by the poller.
    def fetch_order_info(self, order_id: int) -> dict:
        """Issue GET /trading/info/real/orders/{order_id}. Single call,
        no retry. Returns the parsed JSON body (raw dict)."""
        if not isinstance(order_id, int) or order_id <= 0:
            raise ValueError(f"order_id must be positive int, got {order_id!r}")
        url = self._base_url + ORDER_INFO_PATH_TPL.format(order_id=order_id)
        headers = {
            "Accept":       "application/json",
            "x-request-id": str(uuid.uuid4()),
            "x-api-key":    self._api_key,
            "x-user-key":   self._user_key,
        }
        status, _, body = self._transport(url, "GET", headers, None, self._timeout)
        if status == 401 or status == 403:
            raise EtoroAuthError(f"auth error {status}")
        if status == 404:
            raise EtoroRouteError(f"route error {status}")
        if status == 429:
            raise EtoroRateLimitError(f"rate limited {status}")
        if status >= 400:
            raise EtoroValidationError(f"http {status}")
        if not body:
            raise EtoroValidationError("empty body")
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise EtoroValidationError(f"bad json: {e}") from e


# --- Helpers ---------------------------------------------------------------

def _snapshot_policy(policy: dict) -> dict:
    """Return a slim snapshot of policy values relevant to a live write.

    Does not include credentials. Suitable for lifecycle_json storage.
    """
    out: dict = {}
    g = policy.get("global", {})
    if isinstance(g, dict):
        out["global"] = {
            "auto_trading_enabled":   g.get("auto_trading_enabled"),
            "kill_switch":            g.get("kill_switch"),
            "max_auto_trading_capital": g.get("max_auto_trading_capital"),
        }
    e = policy.get("etoro", {})
    if isinstance(e, dict):
        out["etoro"] = {
            "auto_trading_enabled":   e.get("auto_trading_enabled"),
            "kill_switch":            e.get("kill_switch"),
            "max_auto_trading_capital": e.get("max_auto_trading_capital"),
            "max_single_trade_amount": e.get("max_single_trade_amount"),
            "max_daily_loss":         e.get("max_daily_loss"),
            "max_open_positions":     e.get("max_open_positions"),
        }
    r = policy.get("routing", {})
    if isinstance(r, dict):
        out["routing"] = {
            "default_broker":      r.get("default_broker"),
            "etoro_live_enabled":  r.get("etoro_live_enabled"),
            "allowed_brokers":     list(r.get("allowed_brokers", [])),
        }
    return out


__all__ = [
    "DEFAULT_BASE_URL",
    "OPEN_BY_AMOUNT_PATH",
    "ORDER_INFO_PATH_TPL",
    "PreflightError",
    "OperatorConfirmationRequired",
    "PolicyMissing",
    "PolicyInvalid",
    "GlobalKillSwitch",
    "GlobalDisabled",
    "BrokerKillSwitch",
    "BrokerDisabled",
    "BrokerNotAllowed",
    "EtoroLiveDisabled",
    "EtoroLiveDisabledEnv",
    "ExceedsSingleTrade",
    "ExceedsBrokerCapital",
    "ExceedsGlobalCapital",
    "ExceedsOpenPositions",
    "DailyLossUnknown",
    "DailyLossBreached",
    "MarketClosed",
    "StaleQuote",
    "SpreadTooWide",
    "AmountTooSmall",
    "LiveWriteContext",
    "PreflightOk",
    "EtoroLiveBroker",
]
