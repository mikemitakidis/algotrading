"""
bot/etoro/response_parser.py — M13.5.B eToro response parsing.

Schema references are the OpenAPI specs documented in M13.5.A §2.4
and §2.6. This module performs pure parsing — no I/O, no network, no
DB. It is the single source of truth for translating eToro response
JSON to internal lifecycle state.

Status code vocabulary (per OpenAPI):
  0 = Pending
  1 = Executed
  2 = Cancelled
  3 = Rejected
  4 = Partially Executed

Internal mapping (M13.5.A §2.7):
  0 / 4 → 'submitted'      (continue polling)
  1     → 'filled'
  2     → 'cancelled'
  3     → 'broker_rejected'
  other → raise ParserError (defensive — abort, do not guess)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


class ParserError(ValueError):
    """The eToro response could not be parsed safely."""


# Status code mapping documented by eToro OpenAPI.
ETORO_STATUS_ID_TO_INTERNAL = {
    0: "submitted",
    1: "filled",
    2: "cancelled",
    3: "broker_rejected",
    4: "submitted",   # partial — keep polling
}

KNOWN_STATUS_IDS = set(ETORO_STATUS_ID_TO_INTERNAL.keys())


@dataclass
class OpenOrderResponse:
    """Parsed shape of the synchronous POST response on success."""
    order_id: int
    status_id: int
    instrument_id: int
    amount: float
    is_buy: bool
    leverage: int
    open_dt: str
    last_update: str
    tracking_token: str
    raw: dict = field(default_factory=dict)

    @property
    def internal_status(self) -> str:
        return _safe_status(self.status_id)


@dataclass
class OrderInfoResponse:
    """Parsed shape of GET /trading/info/real/orders/{orderId} response."""
    order_id: int
    status_id: int
    error_code: Optional[int]
    error_message: Optional[str]
    positions: list
    amount: float
    units: float
    instrument_id: int
    request_occurred: str
    raw: dict = field(default_factory=dict)

    @property
    def internal_status(self) -> str:
        # Error fields trump status — if eToro reports an error_code we
        # treat the order as broker_rejected even when status_id says 0.
        if self.error_code is not None:
            return "broker_rejected"
        return _safe_status(self.status_id)

    @property
    def first_position_id(self) -> Optional[int]:
        if not self.positions:
            return None
        first = self.positions[0]
        pid = first.get("positionID")
        return int(pid) if pid is not None else None

    @property
    def first_position_rate(self) -> Optional[float]:
        if not self.positions:
            return None
        r = self.positions[0].get("rate")
        return float(r) if r is not None else None

    @property
    def first_position_units(self) -> Optional[float]:
        if not self.positions:
            return None
        u = self.positions[0].get("units")
        return float(u) if u is not None else None

    @property
    def first_position_conversion_rate(self) -> Optional[float]:
        if not self.positions:
            return None
        cr = self.positions[0].get("conversionRate")
        return float(cr) if cr is not None else None

    @property
    def has_positions(self) -> bool:
        return bool(self.positions)


def _safe_status(status_id: Any) -> str:
    if not isinstance(status_id, int) or isinstance(status_id, bool):
        raise ParserError(f"statusID must be int, got {type(status_id).__name__}")
    if status_id not in KNOWN_STATUS_IDS:
        raise ParserError(f"unknown statusID={status_id!r}; expected one of "
                          f"{sorted(KNOWN_STATUS_IDS)}")
    return ETORO_STATUS_ID_TO_INTERNAL[status_id]


def parse_open_response(body: Any) -> OpenOrderResponse:
    """Parse the JSON body of a successful open-by-amount POST."""
    if not isinstance(body, dict):
        raise ParserError("response body must be a JSON object")
    ofo = body.get("orderForOpen")
    if not isinstance(ofo, dict):
        raise ParserError("missing 'orderForOpen' object")

    try:
        order_id = int(ofo["orderID"])
    except (KeyError, TypeError, ValueError):
        raise ParserError("missing or invalid 'orderForOpen.orderID'")
    try:
        status_id = int(ofo["statusID"])
    except (KeyError, TypeError, ValueError):
        raise ParserError("missing or invalid 'orderForOpen.statusID'")
    # Validate vocabulary before constructing.
    if status_id not in KNOWN_STATUS_IDS:
        raise ParserError(f"unknown statusID={status_id} in open response")

    return OpenOrderResponse(
        order_id=order_id,
        status_id=status_id,
        instrument_id=int(ofo.get("instrumentID", 0)),
        amount=float(ofo.get("amount", 0.0)),
        is_buy=bool(ofo.get("isBuy", False)),
        leverage=int(ofo.get("leverage", 0)),
        open_dt=str(ofo.get("openDateTime", "")),
        last_update=str(ofo.get("lastUpdate", "")),
        tracking_token=str(body.get("token", "")),
        raw=body,
    )


def parse_order_info(body: Any) -> OrderInfoResponse:
    """Parse the JSON body of GET /trading/info/real/orders/{orderId}."""
    if not isinstance(body, dict):
        raise ParserError("response body must be a JSON object")
    try:
        order_id = int(body["orderID"])
    except (KeyError, TypeError, ValueError):
        raise ParserError("missing or invalid 'orderID'")
    try:
        status_id = int(body["statusID"])
    except (KeyError, TypeError, ValueError):
        raise ParserError("missing or invalid 'statusID'")
    if status_id not in KNOWN_STATUS_IDS:
        raise ParserError(f"unknown statusID={status_id} in order-info response")

    positions_raw = body.get("positions") or []
    if not isinstance(positions_raw, list):
        raise ParserError("'positions' must be a list when present")

    err_code = body.get("errorCode")
    if err_code is not None and not isinstance(err_code, int):
        # Tolerate float / string error codes by coercing; raise if impossible.
        try:
            err_code = int(err_code)
        except (TypeError, ValueError):
            raise ParserError(f"unparseable errorCode={err_code!r}")
    err_msg = body.get("errorMessage")
    if err_msg is not None and not isinstance(err_msg, str):
        err_msg = str(err_msg)

    return OrderInfoResponse(
        order_id=order_id,
        status_id=status_id,
        error_code=err_code,
        error_message=err_msg,
        positions=positions_raw,
        amount=float(body.get("amount", 0.0)),
        units=float(body.get("units", 0.0)),
        instrument_id=int(body.get("instrumentID", 0)),
        request_occurred=str(body.get("requestOccurred", "")),
        raw=body,
    )


def parse_error(http_status: int, body: Any) -> dict:
    """Parse an eToro error response into a structured dict.

    eToro does not document a single error body shape (M13.5.A §8.2).
    This parser is defensive: it accepts dict, str, or empty body.
    """
    out = {"http_status": int(http_status)}
    if isinstance(body, dict):
        # Common shapes seen across eToro endpoints.
        for k in ("errorCode", "errorMessage", "message", "error"):
            if k in body:
                out[k] = body[k]
        # Sometimes the error is nested under 'error'.
        inner = body.get("error")
        if isinstance(inner, dict):
            for k in ("code", "message", "type"):
                if k in inner:
                    out[f"error_{k}"] = inner[k]
    elif isinstance(body, str):
        out["raw_text"] = body[:500]   # cap
    return out


__all__ = [
    "ParserError",
    "ETORO_STATUS_ID_TO_INTERNAL",
    "KNOWN_STATUS_IDS",
    "OpenOrderResponse",
    "OrderInfoResponse",
    "parse_open_response",
    "parse_order_info",
    "parse_error",
]
