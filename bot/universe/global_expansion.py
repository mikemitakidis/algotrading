"""M21.U1 — global inactive candidate normaliser (framework only).

Pure, offline transform: curated CSV rows (sourced from dated, vaulted files)
-> validated, INACTIVE SymbolRecord dicts -> deterministic global_expanded.json.

This is the FRAMEWORK milestone: it ships no real or synthetic candidate
records. configs/universe/global_expanded.json is committed as an empty envelope
(symbols: []). Real per-region data lands in later milestones (M21.U2+), each
from a verified, dated source vaulted via source_ingest.

STRICT ISOLATION (enforced by tests):
  * production imports: stdlib + bot.universe.schema + bot.universe.suffixes ONLY
    (no bot.universe.registry in production — the US-collision check reads the US
    JSON files directly via stdlib json),
  * no network libs, no scanner/paper/live/brokers/providers/dashboard/main,
  * never writes anything except the global_expanded.json path passed to the
    explicit write function; no deletion/pruning; diff is report-only.

Every produced record is inactive/unverified:
  active=false, scan_ready=false, data_quality_status="unverified",
  liquidity fields null. The schema has no execution_eligible /
  paper_routing_eligible fields, so records never carry them.
"""
from __future__ import annotations

import csv
import json
import re
from datetime import date as _date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bot.universe import schema as _schema
from bot.universe import suffixes as _suffixes

_REPO = Path(__file__).resolve().parents[2]
_US_SEED = _REPO / "configs" / "universe" / "us_seed.json"
_US_EXPANDED = _REPO / "configs" / "universe" / "us_expanded.json"
_GLOBAL_EXPANDED = _REPO / "configs" / "universe" / "global_expanded.json"

GLOBAL_SCHEMA_VERSION = "m21u_global_candidates_v1"
GLOBAL_DESCRIPTION = (
    "Global INACTIVE universe candidates (M21.U). All records active=false, "
    "scan_ready=false, data_quality_status=unverified; liquidity null. NOT "
    "added to active_selection._DEFAULT_PATHS; runtime/scanner/paper ignore "
    "these. Built by bot.universe.global_expansion from verified, dated, "
    "vaulted sources.")

# curated input contract
_REQUIRED_COLS = ("region", "index_source", "exchange_prefix", "local_ticker",
                  "yfinance_symbol", "company_name", "source_name",
                  "source_asof", "verification_status")
_OPTIONAL_COLS = ("isin", "weight", "sector", "notes")
_ALLOWED_COLS = frozenset(_REQUIRED_COLS + _OPTIONAL_COLS)
_VERIFIED = "VERIFIED"
_SKIP_STATUSES = ("NEEDS_REVIEW", "EXCLUDE")
_FORMULA_PREFIXES = ("=", "+", "-", "@")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Supported (region, index_source, exchange_prefix) adapters for the M21.U1
# first cut. A curated row's (region, index_source, exchange_prefix) triple must
# be one of these exactly; mismatches (e.g. UK,DAX,XETRA) are rejected. China,
# Italy, and ADRs are intentionally absent.
_SUPPORTED_ADAPTERS = frozenset({
    ("UK", "FTSE100", "LSE"),
    ("HK", "HSI", "HKEX"),
    ("JP", "NIKKEI225", "TSE"),
    ("EU", "DAX", "XETRA"),
    ("EU", "CAC", "EPA"),
    ("EU", "AEX", "AEX"),
    ("EU", "IBEX", "BME"),
    ("EU", "SMI", "SIX"),
})
# fields that must never contain separators/whitespace/traversal
_TOKEN_FIELDS = ("local_ticker", "yfinance_symbol", "exchange_prefix")


class NormaliserError(ValueError):
    """Raised when curated input fails validation."""


# ── curated CSV parsing + validation ──
def _cell_is_formula(value: str) -> bool:
    return isinstance(value, str) and value[:1] in _FORMULA_PREFIXES


def parse_curated_csv(path: Path) -> List[Dict[str, str]]:
    """Parse a curated CSV into row dicts. Enforces the column contract and the
    formula-injection guard. Does NOT yet map to records."""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        cols = set(reader.fieldnames or [])
        missing = set(_REQUIRED_COLS) - cols
        if missing:
            raise NormaliserError(f"missing required columns: {sorted(missing)}")
        unknown = cols - _ALLOWED_COLS
        if unknown:
            raise NormaliserError(f"unknown columns: {sorted(unknown)}")
        rows: List[Dict[str, str]] = []
        for i, raw in enumerate(reader, start=2):  # row 1 is the header
            row = {k: (v if v is not None else "") for k, v in raw.items()}
            # formula-injection guard on every present cell
            for k, v in row.items():
                if _cell_is_formula(v):
                    raise NormaliserError(
                        f"row {i}: formula-injection cell in {k!r}: {v!r}")
            rows.append(row)
        return rows


def _validate_token(field: str, value: str) -> None:
    if not value:
        raise NormaliserError(f"{field} must be non-empty")
    if any(c.isspace() for c in value) or any(
            bad in value for bad in ("/", "\\", "..")):
        raise NormaliserError(
            f"{field} must not contain whitespace/'/'/'\\\\'/'..': {value!r}")


# ── mapping curated row -> SymbolRecord dict ──
def to_record_dict(row: Dict[str, str]) -> Dict[str, Any]:
    """Map ONE curated VERIFIED row to a SymbolRecord-compatible dict. Identity
    fields are derived from suffixes.exchange_info, never hardcoded. Raises
    NormaliserError on any contract violation."""
    for f in _TOKEN_FIELDS:
        _validate_token(f, row.get(f, ""))
    exchange = row["exchange_prefix"]
    if exchange not in _suffixes.EXCHANGES:
        raise NormaliserError(f"unsupported exchange prefix: {exchange!r}")
    region_in = row.get("region", "")
    index_in = row.get("index_source", "")
    # fix 1: the curated (region, index_source, exchange_prefix) triple must be
    # an approved adapter; rejects mismatches like UK,DAX,XETRA.
    if (region_in, index_in, exchange) not in _SUPPORTED_ADAPTERS:
        raise NormaliserError(
            f"unsupported (region, index_source, exchange_prefix) adapter: "
            f"({region_in!r}, {index_in!r}, {exchange!r})")
    local = row["local_ticker"]
    # fix 2: HK curated source must preserve zero-padding — local_ticker must be
    # exactly 4 digits (e.g. 0700 accepted, 5 rejected).
    if exchange == "HKEX" and not re.fullmatch(r"\d{4}", local):
        raise NormaliserError(
            f"HKEX local_ticker must be exactly 4 digits (zero-padded), "
            f"got {local!r}")
    internal = f"{exchange}:{local}"
    # canonical yfinance symbol from the static suffix table; the curated value
    # must match exactly (catches a wrong suffix in the source).
    expected_yf = _suffixes.to_yfinance_symbol(internal)
    if row["yfinance_symbol"] != expected_yf:
        raise NormaliserError(
            f"yfinance_symbol {row['yfinance_symbol']!r} != expected "
            f"{expected_yf!r} for {internal}")
    info = _suffixes.exchange_info(exchange)
    if not row.get("company_name"):
        raise NormaliserError(f"missing company_name for {internal}")
    asof = row.get("source_asof", "")
    if not _DATE_RE.match(asof):
        raise NormaliserError(
            f"source_asof must be strict YYYY-MM-DD, got {asof!r}")
    try:
        _date.fromisoformat(asof)
    except ValueError:
        raise NormaliserError(f"source_asof not a valid date: {asof!r}")
    region_tag = f"region:{info.region.lower()}"
    rec = {
        "internal_symbol": internal,
        "provider_symbols": {"yfinance": expected_yf},
        "asset_class": _schema.AssetClass.EQUITY.value,
        "name": row["company_name"],
        "exchange": exchange,
        "country": info.country,
        "region": info.region,
        "currency": info.currency,
        "timezone": info.timezone,
        "trading_calendar": info.trading_calendar,
        "universe_tags": ["global_candidate", region_tag],
        "active": False,
        "scan_ready": False,
        "source": row["source_name"],
        "as_of_date": row["source_asof"],
        # deterministic UTC timestamp derived from the dated source (midnight
        # UTC); no wall-clock, preserving byte-determinism.
        "first_seen_utc": f"{row['source_asof']}T00:00:00+00:00",
        "sector": (row.get("sector") or None),
        "industry": None,
        "data_quality_status": _schema.DataQualityStatus.UNVERIFIED.value,
        # fix 4: liquidity fields explicitly null (keys present, value None).
        "avg_volume_20d": None,
        "avg_dollar_volume_20d": None,
        "median_spread_bps": None,
        "min_liquidity_tier": None,
        "notes": (row.get("notes") or None),
    }
    return rec


# ── US registry collision data (read US JSON directly; no registry import) ──
def _load_us_identifiers() -> Tuple[set, set]:
    internals: set = set()
    yfs: set = set()
    for p in (_US_SEED, _US_EXPANDED):
        if not p.exists():
            continue
        doc = json.loads(p.read_text(encoding="utf-8"))
        for r in doc.get("symbols", []):
            internals.add(r.get("internal_symbol"))
            yf = (r.get("provider_symbols") or {}).get("yfinance")
            if yf:
                yfs.add(yf)
    return internals, yfs


# ── build the global candidate set ──
def build_records(rows: List[Dict[str, str]], *,
                  us_internals: Optional[set] = None,
                  us_yfs: Optional[set] = None) -> Dict[str, Any]:
    """Build the deterministic global-candidate envelope from curated rows.

    Only VERIFIED rows become records. Returns
    {envelope, skipped: {reason: count}}. Hard-fails on dup/collision/contract
    violations. Every record validates via SymbolRecord.from_dict.
    """
    if us_internals is None or us_yfs is None:
        us_internals, us_yfs = _load_us_identifiers()
    skipped: Dict[str, int] = {}
    seen_internal: set = set()
    seen_yf: set = set()
    records: List[Dict[str, Any]] = []
    for row in rows:
        status = (row.get("verification_status") or "").strip()
        if status in _SKIP_STATUSES:
            skipped[status] = skipped.get(status, 0) + 1
            continue
        if status != _VERIFIED:
            raise NormaliserError(
                f"unknown verification_status: {status!r}")
        rec = to_record_dict(row)
        internal = rec["internal_symbol"]
        yf = rec["provider_symbols"]["yfinance"]
        if internal in seen_internal:
            raise NormaliserError(f"duplicate internal_symbol: {internal}")
        if yf in seen_yf:
            raise NormaliserError(f"duplicate provider symbol: {yf}")
        if internal in us_internals:
            raise NormaliserError(
                f"collision with US registry internal_symbol: {internal}")
        if yf in us_yfs:
            raise NormaliserError(
                f"collision with US registry provider symbol: {yf}")
        # validate against the real schema (raises on any identity/format issue)
        _schema.SymbolRecord.from_dict(rec)
        seen_internal.add(internal)
        seen_yf.add(yf)
        records.append(rec)
    records.sort(key=lambda r: r["internal_symbol"])  # deterministic
    envelope = {
        "schema_version": GLOBAL_SCHEMA_VERSION,
        "description": GLOBAL_DESCRIPTION,
        "symbols": records,
    }
    return {"envelope": envelope, "skipped": skipped,
            "count": len(records)}


def _canonical_json(envelope: Dict[str, Any]) -> str:
    """Deterministic JSON serialisation (sorted symbols, stable formatting)."""
    return json.dumps(envelope, indent=2, sort_keys=True) + "\n"


def write_global_expanded(envelope: Dict[str, Any],
                          path: Optional[Path] = None) -> Path:
    """Explicit, deterministic write. The ONLY function that mutates a file."""
    target = Path(path) if path is not None else _GLOBAL_EXPANDED
    target.write_text(_canonical_json(envelope), encoding="utf-8")
    return target


def empty_envelope() -> Dict[str, Any]:
    return {"schema_version": GLOBAL_SCHEMA_VERSION,
            "description": GLOBAL_DESCRIPTION, "symbols": []}


# ── report-only diff (no auto-apply, removals advisory only) ──
def diff_against_existing(new_envelope: Dict[str, Any],
                          existing_path: Optional[Path] = None
                          ) -> Dict[str, Any]:
    """Advisory diff between a freshly built envelope and the committed file.

    Returns added/removed/changed/unchanged lists. REPORT ONLY: this function
    never writes, never deletes; 'removed' is quarantined for human review.
    """
    target = Path(existing_path) if existing_path is not None \
        else _GLOBAL_EXPANDED
    existing: Dict[str, Dict[str, Any]] = {}
    if target.exists():
        doc = json.loads(target.read_text(encoding="utf-8"))
        for r in doc.get("symbols", []):
            existing[r["internal_symbol"]] = r
    new_map = {r["internal_symbol"]: r
               for r in new_envelope.get("symbols", [])}
    added = sorted(set(new_map) - set(existing))
    removed = sorted(set(existing) - set(new_map))
    changed = sorted(k for k in (set(new_map) & set(existing))
                     if new_map[k] != existing[k])
    unchanged = sorted(k for k in (set(new_map) & set(existing))
                       if new_map[k] == existing[k])
    return {"added": added, "removed_advisory_only": removed,
            "changed": changed, "unchanged": unchanged,
            "auto_apply": False, "note": "removals are advisory; no pruning"}


def build_from_csv(path: Path) -> Dict[str, Any]:
    """Convenience: parse + build from one curated CSV (no write)."""
    return build_records(parse_curated_csv(Path(path)))


def _main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser("bot.universe.global_expansion")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--csv", required=True)
    b.add_argument("--write", action="store_true",
                   help="write configs/universe/global_expanded.json")
    b.add_argument("--dry-run", action="store_true",
                   help="print counts + diff, write nothing (default)")
    args = ap.parse_args(argv)
    if args.cmd == "build":
        res = build_from_csv(Path(args.csv))
        diff = diff_against_existing(res["envelope"])
        out = {"count": res["count"], "skipped": res["skipped"], "diff": diff}
        if args.write and not args.dry_run:
            p = write_global_expanded(res["envelope"])
            out["written"] = str(p)
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(_main())
