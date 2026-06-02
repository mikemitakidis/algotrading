"""bot/risk_authority/engine.py — M14.E Risk Authority Engine.

PURE decision core. Per ChatGPT M14.E corrections:

  #1 — `decide(...)` is PURE. It does not write to the DB, does not
       open files, does not call any broker. A separate
       `decide_and_audit(...)` (see `audit_decisions.py`) is the thin
       wrapper that performs the audit-table writes.

  #2 — This module imports NO ingestion / adapter / broker code:
         * not bot.risk_authority.ingest
         * not bot.risk_authority.ingest_exposure
         * not bot.risk_authority.ingest_etoro / ingest_ibkr
         * not bot.risk_authority.ingest_etoro_exposure / ingest_ibkr_exposure
         * not bot.etoro.live_broker / tools.etoro_live_write
       The engine consumes a RiskSnapshot. AST-enforced in tests.

  #3 — Concentration cap in M14.E is per-symbol only. Sector hook is
       documented as design-only.

  #4 — Combined-exposure cap covers all four scopes (ibkr_paper,
       ibkr_live, etoro_paper, etoro_real). Unknown scope ⇒ block.

  #5 — Daily-loss latch uses UTC trading day.

Hard invariants enforced here:
  * Unknown PnL ⇒ block live, reason 'daily_pnl_unknown'.
  * Unknown exposure ⇒ block auto-trade, reason 'exposure_unknown'.
  * Known zero is distinguished from unknown zero at every consumer
    site (we call ScopeView.is_pnl_known() / is_exposure_known(), never
    raw numeric columns).
  * No HTTP write verb. No order method. No live broker construction.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from bot.risk_authority.authority import (
    Authority,
    REQUIRED_AUTHORITY,
    is_monotone_safe,
)
from bot.risk_authority.snapshot import (
    ALL_BROKER_SCOPES,
    GlobalView,
    RiskSnapshot,
    ScopeView,
)

log = logging.getLogger(__name__)


# ── Stable reason codes (closed set) ───────────────────────────────────────────


REASON_CODES = frozenset({
    "policy_invalid",
    "global_kill",
    "broker_kill",
    "global_auto_disabled",
    "broker_auto_disabled",
    "broker_not_allowed",
    "etoro_live_flag_disabled",
    "etoro_live_env_disabled",
    "authority_too_low",
    "amount_invalid",
    "amount_below_min",
    "single_trade_cap_exceeded",
    "broker_capital_cap_exceeded",
    "global_capital_cap_exceeded",
    "combined_exposure_cap_exceeded",    # NEW: distinct from global_capital
    "combined_exposure_unknown",        # any scope unknown ⇒ fail-closed
    "broker_open_positions_exceeded",
    "global_open_positions_exceeded",
    "global_open_positions_unknown",
    "broker_daily_loss_exceeded",
    "daily_pnl_unknown",                # M14.C-style fail-closed
    "global_daily_loss_exceeded",
    "global_daily_loss_unknown",
    "drawdown_throttle_hit",
    "concentration_cap_exceeded",
    "market_closed",
    "quote_stale",
    "spread_too_wide",
    "exposure_unknown",                 # M14.D-style fail-closed
    "exposure_stale",
    "daily_loss_block_active",          # M14.C latch carry-forward
})


# ── Policy view consumed by the engine ─────────────────────────────────────────


@dataclass(frozen=True)
class RiskPolicyView:
    """Read-only subset of policy the engine needs.

    Loaded from environment via `load_policy_view_from_env()` so the
    engine stays pure: no I/O at decision time. The caller assembles
    this before calling decide().
    """
    # Kill switches
    global_kill_switch: bool = False
    broker_kill_switch: Dict[str, bool] = field(default_factory=dict)

    # Auto/manual toggles
    global_auto_enabled: bool = True
    broker_auto_enabled: Dict[str, bool] = field(default_factory=dict)
    allowed_brokers: Tuple[str, ...] = ALL_BROKER_SCOPES

    # eToro live flags (mirrors M13.5.B preflight)
    etoro_live_flag_enabled: bool = False     # routing.etoro_live_enabled
    etoro_live_env_enabled: bool = False      # ETORO_LIVE_ENABLED in env

    # Amount / sizing
    amount_min_usd: float = 10.0
    single_trade_cap_usd: float = 1000.0

    # Capital caps
    broker_capital_cap_usd: Dict[str, float] = field(default_factory=dict)
    global_capital_cap_usd: Optional[float] = None
    combined_exposure_cap_usd: Optional[float] = None

    # Position caps
    broker_open_positions_cap: Dict[str, int] = field(default_factory=dict)
    global_open_positions_cap: Optional[int] = None

    # Daily-loss caps
    broker_daily_loss_cap_usd: Dict[str, float] = field(default_factory=dict)
    global_daily_loss_cap_usd: Optional[float] = None

    # Drawdown throttle: above this drawdown, fail.
    drawdown_throttle_threshold: float = 0.10   # 10% drawdown from peak

    # Concentration (per-symbol)
    per_symbol_exposure_cap_usd: Optional[float] = None

    # Staleness thresholds (seconds) — applied at gate 24 / 17 / 20
    pnl_max_age_sec: int = 300
    exposure_max_age_sec: int = 120

    # Default starting authority per scope.
    default_authority: Dict[str, Authority] = field(default_factory=dict)

    # Source-of-truth version for the policy_version field. Optional.
    version: Optional[int] = None


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("true", "1", "yes", "on")


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def load_policy_view_from_env() -> RiskPolicyView:
    """Helper for callers. Pure: only reads os.environ. No file I/O,
    no DB call, no broker call.

    NOTE: For production use, prefer `RiskPolicyView.from_allocation_policy(
    bot.broker_allocation.load_policy(conn))` so the engine respects the
    M13.4A dashboard-set policy. This env-only path is the fallback for
    contexts (tests, CLI scripts) that have no DB connection.
    """
    return RiskPolicyView(
        global_kill_switch=_env_bool("RISK_GLOBAL_KILL_SWITCH", False),
        global_auto_enabled=_env_bool("RISK_GLOBAL_AUTO_ENABLED", True),
        allowed_brokers=tuple(ALL_BROKER_SCOPES),
        etoro_live_flag_enabled=_env_bool("ETORO_ROUTING_LIVE_ENABLED", False),
        etoro_live_env_enabled=_env_bool("ETORO_LIVE_ENABLED", False),
        amount_min_usd=_env_float("RISK_AMOUNT_MIN_USD", 10.0),
        single_trade_cap_usd=_env_float("RISK_SINGLE_TRADE_CAP_USD", 1000.0),
        global_capital_cap_usd=_env_float("RISK_GLOBAL_CAPITAL_CAP_USD", 100000.0),
        combined_exposure_cap_usd=_env_float(
            "RISK_COMBINED_EXPOSURE_CAP_USD", 50000.0),
        global_open_positions_cap=_env_int("RISK_GLOBAL_MAX_OPEN_POSITIONS", 10),
        global_daily_loss_cap_usd=_env_float("RISK_MAX_DAILY_LOSS_USD", 3000.0),
        drawdown_throttle_threshold=_env_float(
            "RISK_DRAWDOWN_THROTTLE_THRESHOLD", 0.10),
        per_symbol_exposure_cap_usd=_env_float(
            "RISK_PER_SYMBOL_EXPOSURE_CAP_USD", 10000.0),
        pnl_max_age_sec=_env_int("RISK_PNL_MAX_AGE_SEC", 300),
        exposure_max_age_sec=_env_int("RISK_EXPOSURE_MAX_AGE_SEC", 120),
    )


# Family map: the M13.4A policy stores per-vendor blocks ('ibkr', 'etoro')
# whose caps apply to BOTH scopes in that family. The engine operates on
# per-scope caps (ibkr_paper / ibkr_live / etoro_paper / etoro_real), so
# the bridge fans the vendor caps out across both scopes per family.
_M13_4A_FAMILY_MAP = {
    "ibkr":  ("ibkr_live", "ibkr_paper"),
    "etoro": ("etoro_real", "etoro_paper"),
}


def policy_view_from_allocation_policy(
    policy: dict,
    *,
    env_overrides: bool = True,
) -> RiskPolicyView:
    """Bridge M13.4A broker-allocation policy (the dashboard-set source
    of truth) into the engine's RiskPolicyView.

    Per ChatGPT M14.E correction (hard requirement #4):
        Policy source MUST respect the existing M13.4A broker allocation
        / dashboard policy. Do not create a disconnected env-only policy
        source.

    Reads the policy dict shape that `bot.broker_allocation.load_policy()`
    returns. The caller is responsible for invoking load_policy(conn)
    and passing the result here — this function does NO I/O so the
    engine and bridge remain pure.

    `env_overrides=True` lets specific env vars override only the values
    that the M13.4A policy does NOT carry today (combined exposure cap,
    drawdown threshold, per-symbol cap, ETORO_LIVE_ENABLED). This keeps
    M13.4A's existing UX as the source of truth for everything it
    already covers.
    """
    if not isinstance(policy, dict):
        raise TypeError(f"policy must be a dict, got {type(policy).__name__}")

    g = policy.get("global") if isinstance(policy.get("global"), dict) else {}
    routing = policy.get("routing") if isinstance(policy.get("routing"), dict) else {}

    # Kill switches: M13.4A has one global kill plus per-family kill in
    # each vendor block. Fan family kill out to both scopes in the family.
    broker_kill: Dict[str, bool] = {}
    broker_auto: Dict[str, bool] = {}
    broker_capital_cap: Dict[str, float] = {}
    broker_open_positions_cap: Dict[str, int] = {}
    broker_daily_loss_cap: Dict[str, float] = {}

    for family, scopes in _M13_4A_FAMILY_MAP.items():
        block = policy.get(family) if isinstance(policy.get(family), dict) else {}
        family_kill = bool(block.get("kill_switch", False))
        family_auto = bool(block.get("auto_trading_enabled", False))
        family_cap_capital = block.get("max_auto_trading_capital")
        family_cap_single = block.get("max_single_trade_amount")  # noqa: F841
        family_cap_daily_loss = block.get("max_daily_loss")
        family_cap_positions = block.get("max_open_positions")
        for s in scopes:
            broker_kill[s] = family_kill
            broker_auto[s] = family_auto
            if isinstance(family_cap_capital, (int, float)) and family_cap_capital > 0:
                broker_capital_cap[s] = float(family_cap_capital)
            if isinstance(family_cap_positions, int) and family_cap_positions > 0:
                broker_open_positions_cap[s] = family_cap_positions
            if isinstance(family_cap_daily_loss, (int, float)) and family_cap_daily_loss > 0:
                broker_daily_loss_cap[s] = float(family_cap_daily_loss)

    # Allowed brokers (intersected with ALL_BROKER_SCOPES — engine only
    # knows the four canonical scopes, not 'paper').
    raw_allowed = routing.get("allowed_brokers") if isinstance(
        routing.get("allowed_brokers"), list) else []
    allowed = tuple(s for s in raw_allowed if s in ALL_BROKER_SCOPES)

    # eToro live flag — from policy.routing.etoro_live_enabled (M13.4A
    # source of truth). Env var ETORO_LIVE_ENABLED stays as a SECOND
    # gate (the runtime double-flag invariant from M13.5.B).
    etoro_live_flag = bool(routing.get("etoro_live_enabled", False))

    # Pick smallest per-vendor single-trade cap as the global single-trade
    # ceiling (defensive: never widen above policy).
    single_caps = []
    for family in _M13_4A_FAMILY_MAP:
        block = policy.get(family) if isinstance(policy.get(family), dict) else {}
        cap = block.get("max_single_trade_amount")
        if isinstance(cap, (int, float)) and cap > 0:
            single_caps.append(float(cap))
    single_trade_cap_usd = min(single_caps) if single_caps else 1000.0

    # Global capital cap from policy.global.max_auto_trading_capital.
    global_capital_cap = g.get("max_auto_trading_capital")
    if not isinstance(global_capital_cap, (int, float)) or global_capital_cap <= 0:
        global_capital_cap = None
    else:
        global_capital_cap = float(global_capital_cap)

    # Knobs M13.4A does not store today — fall back to env (or fixed default).
    combined_exposure_cap = (
        _env_float("RISK_COMBINED_EXPOSURE_CAP_USD", 50000.0)
        if env_overrides else 50000.0
    )
    drawdown_thresh = (
        _env_float("RISK_DRAWDOWN_THROTTLE_THRESHOLD", 0.10)
        if env_overrides else 0.10
    )
    per_symbol_cap = (
        _env_float("RISK_PER_SYMBOL_EXPOSURE_CAP_USD", 10000.0)
        if env_overrides else 10000.0
    )
    global_open_positions_cap = (
        _env_int("RISK_GLOBAL_MAX_OPEN_POSITIONS", 10)
        if env_overrides else 10
    )
    global_daily_loss_cap = (
        _env_float("RISK_MAX_DAILY_LOSS_USD", 3000.0)
        if env_overrides else 3000.0
    )
    amount_min = _env_float("RISK_AMOUNT_MIN_USD", 10.0) if env_overrides else 10.0
    etoro_live_env = _env_bool("ETORO_LIVE_ENABLED", False) if env_overrides else False
    pnl_max_age = _env_int("RISK_PNL_MAX_AGE_SEC", 300) if env_overrides else 300
    exposure_max_age = _env_int("RISK_EXPOSURE_MAX_AGE_SEC", 120) if env_overrides else 120

    return RiskPolicyView(
        global_kill_switch=bool(g.get("kill_switch", False)),
        broker_kill_switch=broker_kill,
        global_auto_enabled=bool(g.get("auto_trading_enabled", False)),
        broker_auto_enabled=broker_auto,
        allowed_brokers=allowed if allowed else tuple(ALL_BROKER_SCOPES),
        etoro_live_flag_enabled=etoro_live_flag,
        etoro_live_env_enabled=etoro_live_env,
        amount_min_usd=amount_min,
        single_trade_cap_usd=single_trade_cap_usd,
        broker_capital_cap_usd=broker_capital_cap,
        global_capital_cap_usd=global_capital_cap,
        combined_exposure_cap_usd=combined_exposure_cap,
        broker_open_positions_cap=broker_open_positions_cap,
        global_open_positions_cap=global_open_positions_cap,
        broker_daily_loss_cap_usd=broker_daily_loss_cap,
        global_daily_loss_cap_usd=global_daily_loss_cap,
        drawdown_throttle_threshold=drawdown_thresh,
        per_symbol_exposure_cap_usd=per_symbol_cap,
        pnl_max_age_sec=pnl_max_age,
        exposure_max_age_sec=exposure_max_age,
        version=policy.get("version") if isinstance(policy.get("version"), int) else None,
    )


# ── Dataclasses for context / request / decision ───────────────────────────────


@dataclass(frozen=True)
class RiskContext:
    """The question to the engine."""
    broker_scope: str
    requested_action: str       # 'trade_open' | 'trade_close' | 'query_authority'
    current_authority: Authority = Authority.SIGNAL_ONLY
    now_utc: Optional[str] = None
    market_open: bool = True
    quote_age_sec: Optional[float] = None
    quote_max_age_sec: float = 30.0
    spread_bps: Optional[float] = None
    spread_max_bps: float = 50.0


@dataclass(frozen=True)
class TradeRequest:
    """Optional payload for trade_open / trade_close."""
    symbol: str
    amount_usd: float
    side: str                    # 'long' | 'short'
    leverage: int = 1
    sl: Optional[float] = None
    tp: Optional[float] = None


@dataclass(frozen=True)
class RiskDecision:
    """The engine's answer. Frozen, fully self-describing."""
    decision_id: str
    taken_at_utc: str
    broker_scope: str
    requested_action: str
    result: str                  # 'allow' | 'block' | 'downgrade_then_block'
    authority_before: Authority
    authority_after: Authority
    reason_codes: Tuple[str, ...]
    recovery_paths: Dict[str, str]
    explainer: str
    snapshot_ref: Optional[int] = None
    request_payload: Optional[dict] = None


# ── Recovery paths for each reason ─────────────────────────────────────────────


RECOVERY_PATHS: Dict[str, str] = {
    "policy_invalid":               "fix policy and reload",
    "global_kill":                  "operator clears global kill switch (manual_reset)",
    "broker_kill":                  "operator clears broker kill switch (manual_reset)",
    "global_auto_disabled":         "operator enables RISK_GLOBAL_AUTO_ENABLED",
    "broker_auto_disabled":         "operator enables broker auto",
    "broker_not_allowed":           "operator adds broker to allowed_brokers",
    "etoro_live_flag_disabled":     "operator sets routing.etoro_live_enabled=true",
    "etoro_live_env_disabled":      "operator sets ETORO_LIVE_ENABLED=true",
    "authority_too_low":            "operator raises authority via manual_reset",
    "amount_invalid":               "supply a positive numeric amount",
    "amount_below_min":             "increase amount above amount_min_usd",
    "single_trade_cap_exceeded":    "reduce amount below single_trade_cap_usd",
    "broker_capital_cap_exceeded":  "wait for broker exposure to drop or raise cap",
    "global_capital_cap_exceeded":  "wait for global exposure to drop or raise cap",
    "combined_exposure_unknown":    "wait for M14.D ingestion to produce FRESH for all unknown scopes",
    "combined_exposure_cap_exceeded": "wait for combined exposure to drop or raise combined cap",
    "broker_open_positions_exceeded": "close existing position on this broker first",
    "global_open_positions_exceeded": "close any existing position first",
    "global_open_positions_unknown":  "wait for M14.D ingestion to produce FRESH for all scopes",
    "broker_daily_loss_exceeded":   "wait for next UTC trading day (latch)",
    "daily_pnl_unknown":            "wait for M14.C ingestion to produce FRESH PnL",
    "global_daily_loss_exceeded":   "wait for next UTC trading day (latch)",
    "global_daily_loss_unknown":    "wait for M14.C ingestion to produce FRESH PnL for all scopes",
    "drawdown_throttle_hit":        "equity recovers above peak * (1 - threshold) + human ack",
    "concentration_cap_exceeded":   "close some of the symbol's exposure first",
    "market_closed":                "wait for market open",
    "quote_stale":                  "wait for fresh quote",
    "spread_too_wide":              "wait for spread to tighten",
    "exposure_unknown":             "wait for M14.D ingestion to produce FRESH exposure",
    "exposure_stale":               "wait for N consecutive fresh exposure reads",
    "daily_loss_block_active":     "wait for next UTC trading day + operator manual_reset",
}


def _scope_view(snapshot: RiskSnapshot, scope: str) -> ScopeView:
    sv = snapshot.scopes.get(scope)
    if sv is None:
        raise ValueError(f"snapshot has no scope {scope!r}; "
                         f"expected one of {sorted(snapshot.scopes)}")
    return sv


# ── Gate functions (each pure: (ctx, snapshot, request, policy) -> reason|None) ──


def _gate_policy(ctx, snap, req, pol):
    # Minimal sanity: policy view must be a RiskPolicyView, scopes
    # present. assemble_snapshot guarantees the latter under normal use.
    if not isinstance(pol, RiskPolicyView):
        return "policy_invalid"
    if not snap.scopes:
        return "policy_invalid"
    return None


def _gate_global_kill(ctx, snap, req, pol):
    return "global_kill" if pol.global_kill_switch else None


def _gate_broker_kill(ctx, snap, req, pol):
    return "broker_kill" if pol.broker_kill_switch.get(ctx.broker_scope, False) else None


def _gate_global_auto(ctx, snap, req, pol):
    if ctx.requested_action == "query_authority":
        return None
    return None if pol.global_auto_enabled else "global_auto_disabled"


def _gate_broker_auto(ctx, snap, req, pol):
    if ctx.requested_action == "query_authority":
        return None
    enabled = pol.broker_auto_enabled.get(ctx.broker_scope, True)
    return None if enabled else "broker_auto_disabled"


def _gate_broker_allowed(ctx, snap, req, pol):
    if ctx.requested_action == "query_authority":
        return None
    return None if ctx.broker_scope in pol.allowed_brokers else "broker_not_allowed"


def _gate_etoro_live_flag(ctx, snap, req, pol):
    if ctx.broker_scope != "etoro_real" or ctx.requested_action == "query_authority":
        return None
    return None if pol.etoro_live_flag_enabled else "etoro_live_flag_disabled"


def _gate_etoro_live_env(ctx, snap, req, pol):
    if ctx.broker_scope != "etoro_real" or ctx.requested_action == "query_authority":
        return None
    return None if pol.etoro_live_env_enabled else "etoro_live_env_disabled"


def _gate_authority(ctx, snap, req, pol):
    needed = REQUIRED_AUTHORITY[ctx.requested_action]
    return None if ctx.current_authority >= needed else "authority_too_low"


def _gate_amount_valid(ctx, snap, req, pol):
    if ctx.requested_action == "query_authority":
        return None
    if req is None:
        return "amount_invalid"
    if not isinstance(req.amount_usd, (int, float)) or isinstance(req.amount_usd, bool):
        return "amount_invalid"
    if req.amount_usd != req.amount_usd:  # NaN
        return "amount_invalid"
    if req.amount_usd <= 0:
        return "amount_invalid"
    return None


def _gate_amount_min(ctx, snap, req, pol):
    if ctx.requested_action == "query_authority" or req is None:
        return None
    return None if req.amount_usd >= pol.amount_min_usd else "amount_below_min"


def _gate_single_trade_cap(ctx, snap, req, pol):
    if ctx.requested_action == "query_authority" or req is None:
        return None
    return None if req.amount_usd <= pol.single_trade_cap_usd else "single_trade_cap_exceeded"


def _gate_broker_capital(ctx, snap, req, pol):
    if ctx.requested_action == "query_authority" or req is None:
        return None
    cap = pol.broker_capital_cap_usd.get(ctx.broker_scope)
    if cap is None:
        return None
    sv = _scope_view(snap, ctx.broker_scope)
    if not sv.is_exposure_known():
        return "exposure_unknown"
    projected = sv.capital_deployed + req.amount_usd
    return None if projected <= cap else "broker_capital_cap_exceeded"


def _gate_global_capital(ctx, snap, req, pol):
    if ctx.requested_action == "query_authority" or req is None:
        return None
    if pol.global_capital_cap_usd is None:
        return None
    g = snap.global_view.combined_capital_deployed
    if g is None:
        return "combined_exposure_unknown"
    projected = g + req.amount_usd
    return None if projected <= pol.global_capital_cap_usd else "global_capital_cap_exceeded"


def _gate_combined_exposure(ctx, snap, req, pol):
    # Correction #4: combined-exposure cap covers ALL four scopes.
    # Unknown ⇒ fail-closed.
    if ctx.requested_action == "query_authority" or req is None:
        return None
    if pol.combined_exposure_cap_usd is None:
        return None
    g = snap.global_view.combined_capital_deployed
    if g is None:
        return "combined_exposure_unknown"
    projected = g + req.amount_usd
    return None if projected <= pol.combined_exposure_cap_usd else "combined_exposure_cap_exceeded"


def _gate_broker_open_positions(ctx, snap, req, pol):
    if ctx.requested_action == "query_authority" or req is None:
        return None
    cap = pol.broker_open_positions_cap.get(ctx.broker_scope)
    if cap is None:
        return None
    sv = _scope_view(snap, ctx.broker_scope)
    if not sv.is_exposure_known():
        return "exposure_unknown"
    return None if sv.open_positions < cap else "broker_open_positions_exceeded"


def _gate_global_open_positions(ctx, snap, req, pol):
    if ctx.requested_action == "query_authority" or req is None:
        return None
    if pol.global_open_positions_cap is None:
        return None
    g = snap.global_view.combined_open_positions
    if g is None:
        return "global_open_positions_unknown"
    return None if g < pol.global_open_positions_cap else "global_open_positions_exceeded"


def _gate_broker_daily_loss(ctx, snap, req, pol):
    if ctx.requested_action == "query_authority":
        return None
    sv = _scope_view(snap, ctx.broker_scope)
    # Carry-forward: M14.C daily-loss latch is a hard block.
    if sv.daily_loss_block_active:
        return "daily_loss_block_active"
    # Fail-closed on unknown PnL — distinguishes known-zero from
    # unknown-zero per M14.C correction.
    if not sv.is_pnl_known():
        return "daily_pnl_unknown"
    cap = pol.broker_daily_loss_cap_usd.get(ctx.broker_scope)
    if cap is None:
        return None
    return None if sv.realised_daily_loss <= cap else "broker_daily_loss_exceeded"


def _gate_global_daily_loss(ctx, snap, req, pol):
    if ctx.requested_action == "query_authority":
        return None
    if pol.global_daily_loss_cap_usd is None:
        return None
    g = snap.global_view.combined_realised_daily_loss
    if g is None:
        return "global_daily_loss_unknown"
    return None if g <= pol.global_daily_loss_cap_usd else "global_daily_loss_exceeded"


def _gate_drawdown(ctx, snap, req, pol):
    if ctx.requested_action == "query_authority":
        return None
    sv = _scope_view(snap, ctx.broker_scope)
    if not sv.is_exposure_known():
        return "exposure_unknown"
    if sv.drawdown_from_peak >= pol.drawdown_throttle_threshold:
        return "drawdown_throttle_hit"
    return None


def _gate_concentration(ctx, snap, req, pol):
    # Correction #3: per-symbol only. Sector is design-only for a later
    # milestone — broker_positions has no sector metadata yet.
    if ctx.requested_action == "query_authority" or req is None:
        return None
    if pol.per_symbol_exposure_cap_usd is None:
        return None
    cur = snap.global_view.per_symbol_exposure.get(req.symbol, 0.0)
    projected = cur + req.amount_usd
    return None if projected <= pol.per_symbol_exposure_cap_usd else "concentration_cap_exceeded"


def _gate_market_open(ctx, snap, req, pol):
    if ctx.requested_action == "query_authority":
        return None
    return None if ctx.market_open else "market_closed"


def _gate_quote_freshness(ctx, snap, req, pol):
    if ctx.requested_action == "query_authority":
        return None
    if ctx.quote_age_sec is None:
        return None  # quote freshness only enforced if supplied
    return None if ctx.quote_age_sec <= ctx.quote_max_age_sec else "quote_stale"


def _gate_spread(ctx, snap, req, pol):
    if ctx.requested_action == "query_authority":
        return None
    if ctx.spread_bps is None:
        return None
    return None if ctx.spread_bps <= ctx.spread_max_bps else "spread_too_wide"


def _gate_data_staleness(ctx, snap, req, pol):
    if ctx.requested_action == "query_authority":
        return None
    sv = _scope_view(snap, ctx.broker_scope)
    if not sv.is_exposure_known():
        return "exposure_unknown"
    if not sv.is_pnl_known():
        return "daily_pnl_unknown"
    # Hysteresis: a single fresh read is not enough; require N=3 to
    # consider stale-gates fully cleared (M14.A staleness rule). We
    # don't reset on every call — the orchestrator's hysteresis counter
    # carries this state across reads.
    if sv.exposure_fresh_reads_count < 1:
        return "exposure_stale"
    return None


# Ordered registry — first-failure-wins. The order matches M14.A §9.
_GATES: Tuple[Tuple[str, Any], ...] = (
    ("policy",                   _gate_policy),
    ("global_kill",              _gate_global_kill),
    ("broker_kill",              _gate_broker_kill),
    ("global_auto",              _gate_global_auto),
    ("broker_auto",              _gate_broker_auto),
    ("broker_allowed",           _gate_broker_allowed),
    ("etoro_live_flag",          _gate_etoro_live_flag),
    ("etoro_live_env",           _gate_etoro_live_env),
    ("authority",                _gate_authority),
    ("amount_valid",             _gate_amount_valid),
    ("amount_min",               _gate_amount_min),
    ("single_trade_cap",         _gate_single_trade_cap),
    ("broker_capital",           _gate_broker_capital),
    ("global_capital",           _gate_global_capital),
    ("combined_exposure",        _gate_combined_exposure),
    ("broker_open_positions",    _gate_broker_open_positions),
    ("global_open_positions",    _gate_global_open_positions),
    ("broker_daily_loss",        _gate_broker_daily_loss),
    ("global_daily_loss",        _gate_global_daily_loss),
    ("drawdown",                 _gate_drawdown),
    ("concentration",            _gate_concentration),
    ("market_open",              _gate_market_open),
    ("quote_freshness",          _gate_quote_freshness),
    ("spread",                   _gate_spread),
    ("data_staleness",           _gate_data_staleness),
)


# ── Public entrypoint ─────────────────────────────────────────────────────────


def decide(
    context: RiskContext,
    snapshot: RiskSnapshot,
    request: Optional[TradeRequest] = None,
    *,
    policy: Optional[RiskPolicyView] = None,
) -> RiskDecision:
    """Pure decision function.

    Walks the 24 gates in fixed order; first failure determines the
    result. On block, the engine also computes a recommended
    `authority_after` (downgrade-only); on allow, authority is
    unchanged.

    Returns a frozen RiskDecision. NO DB write. NO file I/O. NO broker
    call. The caller is responsible for any audit-table write (see
    `audit_decisions.decide_and_audit`).
    """
    pol = policy if policy is not None else load_policy_view_from_env()
    decision_id = str(uuid.uuid4())
    taken_at = datetime.now(timezone.utc).isoformat()

    reasons: List[str] = []
    fired_gate: Optional[str] = None
    for gate_name, gate_fn in _GATES:
        r = gate_fn(context, snapshot, request, pol)
        if r is not None:
            if r not in REASON_CODES:
                # Defensive: never emit a reason not in the closed set.
                # If we hit this, it's a bug — but we must still produce
                # a deterministic decision.
                log.warning("[engine] gate %s emitted unknown reason %r",
                            gate_name, r)
                r = "policy_invalid"
            reasons.append(r)
            fired_gate = gate_name
            break

    if not reasons:
        result = "allow"
        authority_after = context.current_authority
        explainer = (f"allow {context.requested_action} on "
                     f"{context.broker_scope}: all {len(_GATES)} gates passed")
    else:
        result = "block"
        authority_after = _compute_authority_after(context.current_authority,
                                                     reasons[0])
        explainer = (f"block {context.requested_action} on "
                     f"{context.broker_scope}: gate {fired_gate!r} fired "
                     f"with reason {reasons[0]!r}")

    return RiskDecision(
        decision_id=decision_id,
        taken_at_utc=taken_at,
        broker_scope=context.broker_scope,
        requested_action=context.requested_action,
        result=result,
        authority_before=context.current_authority,
        authority_after=authority_after,
        reason_codes=tuple(reasons),
        recovery_paths={r: RECOVERY_PATHS.get(r, "operator review required")
                        for r in reasons},
        explainer=explainer,
        snapshot_ref=None,
        request_payload=(
            {"symbol": request.symbol, "amount_usd": request.amount_usd,
             "side": request.side, "leverage": request.leverage}
            if request is not None else None
        ),
    )


_DOWNGRADE_TRIGGERS = {
    "global_kill":                Authority.OFF,
    "broker_kill":                Authority.OFF,
    "global_auto_disabled":       Authority.SIGNAL_ONLY,
    "broker_auto_disabled":       Authority.SIGNAL_ONLY,
    "daily_loss_block_active":    Authority.SIGNAL_ONLY,
    "broker_daily_loss_exceeded": Authority.SIGNAL_ONLY,
    "global_daily_loss_exceeded": Authority.SIGNAL_ONLY,
    "drawdown_throttle_hit":      Authority.SIGNAL_ONLY,
    "daily_pnl_unknown":          Authority.SIGNAL_ONLY,
    "global_daily_loss_unknown":  Authority.SIGNAL_ONLY,
    "exposure_unknown":           Authority.SIGNAL_ONLY,
    "exposure_stale":             Authority.SIGNAL_ONLY,
    "combined_exposure_unknown":  Authority.SIGNAL_ONLY,
    "global_open_positions_unknown": Authority.SIGNAL_ONLY,
}


def _compute_authority_after(before: Authority, primary_reason: str) -> Authority:
    """Downgrade-only. Returns the lower of `before` and the trigger
    target. Never increases authority."""
    target = _DOWNGRADE_TRIGGERS.get(primary_reason)
    if target is None:
        return before
    return Authority(min(int(before), int(target)))


__all__ = [
    "RiskContext",
    "TradeRequest",
    "RiskDecision",
    "RiskPolicyView",
    "REASON_CODES",
    "RECOVERY_PATHS",
    "load_policy_view_from_env",
    "policy_view_from_allocation_policy",
    "decide",
    "is_monotone_safe",
    "Authority",
]
