"""bot/risk_authority/snapshot.py — M14.E read-only snapshot assembly.

Produces an immutable `RiskSnapshot` for the engine. Reads ONLY from:

  * daily_state_per_broker  (M14.B/C/D-owned columns + lifecycle_json)
  * broker_positions        (M14.D append-only; latest exposure_batch_id
                             per scope via MAX(fetched_at_utc))
  * portfolio_risk_state    (sentinels, manual_reset markers)

Does NOT touch any adapter or ingestion module. Does NOT contact a
broker. Does NOT write to the DB. The engine consumes this object; it
never re-derives state from raw broker calls.

Per ChatGPT M14.E correction #2:
    Engine must not import ingestion/adapters.
    M14.E consumes a `RiskSnapshot`.

Per correction #4:
    Combined-exposure cap includes ALL FOUR scopes
    (ibkr_paper, ibkr_live, etoro_paper, etoro_real).
    A scope whose lifecycle.exposure_status is 'unknown' MUST be
    treated as unknown (combined exposure becomes unknown).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# Per M14.B + ChatGPT correction #4: all four scopes are tracked for
# combined exposure. GLOBAL is computed in-memory from per-broker rows,
# never used as a "broker" key for execution.
ALL_BROKER_SCOPES = (
    "ibkr_live", "ibkr_paper", "etoro_real", "etoro_paper",
)


@dataclass(frozen=True)
class ScopeView:
    """Per-broker frozen view consumed by the engine."""
    scope: str

    # M14.C-owned PnL surface
    realised_pnl_usd:        float
    realised_daily_loss:     float
    daily_pnl_available:     bool          # True iff PnL is known (FRESH or PARTIAL)
    daily_loss_block_active: bool
    pnl_status:              str           # 'fresh' | 'partial' | 'unknown' | 'unavailable'
    pnl_fresh_reads_count:   int

    # M14.D-owned exposure surface
    open_positions:          int
    capital_deployed:        float
    peak_equity:             Optional[float]
    drawdown_from_peak:      float
    exposure_status:         str           # 'exposure_fresh' | 'exposure_partial' | 'exposure_unknown' | 'absent'
    exposure_fresh_reads_count: int
    exposure_batch_id:       Optional[str]

    # Freshness
    last_ingested_at:        Optional[str]

    # Per-position detail (defaulted; must come last in dataclass field order)
    positions:               Tuple[dict, ...] = field(default_factory=tuple)

    # Helpers
    def is_pnl_known(self) -> bool:
        """Distinguishes known-zero from unknown-zero per M14.C
        correction. Engine MUST call this, never read the raw number."""
        return bool(self.daily_pnl_available) and self.pnl_status in ("fresh", "partial")

    def is_exposure_known(self) -> bool:
        """Distinguishes known-zero from unknown-zero per M14.D
        correction. Engine MUST call this."""
        return self.exposure_status in ("exposure_fresh", "exposure_partial")


@dataclass(frozen=True)
class GlobalView:
    """Cross-scope rollup computed in-memory; never written to DB."""
    combined_capital_deployed:  Optional[float]      # None if ANY scope unknown
    combined_open_positions:    Optional[int]        # None if ANY scope unknown
    combined_realised_daily_loss: Optional[float]    # None if ANY scope unknown
    per_symbol_exposure:        Dict[str, float] = field(default_factory=dict)
    any_pnl_unknown:            bool = False
    any_exposure_unknown:       bool = False
    unknown_pnl_scopes:         Tuple[str, ...] = field(default_factory=tuple)
    unknown_exposure_scopes:    Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RiskSnapshot:
    """Frozen evidence object consumed by engine.decide().

    Reproducible: the engine writes this snapshot's JSON to
    `risk_snapshots` so any past decision can be re-run on the exact
    same inputs.
    """
    taken_at_utc:       str
    trading_day_utc:    str
    scopes:             Dict[str, ScopeView]
    global_view:        GlobalView
    policy_version:     Optional[int] = None
    raw_evidence:       dict = field(default_factory=dict)


# ─── DB helpers (read-only) ──────────────────────────────────────────────────


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_scope_row(conn: sqlite3.Connection, today: str,
                    scope: str) -> ScopeView:
    """Read one daily_state_per_broker row and parse its lifecycle_json.
    If no row exists for today, returns a ScopeView with
    pnl_status='unavailable' and exposure_status='absent'."""
    row = conn.execute(
        "SELECT realised_pnl_usd, realised_daily_loss, daily_pnl_available, "
        "       daily_pnl_source, daily_loss_block_active, "
        "       open_positions, capital_deployed, peak_equity, "
        "       drawdown_from_peak, last_ingested_at, lifecycle_json, "
        "       fresh_reads_count "
        "FROM daily_state_per_broker "
        "WHERE date=? AND broker_scope=?",
        (today, scope),
    ).fetchone()
    if not row:
        return ScopeView(
            scope=scope,
            realised_pnl_usd=0.0,
            realised_daily_loss=0.0,
            daily_pnl_available=False,
            daily_loss_block_active=False,
            pnl_status="unavailable",
            pnl_fresh_reads_count=0,
            open_positions=0,
            capital_deployed=0.0,
            peak_equity=None,
            drawdown_from_peak=0.0,
            exposure_status="absent",
            exposure_fresh_reads_count=0,
            exposure_batch_id=None,
            positions=tuple(),
            last_ingested_at=None,
        )
    try:
        lifecycle = json.loads(row[10]) if row[10] else {}
        if not isinstance(lifecycle, dict):
            lifecycle = {}
    except (TypeError, ValueError):
        lifecycle = {}

    pnl_status = lifecycle.get("status") or row[3] or "unavailable"
    exposure_status = lifecycle.get("exposure_status") or "absent"
    exposure_batch_id = lifecycle.get("exposure_batch_id")

    # Load latest batch positions if exposure data is fresh enough to
    # contain a batch id. We never reuse a stale batch's positions; the
    # engine's per-symbol concentration gate consults this.
    positions: Tuple[dict, ...] = tuple()
    if exposure_batch_id and exposure_status in ("exposure_fresh",
                                                  "exposure_partial"):
        positions = _load_positions_for_batch(conn, scope, today,
                                              exposure_batch_id)

    return ScopeView(
        scope=scope,
        realised_pnl_usd=float(row[0] or 0.0),
        realised_daily_loss=float(row[1] or 0.0),
        daily_pnl_available=bool(row[2]),
        daily_loss_block_active=bool(row[4]),
        pnl_status=str(pnl_status),
        pnl_fresh_reads_count=int(row[11] or 0),
        open_positions=int(row[5] or 0),
        capital_deployed=float(row[6] or 0.0),
        peak_equity=row[7],
        drawdown_from_peak=float(row[8] or 0.0),
        exposure_status=str(exposure_status),
        exposure_fresh_reads_count=int(
            lifecycle.get("exposure_fresh_reads_count", 0) or 0
        ),
        exposure_batch_id=exposure_batch_id,
        positions=positions,
        last_ingested_at=row[9],
    )


def _load_positions_for_batch(conn: sqlite3.Connection, scope: str,
                              today: str, batch_id: str) -> Tuple[dict, ...]:
    """Return the positions of one batch as compact dicts. Read-only."""
    rows = conn.execute(
        "SELECT symbol, side, qty, exposure_usd, instrument_id "
        "FROM broker_positions "
        "WHERE broker_scope=? AND date=? AND exposure_batch_id=?",
        (scope, today, batch_id),
    ).fetchall()
    return tuple(
        {
            "symbol":        r[0],
            "side":          r[1],
            "qty":           float(r[2]),
            "exposure_usd":  float(r[3]),
            "instrument_id": r[4],
        }
        for r in rows
    )


def _build_global_view(scopes: Dict[str, ScopeView]) -> GlobalView:
    """Per ChatGPT correction #4: include all four scopes. If ANY scope
    has unknown exposure or unknown PnL, the combined value becomes
    None (engine treats None as fail-closed)."""
    cap = 0.0
    pos = 0
    loss = 0.0
    cap_unknown = False
    pos_unknown = False
    pnl_unknown = False
    unknown_pnl: List[str] = []
    unknown_exp: List[str] = []
    per_symbol: Dict[str, float] = {}

    for sname, sv in scopes.items():
        # Combined exposure: every scope counts. Unknown exposure on
        # ANY scope means we cannot trust the combined number.
        if not sv.is_exposure_known():
            cap_unknown = True
            pos_unknown = True
            unknown_exp.append(sname)
        else:
            cap += sv.capital_deployed
            pos += sv.open_positions

        # Combined daily loss: same rule on the PnL side.
        if not sv.is_pnl_known():
            pnl_unknown = True
            unknown_pnl.append(sname)
        else:
            loss += sv.realised_daily_loss

        # Per-symbol aggregation across scopes (only known-fresh positions).
        if sv.is_exposure_known():
            for p in sv.positions:
                sym = p.get("symbol")
                if not isinstance(sym, str) or not sym:
                    continue
                per_symbol[sym] = per_symbol.get(sym, 0.0) + float(p["exposure_usd"])

    return GlobalView(
        combined_capital_deployed=(None if cap_unknown else cap),
        combined_open_positions=(None if pos_unknown else pos),
        combined_realised_daily_loss=(None if pnl_unknown else loss),
        per_symbol_exposure=per_symbol,
        any_pnl_unknown=pnl_unknown,
        any_exposure_unknown=cap_unknown,
        unknown_pnl_scopes=tuple(sorted(unknown_pnl)),
        unknown_exposure_scopes=tuple(sorted(unknown_exp)),
    )


def assemble_snapshot(conn: sqlite3.Connection, *,
                      trading_day: Optional[str] = None,
                      policy_version: Optional[int] = None) -> RiskSnapshot:
    """Read-only snapshot assembly. The engine consumes the returned
    object; it does not call back into ingestion."""
    today = trading_day or _today_utc()
    scopes: Dict[str, ScopeView] = {}
    for s in ALL_BROKER_SCOPES:
        scopes[s] = _load_scope_row(conn, today, s)
    gv = _build_global_view(scopes)
    return RiskSnapshot(
        taken_at_utc=datetime.now(timezone.utc).isoformat(),
        trading_day_utc=today,
        scopes=scopes,
        global_view=gv,
        policy_version=policy_version,
        raw_evidence={"scope_count": len(scopes)},
    )


__all__ = [
    "ALL_BROKER_SCOPES",
    "ScopeView",
    "GlobalView",
    "RiskSnapshot",
    "assemble_snapshot",
]
