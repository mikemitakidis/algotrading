"""research.dataqa.inventory — read-only historical-data inventory (canonical B01).

``inventory_store(root)`` walks an M16 parquet store laid out as
``<root>/<provider>/<timeframe>/<SYMBOL>.parquet`` (``bot.historical.store``
convention). For every file it reports rows, time span, column schema, bar
spacing and a quality grade (``research.dataqa.quality``).

``inventory_repo(repo_root)`` reports what the REPOSITORY holds: the symbol
universe and metadata files, their consistency, and whether any bars are
present at all.

Nothing is written, fetched or exported. Inventories of the production store
on the VPS must be run there by the operator with approval. This module only
reads the path it is given.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Union

import numpy as np
import pandas as pd

from research.dataqa.quality import audit_panel
from research.dataqa.snapshot import sha256_file


def _spacing(ts: pd.Series) -> Dict:
    if len(ts) < 2:
        return {"median_step_minutes": None}
    d = ts.diff().dropna().dt.total_seconds() / 60.0
    return {"median_step_minutes": float(d.median()), "max_step_minutes": float(d.max())}


def inventory_store(root: Union[str, Path], *, audit: bool = True) -> Dict:
    root = Path(root)
    if not root.exists():
        return {"root": str(root), "exists": False, "datasets": [], "files": 0}
    rows: List[Dict] = []
    groups: Dict = {}
    for p in sorted(root.glob("*/*/*.parquet")):
        provider, timeframe, sym = p.parent.parent.name, p.parent.name, p.stem
        try:
            df = pd.read_parquet(p)
        except Exception as exc:                          # corrupt or foreign file: report, never crash
            rows.append({"provider": provider, "timeframe": timeframe, "symbol": sym, "readable": False,
                         "error": type(exc).__name__, "sha256": sha256_file(p), "bytes": p.stat().st_size})
            continue
        ts = pd.to_datetime(df["ts_utc"], utc=True) if "ts_utc" in df.columns else pd.Series([], dtype="datetime64[ns, UTC]")
        rows.append({"provider": provider, "timeframe": timeframe, "symbol": sym, "readable": True, "rows": int(len(df)),
                     "first_ts": str(ts.min()) if len(ts) else None, "last_ts": str(ts.max()) if len(ts) else None,
                     "columns": [str(c) for c in df.columns], "sha256": sha256_file(p), "bytes": p.stat().st_size,
                     **_spacing(ts.sort_values())})
        groups.setdefault((provider, timeframe), {})[sym] = df
    quality = {}
    if audit:
        for (prov, tf), panel in groups.items():
            quality["%s/%s" % (prov, tf)] = audit_panel(panel, timeframe=tf)
    by_group = [{"provider": pv, "timeframe": tf, "symbols": len(panel),
                 "rows": int(sum(len(x) for x in panel.values()))} for (pv, tf), panel in sorted(groups.items())]
    return {"root": str(root), "exists": True, "files": len(rows), "unreadable": sum(not r["readable"] for r in rows),
            "groups": by_group, "datasets": rows, "quality": quality}


def inventory_repo(repo_root: Union[str, Path]) -> Dict:
    repo = Path(repo_root)
    data = repo / "data"
    out: Dict = {"data_dir_exists": data.exists(), "files": []}
    if data.exists():
        out["files"] = sorted(p.relative_to(repo).as_posix() for p in data.rglob("*") if p.is_file())
    bar_files = [f for f in out["files"] if f.endswith((".parquet",)) or "/historical/" in f]
    out["bar_files"] = bar_files
    out["has_bars"] = bool(bar_files)
    uni = data / "symbol_universe.csv"
    meta = data / "symbol_metadata.csv"
    if uni.exists():
        u = pd.read_csv(uni)
        syms = u.iloc[:, 0].astype(str).str.strip().str.upper()
        out["universe"] = {"symbols": int(len(syms)), "duplicates": int(syms.duplicated().sum()),
                           "list": syms.tolist()}
    if meta.exists():
        m = pd.read_csv(meta)
        ms = m["symbol"].astype(str).str.strip().str.upper()
        out["metadata"] = {"rows": int(len(m)), "duplicates": int(ms.duplicated().sum()),
                           "sectors": {str(k): int(v) for k, v in m["sector"].fillna("(blank)").value_counts().items()},
                           "blank_industry": int(m["industry"].isna().sum()) if "industry" in m.columns else None}
        if "universe" in out:
            out["universe"]["missing_metadata"] = sorted(set(out["universe"]["list"]) - set(ms))
    out["finding"] = ("the repository holds no historical bars; the M16 parquet store lives on the production host"
                      if not out["has_bars"] else "historical bar files are present in the repository")
    return out


__all__ = ["inventory_store", "inventory_repo"]
