"""
bot/broker_allocation.py — Milestone 13.4A.

Broker Allocation + Budget Controls policy.

Scope (M13.4A):
- Define DEFAULT_POLICY shape.
- Server-side validation (validate_policy).
- Persistence helpers using the existing portfolio_risk_state KV table
  (no DB schema change).
- Read helpers (is_broker_allowed, is_auto_trading_allowed) for future
  M13.5 live writer to consult.

Out of scope (M13.4A):
- No live trading.
- No runtime wiring into main.py.
- No eToro write paths.
- etoro_real remains blocked.
- etoro_live_enabled=true is rejected at validation.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# Persistence key in portfolio_risk_state(key, value, updated_at)
POLICY_KEY = "broker_allocation_policy"

# Brokers that may ever appear in allowed_brokers. M13.5.B extends the
# whitelist to include etoro_real, since the live writer ships in this
# milestone. Runtime gating still requires BOTH policy.etoro_live_enabled
# AND .env ETORO_LIVE_ENABLED to be True before any real POST.
ALLOWED_BROKER_WHITELIST = {"paper", "ibkr_paper", "ibkr_live",
                            "etoro_paper", "etoro_real"}
# M13.5.B: no broker is hard-blocked at policy validation. Runtime
# gates (preflight + .env + nonce) decide whether a real POST happens.
FORBIDDEN_BROKERS: set = set()

POLICY_VERSION = 1

# Allowed top-level keys (unknown keys are rejected)
_TOP_LEVEL_KEYS = {"version", "global", "ibkr", "ibkr_paper", "ibkr_live",
                   "etoro", "routing"}
_GLOBAL_KEYS = {"auto_trading_enabled", "auto_trading_enabled_until_utc",
                "max_auto_trading_capital", "kill_switch"}
# Lean per-lane authorization blocks (M21.1extra-D0). These carry ONLY the
# authorization fields — capital/limits stay in the legacy `ibkr`/`etoro`
# blocks. The reader resolves ibkr_paper/ibkr_live to these, NOT to `ibkr`, so
# paper and live are authorized independently and the legacy shared
# `ibkr.auto_trading_enabled` can never authorize a lane.
_LANE_KEYS = {
    "auto_trading_enabled",
    "auto_trading_enabled_until_utc",
    "kill_switch",
}
_BROKER_KEYS = {
    "auto_trading_enabled",
    "max_auto_trading_capital",
    "max_single_trade_amount",
    "max_daily_loss",
    "max_open_positions",
    "kill_switch",
}
_ROUTING_KEYS = {
    "default_broker",
    "route_overrides",
    "allowed_brokers",
    "etoro_live_enabled",
}


DEFAULT_POLICY: dict = {
    "version": POLICY_VERSION,
    "global": {
        "auto_trading_enabled": False,
        "auto_trading_enabled_until_utc": None,
        "max_auto_trading_capital": 0.0,
        "kill_switch": False,
    },
    # Lean per-lane authorization blocks (D0). Disabled with no expiry by
    # default — fail-closed. The D0 CLI can enable ibkr_paper only.
    "ibkr_paper": {
        "auto_trading_enabled": False,
        "auto_trading_enabled_until_utc": None,
        "kill_switch": False,
    },
    "ibkr_live": {
        "auto_trading_enabled": False,
        "auto_trading_enabled_until_utc": None,
        "kill_switch": False,
    },
    "ibkr": {
        "auto_trading_enabled": False,
        "max_auto_trading_capital": 0.0,
        "max_single_trade_amount": 0.0,
        "max_daily_loss": 0.0,
        "max_open_positions": 0,
        "kill_switch": False,
    },
    "etoro": {
        "auto_trading_enabled": False,
        "max_auto_trading_capital": 0.0,
        "max_single_trade_amount": 0.0,
        "max_daily_loss": 0.0,
        "max_open_positions": 0,
        "kill_switch": False,
    },
    "routing": {
        "default_broker": "paper",
        "route_overrides": {
            "IBKR": "ibkr_live",
            "ETORO": "etoro_paper",
        },
        "allowed_brokers": ["paper", "ibkr_paper", "ibkr_live", "etoro_paper"],
        "etoro_live_enabled": False,
    },
}


@dataclass
class ValidationResult:
    """Outcome of validate_policy(). ok=True iff errors is empty."""
    ok: bool
    errors: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "errors": list(self.errors)}


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────

def _err(errors: list, path: str, code: str, msg: str) -> None:
    errors.append({"path": path, "code": code, "msg": msg})


def _is_bool(v: Any) -> bool:
    # Strict: real bool only. 1/0/"true"/"false" rejected.
    return isinstance(v, bool)


def _is_non_negative_number(v: Any) -> bool:
    # Reject bool (bool is subclass of int in python).
    if isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    try:
        return float(v) >= 0.0
    except (TypeError, ValueError):
        return False


def _is_non_negative_int(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if not isinstance(v, int):
        return False
    return v >= 0


def _is_valid_until_utc(v: Any) -> bool:
    """auto_trading_enabled_until_utc may be None (no authorization window) or a
    parseable ISO-8601 string. Validation only checks SHAPE; the reader is what
    enforces unexpired-ness at read time. A non-None, non-string, or unparseable
    value is invalid."""
    if v is None:
        return True
    if not isinstance(v, str):
        return False
    try:
        datetime.fromisoformat(v)
        return True
    except ValueError:
        return False


def _validate_lane_block(name: str, block: Any, errors: list) -> None:
    """Validate a lean per-lane authorization block (ibkr_paper / ibkr_live)."""
    if not isinstance(block, dict):
        _err(errors, name, "type_error", f"{name} must be an object")
        return
    extra = set(block.keys()) - _LANE_KEYS
    if extra:
        _err(errors, name, "unknown_key",
             f"unknown keys in {name}: {sorted(extra)}")
    missing = _LANE_KEYS - set(block.keys())
    if missing:
        _err(errors, name, "missing_key",
             f"missing keys in {name}: {sorted(missing)}")
        return
    for k in ("auto_trading_enabled", "kill_switch"):
        if not _is_bool(block[k]):
            _err(errors, f"{name}.{k}", "type_error",
                 f"{k} must be a boolean")
    if not _is_valid_until_utc(block["auto_trading_enabled_until_utc"]):
        _err(errors, f"{name}.auto_trading_enabled_until_utc", "value_error",
             "auto_trading_enabled_until_utc must be null or an ISO-8601 string")


def _validate_broker_block(name: str, block: Any, errors: list) -> None:
    path = name
    if not isinstance(block, dict):
        _err(errors, path, "type_error", f"{name} must be an object")
        return

    extra = set(block.keys()) - _BROKER_KEYS
    if extra:
        _err(errors, path, "unknown_key",
             f"unknown keys in {name}: {sorted(extra)}")

    missing = _BROKER_KEYS - set(block.keys())
    if missing:
        _err(errors, path, "missing_key",
             f"missing keys in {name}: {sorted(missing)}")
        return  # cannot validate values without keys

    for k in ("auto_trading_enabled", "kill_switch"):
        if not _is_bool(block[k]):
            _err(errors, f"{path}.{k}", "type_error",
                 f"{k} must be a boolean")

    for k in ("max_auto_trading_capital",
              "max_single_trade_amount",
              "max_daily_loss"):
        if not _is_non_negative_number(block[k]):
            _err(errors, f"{path}.{k}", "value_error",
                 f"{k} must be a non-negative number")

    if not _is_non_negative_int(block["max_open_positions"]):
        _err(errors, f"{path}.max_open_positions", "value_error",
             "max_open_positions must be an integer >= 0")

    # Per-broker rule: max_single_trade_amount <= max_auto_trading_capital
    try:
        mst = float(block["max_single_trade_amount"])
        mac = float(block["max_auto_trading_capital"])
        if _is_non_negative_number(block["max_single_trade_amount"]) \
                and _is_non_negative_number(block["max_auto_trading_capital"]) \
                and mst > mac:
            _err(errors, f"{path}.max_single_trade_amount",
                 "single_trade_exceeds_capital",
                 f"max_single_trade_amount ({mst}) must be "
                 f"<= max_auto_trading_capital ({mac})")
    except (TypeError, ValueError):
        pass


def _validate_global(g: Any, errors: list) -> None:
    if not isinstance(g, dict):
        _err(errors, "global", "type_error", "global must be an object")
        return
    extra = set(g.keys()) - _GLOBAL_KEYS
    if extra:
        _err(errors, "global", "unknown_key",
             f"unknown keys in global: {sorted(extra)}")
    missing = _GLOBAL_KEYS - set(g.keys())
    if missing:
        _err(errors, "global", "missing_key",
             f"missing keys in global: {sorted(missing)}")
        return
    for k in ("auto_trading_enabled", "kill_switch"):
        if not _is_bool(g[k]):
            _err(errors, f"global.{k}", "type_error",
                 f"{k} must be a boolean")
    if not _is_non_negative_number(g["max_auto_trading_capital"]):
        _err(errors, "global.max_auto_trading_capital", "value_error",
             "max_auto_trading_capital must be a non-negative number")
    if not _is_valid_until_utc(g["auto_trading_enabled_until_utc"]):
        _err(errors, "global.auto_trading_enabled_until_utc", "value_error",
             "auto_trading_enabled_until_utc must be null or an ISO-8601 string")


def _validate_routing(r: Any, errors: list) -> None:
    if not isinstance(r, dict):
        _err(errors, "routing", "type_error", "routing must be an object")
        return
    extra = set(r.keys()) - _ROUTING_KEYS
    if extra:
        _err(errors, "routing", "unknown_key",
             f"unknown keys in routing: {sorted(extra)}")
    missing = _ROUTING_KEYS - set(r.keys())
    if missing:
        _err(errors, "routing", "missing_key",
             f"missing keys in routing: {sorted(missing)}")
        return

    allowed = r["allowed_brokers"]
    if not isinstance(allowed, list) or not all(isinstance(x, str) for x in allowed):
        _err(errors, "routing.allowed_brokers", "type_error",
             "allowed_brokers must be a list of strings")
        allowed = []

    # Reject forbidden brokers in allowed_brokers
    for b in allowed:
        if b in FORBIDDEN_BROKERS:
            _err(errors, "routing.allowed_brokers", "forbidden_broker",
                 f"broker {b!r} is not permitted in M13.4A")
        elif b not in ALLOWED_BROKER_WHITELIST:
            _err(errors, "routing.allowed_brokers", "unknown_broker",
                 f"broker {b!r} is not in the M13.4A whitelist")

    default = r["default_broker"]
    if not isinstance(default, str):
        _err(errors, "routing.default_broker", "type_error",
             "default_broker must be a string")
    elif default in FORBIDDEN_BROKERS:
        _err(errors, "routing.default_broker", "forbidden_broker",
             f"default_broker {default!r} is not permitted in M13.4A")
    elif default not in allowed:
        _err(errors, "routing.default_broker", "not_in_allowed",
             f"default_broker {default!r} must be in allowed_brokers")

    overrides = r["route_overrides"]
    if not isinstance(overrides, dict):
        _err(errors, "routing.route_overrides", "type_error",
             "route_overrides must be an object")
    else:
        for route_name, broker_name in overrides.items():
            if not isinstance(route_name, str) or not isinstance(broker_name, str):
                _err(errors, "routing.route_overrides", "type_error",
                     "route_overrides entries must be string->string")
                continue
            if broker_name in FORBIDDEN_BROKERS:
                _err(errors, f"routing.route_overrides.{route_name}",
                     "forbidden_broker",
                     f"route_overrides.{route_name}={broker_name!r} "
                     f"is not permitted in M13.4A")
            elif broker_name not in allowed:
                _err(errors, f"routing.route_overrides.{route_name}",
                     "not_in_allowed",
                     f"route_overrides.{route_name}={broker_name!r} "
                     f"must be in allowed_brokers")

    if not _is_bool(r["etoro_live_enabled"]):
        _err(errors, "routing.etoro_live_enabled", "type_error",
             "etoro_live_enabled must be a boolean")
    # M13.5.B: etoro_live_enabled=true is now policy-permitted.
    # Runtime guards (.env ETORO_LIVE_ENABLED + EtoroLiveBroker preflight
    # + operator nonce) decide whether a real POST is actually emitted.


def _validate_cross_broker_capital(policy: dict, errors: list) -> None:
    """When global.max_auto_trading_capital > 0, each broker block's
    max_auto_trading_capital must be <= the global cap."""
    g = policy.get("global", {})
    if not isinstance(g, dict):
        return
    gcap = g.get("max_auto_trading_capital")
    if not _is_non_negative_number(gcap):
        return
    gcap_f = float(gcap)
    if gcap_f <= 0:
        return
    for name in ("ibkr", "etoro"):
        b = policy.get(name, {})
        if not isinstance(b, dict):
            continue
        bcap = b.get("max_auto_trading_capital")
        if _is_non_negative_number(bcap) and float(bcap) > gcap_f:
            _err(errors, f"{name}.max_auto_trading_capital",
                 "exceeds_global_capital",
                 f"{name}.max_auto_trading_capital ({float(bcap)}) "
                 f"must be <= global.max_auto_trading_capital ({gcap_f})")


def validate_policy(policy: Any) -> ValidationResult:
    """Validate a broker allocation policy. Pure function — no I/O.

    Returns ValidationResult(ok=bool, errors=[{path, code, msg}, ...]).
    """
    errors: list[dict] = []

    if not isinstance(policy, dict):
        _err(errors, "$", "type_error", "policy must be an object")
        return ValidationResult(ok=False, errors=errors)

    extra = set(policy.keys()) - _TOP_LEVEL_KEYS
    if extra:
        _err(errors, "$", "unknown_key",
             f"unknown top-level keys: {sorted(extra)}")

    missing = _TOP_LEVEL_KEYS - set(policy.keys())
    if missing:
        _err(errors, "$", "missing_key",
             f"missing top-level keys: {sorted(missing)}")

    if "version" in policy:
        if not isinstance(policy["version"], int) or isinstance(policy["version"], bool):
            _err(errors, "version", "type_error", "version must be an integer")
        elif policy["version"] != POLICY_VERSION:
            _err(errors, "version", "version_mismatch",
                 f"version must be {POLICY_VERSION}")

    if "global" in policy:
        _validate_global(policy["global"], errors)
    if "ibkr" in policy:
        _validate_broker_block("ibkr", policy["ibkr"], errors)
    if "ibkr_paper" in policy:
        _validate_lane_block("ibkr_paper", policy["ibkr_paper"], errors)
    if "ibkr_live" in policy:
        _validate_lane_block("ibkr_live", policy["ibkr_live"], errors)
    if "etoro" in policy:
        _validate_broker_block("etoro", policy["etoro"], errors)
    if "routing" in policy:
        _validate_routing(policy["routing"], errors)

    if not errors:
        _validate_cross_broker_capital(policy, errors)

    return ValidationResult(ok=(not errors), errors=errors)


# ─────────────────────────────────────────────────────────────────────────────
# Persistence (reuses portfolio_risk_state KV table — no schema change)
# ─────────────────────────────────────────────────────────────────────────────

def _default_copy() -> dict:
    """Deep copy of DEFAULT_POLICY (json round-trip — safe & cheap)."""
    return json.loads(json.dumps(DEFAULT_POLICY))


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Idempotent — does not change live schema if the table already exists."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS portfolio_risk_state ("
        "  key TEXT PRIMARY KEY,"
        "  value TEXT,"
        "  updated_at TEXT"
        ")"
    )


def load_policy(conn: sqlite3.Connection) -> dict:
    """Load policy from portfolio_risk_state.

    - Missing row -> DEFAULT_POLICY (deep copy)
    - Corrupt JSON -> DEFAULT_POLICY (deep copy) + warning
    - Valid JSON returned as-is (no implicit migration in M13.4A)
    """
    _ensure_table(conn)
    row = conn.execute(
        "SELECT value FROM portfolio_risk_state WHERE key=?",
        (POLICY_KEY,),
    ).fetchone()
    if row is None or row[0] is None:
        return _default_copy()
    try:
        parsed = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        log.warning("[broker_allocation] corrupt JSON in %s, "
                    "returning DEFAULT_POLICY: %s", POLICY_KEY, exc)
        return _default_copy()
    if not isinstance(parsed, dict):
        log.warning("[broker_allocation] stored policy is not an object, "
                    "returning DEFAULT_POLICY")
        return _default_copy()
    return parsed


def save_policy(conn: sqlite3.Connection, policy: dict) -> None:
    """Persist policy after validating. Raises ValueError on invalid input."""
    result = validate_policy(policy)
    if not result.ok:
        raise ValueError(f"invalid policy: {result.errors}")
    _ensure_table(conn)
    conn.execute(
        "INSERT OR REPLACE INTO portfolio_risk_state (key, value, updated_at) "
        "VALUES (?, ?, ?)",
        (POLICY_KEY,
         json.dumps(policy, sort_keys=True),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Read helpers (for future M13.5+ — not wired in M13.4A)
# ─────────────────────────────────────────────────────────────────────────────

def is_broker_allowed(policy: dict, broker_name: str) -> bool:
    """True iff broker_name is listed in routing.allowed_brokers AND not
    in FORBIDDEN_BROKERS. Defensive against malformed policies."""
    if not isinstance(broker_name, str):
        return False
    if broker_name in FORBIDDEN_BROKERS:
        return False
    routing = policy.get("routing") if isinstance(policy, dict) else None
    if not isinstance(routing, dict):
        return False
    allowed = routing.get("allowed_brokers")
    if not isinstance(allowed, list):
        return False
    return broker_name in allowed


def is_auto_trading_allowed(policy: dict, broker_name: str) -> tuple[bool, str]:
    """Return (allowed, reason). False with named reason when blocked.

    Blocks (in order):
      - policy missing/invalid -> "policy_missing"
      - global.auto_trading_enabled False -> "global_disabled"
      - global.kill_switch True -> "global_kill_switch"
      - broker not in allowed_brokers -> "broker_not_allowed"
      - etoro_real requested while etoro_live_enabled False
        -> "etoro_live_disabled"
      - broker block missing -> "broker_block_missing"
      - broker.auto_trading_enabled False -> "broker_disabled"
      - broker.kill_switch True -> "broker_kill_switch"
    """
    if not isinstance(policy, dict):
        return False, "policy_missing"
    if not isinstance(broker_name, str):
        return False, "broker_not_allowed"

    g = policy.get("global")
    if not isinstance(g, dict):
        return False, "policy_missing"
    if g.get("kill_switch") is True:
        return False, "global_kill_switch"
    if g.get("auto_trading_enabled") is not True:
        return False, "global_disabled"

    routing = policy.get("routing") or {}
    # Strict: routing.etoro_live_enabled must be True identity, not just truthy.
    etoro_live = (isinstance(routing, dict)
                  and routing.get("etoro_live_enabled") is True)

    # Special-case etoro_real: blocked unless etoro_live_enabled True.
    # In M13.4A validation rejects etoro_live_enabled=true, so this returns
    # False here. The check is retained for M13.5+ once the flag is allowed.
    if broker_name == "etoro_real" and not etoro_live:
        return False, "etoro_live_disabled"

    if not is_broker_allowed(policy, broker_name):
        return False, "broker_not_allowed"

    # Map broker_name -> broker block. paper has no block; treat as
    # globally controlled only.
    block_key = None
    if broker_name in ("ibkr_paper", "ibkr_live", "ibkr"):
        block_key = "ibkr"
    elif broker_name in ("etoro_paper", "etoro_real"):
        block_key = "etoro"

    if block_key is None:
        # e.g. "paper" — no broker-level block, fall back to global gates only.
        return True, "ok"

    b = policy.get(block_key)
    if not isinstance(b, dict):
        return False, "broker_block_missing"
    if b.get("kill_switch") is True:
        return False, "broker_kill_switch"
    if b.get("auto_trading_enabled") is not True:
        return False, "broker_disabled"

    return True, "ok"


__all__ = [
    "DEFAULT_POLICY",
    "POLICY_KEY",
    "POLICY_VERSION",
    "ALLOWED_BROKER_WHITELIST",
    "FORBIDDEN_BROKERS",
    "ValidationResult",
    "validate_policy",
    "load_policy",
    "save_policy",
    "is_broker_allowed",
    "is_auto_trading_allowed",
]
