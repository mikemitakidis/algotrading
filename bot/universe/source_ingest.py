"""M21.U0 — global source ingestion / raw vault.

Isolated tool that vaults dated ETF/index holdings files IMMUTABLY and records
provenance + SHA-256 in a committed ledger. Upload/approved-source only; no
automated download in this cut.

STRICT ISOLATION (enforced by tests):
  * imports only stdlib (no network libs, no scanner/paper/live/brokers/
    providers, no universe registry),
  * performs NO candidate-registry writes and produces no normalised universe
    file,
  * writes ONLY into the raw vault tree and the provenance ledger,
  * never deletes vault files or ledger entries,
  * makes no price/quality/data-provider calls and no trading.

CLI:
  python -m bot.universe.source_ingest ingest --file <path> --region UK \
      --index-source FTSE100 --source-name "..." --source-type etf_holdings \
      --source-asof 2026-06-30 [--source-url ...] [--licence-note ...]
  python -m bot.universe.source_ingest list
  python -m bot.universe.source_ingest verify
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO = Path(__file__).resolve().parents[2]
_VAULT_DIR = _REPO / "data" / "universe" / "raw_sources"
_LEDGER = _REPO / "configs" / "universe" / "source_registry.json"

LEDGER_SCHEMA_VERSION = "m21u0_source_registry_v1"
_REGIONS = ("UK", "EU", "JP", "HK", "ADR")
_SOURCE_TYPES = ("official_index", "etf_holdings", "exchange_listing")

# Safe-token pattern for any free-text input that becomes part of a source_id
# or a file path. Strict uppercase alnum plus _ . - ; no slashes, spaces, or
# traversal sequences. (A leading dot or a ".." run is additionally rejected.)
_SAFE_TOKEN_RE = re.compile(r"^[A-Z0-9_.-]+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class IngestValidationError(ValueError):
    """Raised when an ingest input fails validation."""


def _validate_source_asof(value: str) -> None:
    # strict YYYY-MM-DD, real calendar date, no slashes/spaces/traversal.
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise IngestValidationError(
            f"source_asof must be strict YYYY-MM-DD, got {value!r}")
    y, m, d = (int(p) for p in value.split("-"))
    try:
        parsed = date(y, m, d)
    except ValueError:
        raise IngestValidationError(
            f"source_asof is not a valid calendar date: {value!r}")
    # round-trip guard (rejects e.g. zero-padded oddities that re-format)
    if parsed.isoformat() != value:
        raise IngestValidationError(
            f"source_asof not canonical YYYY-MM-DD: {value!r}")


def _validate_safe_token(name: str, value: str) -> None:
    if not isinstance(value, str) or value == "":
        raise IngestValidationError(f"{name} must be a non-empty string")
    if any(bad in value for bad in ("/", "\\", "..")):
        raise IngestValidationError(
            f"{name} must not contain '/', '\\\\', or '..': {value!r}")
    if not _SAFE_TOKEN_RE.match(value):
        raise IngestValidationError(
            f"{name} must match {_SAFE_TOKEN_RE.pattern}: {value!r}")


def _validate_path_safe(name: str, value: str) -> None:
    # for any string used in a path/source_id beyond the token rule: no
    # separators, no traversal, non-empty.
    if not isinstance(value, str) or value == "":
        raise IngestValidationError(f"{name} must be a non-empty string")
    if any(bad in value for bad in ("/", "\\", "..")):
        raise IngestValidationError(
            f"{name} must not contain '/', '\\\\', or '..': {value!r}")



# ── ledger I/O (the only writable targets are the vault + this ledger) ──
def _load_ledger() -> Dict[str, Any]:
    if not _LEDGER.exists():
        return {"schema_version": LEDGER_SCHEMA_VERSION,
                "description": "Provenance + SHA-256 index of vaulted global "
                               "source files. Raw files are gitignored; this "
                               "ledger is the committed evidence.",
                "sources": []}
    doc = json.loads(_LEDGER.read_text(encoding="utf-8"))
    sv = doc.get("schema_version")
    if sv != LEDGER_SCHEMA_VERSION:
        raise IngestValidationError(
            f"source_registry schema_version {sv!r} != expected "
            f"{LEDGER_SCHEMA_VERSION!r}")
    if not isinstance(doc.get("sources"), list):
        raise IngestValidationError("source_registry 'sources' must be a list")
    return doc


def _write_ledger(doc: Dict[str, Any]) -> None:
    doc["sources"] = sorted(doc.get("sources", []),
                            key=lambda s: s.get("source_id", ""))
    _LEDGER.write_text(
        json.dumps(doc, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _vault_root() -> Path:
    # Repo anchor derived from the (possibly redirected) vault dir, so the
    # stored vault_path is relative to whichever root the vault lives under.
    # _VAULT_DIR = <root>/data/universe/raw_sources -> parents[2] = <root>.
    return _VAULT_DIR.resolve().parents[2]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _next_seq(sources: List[Dict[str, Any]], region: str,
              index_source: str, source_asof: str) -> str:
    prefix = f"{region}__{index_source}__{source_asof}__"
    existing = [s for s in sources
                if s.get("source_id", "").startswith(prefix)]
    return f"{len(existing) + 1:03d}"


def ingest(*, file: str, region: str, index_source: str, source_name: str,
           source_type: str, source_asof: str, source_url: Optional[str] = None,
           licence_note: Optional[str] = None,
           notes: Optional[str] = None) -> Dict[str, Any]:
    """Vault a raw source file immutably and append a ledger entry.

    Idempotent: identical bytes for the same (region, index_source, asof) ->
    no-op. Different bytes for the same logical source/date -> a NEW vault file
    + entry flagged content_changed (prior file untouched). Never overwrites,
    never deletes.
    """
    src = Path(file)
    if not src.is_file():
        return {"ok": False, "reason": f"file_not_found:{file}"}
    if region not in _REGIONS:
        return {"ok": False, "reason": f"bad_region:{region}"}
    if source_type not in _SOURCE_TYPES:
        return {"ok": False, "reason": f"bad_source_type:{source_type}"}
    # input hardening: anything that becomes part of source_id or a vault path
    # must be a safe token / canonical date with no separators or traversal.
    try:
        _validate_source_asof(source_asof)
        _validate_safe_token("index_source", index_source)
        # region is already constrained to the enum; re-assert path-safety.
        _validate_path_safe("region", region)
    except IngestValidationError as e:
        return {"ok": False, "reason": f"validation:{e}"}

    digest = _sha256_file(src)
    doc = _load_ledger()
    sources = doc.setdefault("sources", [])

    # idempotency: identical content for same logical source/date -> no-op
    for s in sources:
        if (s.get("sha256") == digest and s.get("region") == region
                and s.get("index_source") == index_source
                and s.get("source_asof") == source_asof):
            return {"ok": True, "noop": True, "reason": "already_vaulted",
                    "source_id": s["source_id"]}

    # content-changed detection (same logical source/date, different bytes)
    content_changed = any(
        s.get("region") == region and s.get("index_source") == index_source
        and s.get("source_asof") == source_asof and s.get("sha256") != digest
        for s in sources)

    now_utc = datetime.now(timezone.utc)
    downloaded_at = now_utc.isoformat()
    # filename stamp: compact UTC with an explicit trailing Z, e.g.
    # 20260627T111650Z (second resolution; digest fragment guarantees
    # uniqueness within the same second).
    stamp = now_utc.strftime("%Y%m%dT%H%M%SZ")
    ext = src.suffix.lstrip(".") or "dat"
    dest_dir = _VAULT_DIR / source_asof
    dest_dir.mkdir(parents=True, exist_ok=True)
    # include a short digest fragment so distinct-content ingests within the
    # same second never collide on the filename (the timestamp alone is not
    # unique at sub-second resolution).
    dest = dest_dir / f"{region}__{index_source}__{stamp}__{digest[:12]}.{ext}"
    if dest.exists():
        return {"ok": False, "reason": f"vault_path_exists:{dest}"}
    shutil.copy2(src, dest)  # copy raw bytes into the immutable vault

    seq = _next_seq(sources, region, index_source, source_asof)
    source_id = f"{region}__{index_source}__{source_asof}__{seq}"
    entry = {
        "source_id": source_id, "region": region,
        "index_source": index_source, "source_name": source_name,
        "source_type": source_type, "source_asof": source_asof,
        "downloaded_at": downloaded_at, "source_url": source_url,
        "ingest_method": "upload",
        "vault_path": str(dest.resolve().relative_to(_vault_root())),
        "sha256": digest, "byte_size": src.stat().st_size,
        "row_count_raw": None, "licence_note": licence_note, "notes": notes,
        "content_changed": content_changed,
    }
    sources.append(entry)
    _write_ledger(doc)
    return {"ok": True, "noop": False, "source_id": source_id,
            "content_changed": content_changed, "vault_path": entry["vault_path"]}


def list_sources() -> List[Dict[str, Any]]:
    return _load_ledger().get("sources", [])


def verify() -> Dict[str, Any]:
    """Re-hash every vaulted file and check vs the ledger. Read-only."""
    doc = _load_ledger()
    mismatches: List[str] = []
    missing: List[str] = []
    checked = 0
    for s in doc.get("sources", []):
        p = _vault_root() / s["vault_path"]
        if not p.is_file():
            missing.append(s["source_id"])
            continue
        checked += 1
        if _sha256_file(p) != s.get("sha256"):
            mismatches.append(s["source_id"])
    return {"ok": not mismatches and not missing, "checked": checked,
            "mismatches": mismatches, "missing": missing}


def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser("bot.universe.source_ingest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest")
    ing.add_argument("--file", required=True)
    ing.add_argument("--region", required=True, choices=list(_REGIONS))
    ing.add_argument("--index-source", required=True, dest="index_source")
    ing.add_argument("--source-name", required=True, dest="source_name")
    ing.add_argument("--source-type", required=True, dest="source_type",
                     choices=list(_SOURCE_TYPES))
    ing.add_argument("--source-asof", required=True, dest="source_asof")
    ing.add_argument("--source-url", default=None, dest="source_url")
    ing.add_argument("--licence-note", default=None, dest="licence_note")
    ing.add_argument("--notes", default=None)
    sub.add_parser("list")
    sub.add_parser("verify")
    args = ap.parse_args(argv)

    if args.cmd == "ingest":
        res = ingest(file=args.file, region=args.region,
                     index_source=args.index_source,
                     source_name=args.source_name,
                     source_type=args.source_type,
                     source_asof=args.source_asof, source_url=args.source_url,
                     licence_note=args.licence_note, notes=args.notes)
        print(json.dumps(res, indent=2, sort_keys=True))
        return 0 if res.get("ok") else 1
    if args.cmd == "list":
        print(json.dumps(list_sources(), indent=2, sort_keys=True))
        return 0
    if args.cmd == "verify":
        res = verify()
        print(json.dumps(res, indent=2, sort_keys=True))
        return 0 if res["ok"] else 1
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(_main())
