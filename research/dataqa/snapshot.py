"""research.dataqa.snapshot — immutable dataset snapshot manifests (canonical B02).

A manifest freezes exactly which bytes an experiment used. For every file it
records the SHA-256, the size and, for bar files, the row count, columns and
timestamp range. The manifest digest identifies the snapshot, and lane B's
trial ledger records it as ``data_digest``.

``verify`` re-hashes a directory against a manifest and reports every
difference: modified, missing and unexpected files. A snapshot is valid only
if the list is empty.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Union

import pandas as pd

from research.common.hashing import digest

MANIFEST_VERSION = 1
BAR_SUFFIXES = (".parquet", ".csv")


def sha256_file(path: Union[str, Path], chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _describe(path: Path) -> Dict:
    info: Dict = {}
    try:
        df = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
    except Exception as exc:                            # unreadable files are still hashed
        return {"readable": False, "error": type(exc).__name__}
    info.update({"readable": True, "rows": int(len(df)), "columns": [str(c) for c in df.columns]})
    if "ts_utc" in df.columns and len(df):
        ts = pd.to_datetime(df["ts_utc"], utc=True)
        info.update({"first_ts": str(ts.min()), "last_ts": str(ts.max())})
    return info


def build(root: Union[str, Path], files: Optional[Iterable[Union[str, Path]]] = None, *, name: str = "",
          describe: bool = True) -> Dict:
    root = Path(root)
    paths = sorted(Path(p) for p in files) if files is not None else sorted(p for p in root.rglob("*") if p.is_file())
    entries = []
    for p in paths:
        full = p if p.is_absolute() else root / p
        rel = full.relative_to(root).as_posix()
        e = {"path": rel, "sha256": sha256_file(full), "bytes": full.stat().st_size}
        if describe and full.suffix in BAR_SUFFIXES:
            e.update(_describe(full))
        entries.append(e)
    body = {"manifest_version": MANIFEST_VERSION, "name": name, "files": entries}
    body["snapshot_digest"] = digest(body)
    return body


def verify(manifest: Dict, root: Union[str, Path]) -> List[str]:
    root = Path(root)
    problems: List[str] = []
    body = {k: v for k, v in manifest.items() if k != "snapshot_digest"}
    if digest(body) != manifest.get("snapshot_digest"):
        problems.append("manifest digest does not match its content (manifest edited)")
    listed = {e["path"]: e for e in manifest.get("files", [])}
    present = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    for rel, e in sorted(listed.items()):
        full = root / rel
        if rel not in present:
            problems.append("missing: %s" % rel)
        elif sha256_file(full) != e["sha256"]:
            problems.append("modified: %s" % rel)
    for rel in sorted(present - set(listed)):
        problems.append("unexpected: %s" % rel)
    return problems


def write(manifest: Dict, path: Union[str, Path]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return p


__all__ = ["MANIFEST_VERSION", "sha256_file", "build", "verify", "write"]
