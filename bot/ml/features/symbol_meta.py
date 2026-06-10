"""bot.ml.features.symbol_meta — static per-symbol metadata features.

All features here are leak_class="safe" — they are STATIC per-symbol
attributes (sector, market cap bucket, IPO year, ETF flag) that do
not depend on any per-anchor data and therefore cannot leak future
information.

The metadata is loaded from a JSON file (typical path:
configs/ml/symbol_metadata.json; example at
configs/ml/symbol_metadata.example.json). The file format is
schema-versioned (schema_version=1 currently). Each symbol entry maps
free-text categorical attributes that get encoded to small integer
codes via the file's own 'encodings' table.

Missing-symbol policy:
  If `symbol` is NOT in the metadata file, every feature for every
  row returns the int code for 'unknown' (=99 in the example file).
  This is intentional: dataset assembly should not fail on an unseen
  symbol — the model can learn that 'sector=unknown' is its own
  signal. Operators add new symbols to the JSON when they want them
  encoded properly.

Features (5):
  sector_code          int8  encoded sector
  market_cap_code      int8  encoded market-cap bucket
  asset_class_code     int8  encoded asset class (equity/etf/adr)
  ipo_year             int16 raw year (0 if unknown)
  is_etf               int8  1 if etf else 0; -1 if unknown

All values are constant across the entire `bars` index — every row
gets the same value for a given (symbol, metadata_file) combination.
This is by design: the dataset assembler joins these constants to
every anchor.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

import numpy as np
import pandas as pd

from bot.ml.schemas import FeatureSpec
from bot.ml.features.base import align_to_bars


GROUP_NAME = "symbol_meta"
GROUP_VERSION = 1

# Sentinel int code used when the metadata file lists 'unknown' but
# the value is genuinely missing. Matches the example JSON.
_UNKNOWN_CODE = 99


def _spec(name: str, *, dtype: str, desc: str,
           value_range=None) -> FeatureSpec:
    return FeatureSpec(
        feature_id=f"{GROUP_NAME}.{name}",
        feature_group=GROUP_NAME,
        feature_group_version=GROUP_VERSION,
        dtype=dtype,
        leak_class="safe",
        lookback_bars=0,             # static — no bar lookback at all
        lookback_unit="bars_at_this_tf",
        computed_from=("__static_metadata__",),
        description=desc,
        value_range=value_range,
        live_compatible=False,
        live_compatible_with=None,
        tested_in="test_m18_ml.py::G2_SymbolMeta",
    )


SPECS: tuple = (
    _spec("sector_code",      dtype="int8",
            desc="integer code for the symbol's sector (or 99 unknown)"),
    _spec("market_cap_code",  dtype="int8",
            desc="integer code for the symbol's market-cap bucket"
                  " (0=micro..4=mega, 5=etf, 99=unknown)"),
    _spec("asset_class_code", dtype="int8",
            desc="integer code for the symbol's asset class"
                  " (0=equity, 1=etf, 2=adr, 99=unknown)"),
    _spec("ipo_year",         dtype="int16",
            desc="IPO year as an integer (0 if unknown)"),
    _spec("is_etf",           dtype="int8",
            desc="1 if ETF, 0 if not, -1 if unknown",
            value_range=(-1.0, 1.0)),
)


def load_metadata(path: Union[str, Path]) -> Dict[str, Any]:
    """Load and lightly validate a symbol_metadata JSON file.

    Returns the parsed dict on success. Raises ValueError on any
    schema violation (missing schema_version, missing symbols block,
    missing encodings block, encodings missing required keys).
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"symbol_metadata file not found: {p}")
    with open(p) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(
            f"symbol_metadata top-level must be a JSON object, got "
            f"{type(data).__name__}")
    if int(data.get("schema_version", 0)) != 1:
        raise ValueError(
            f"symbol_metadata schema_version must be 1, got "
            f"{data.get('schema_version')!r}")
    for key in ("symbols", "encodings"):
        if key not in data:
            raise ValueError(
                f"symbol_metadata missing top-level {key!r} block")
    enc = data["encodings"]
    for key in ("sector", "market_cap_bucket", "asset_class"):
        if key not in enc:
            raise ValueError(
                f"symbol_metadata encodings missing {key!r} table")
    return data


def compute(bars: pd.DataFrame, *, symbol: str,
              metadata: Optional[Mapping[str, Any]] = None,
              metadata_path: Optional[Union[str, Path]] = None,
              ) -> pd.DataFrame:
    """Compute symbol_meta features for `bars`.

    Parameters
    ----------
    bars            anchor TF bars; only the row count and index are
                      used (every output row has the same value).
    symbol          the symbol to look up (case-sensitive — should
                      match the JSON key, conventionally uppercase).
    metadata        parsed metadata dict (from load_metadata).
                      EITHER metadata OR metadata_path must be given.
    metadata_path   path to a JSON file (read each call — small,
                      cheap, and avoids stale-cache surprises).

    Returns
    -------
    pd.DataFrame with one column per SPECS entry, indexed identically
    to `bars`, with the same value at every row.
    """
    if metadata is None and metadata_path is None:
        raise ValueError(
            "symbol_meta.compute requires either metadata or "
            "metadata_path")
    if metadata is None:
        metadata = load_metadata(metadata_path)

    enc_sector  = metadata["encodings"]["sector"]
    enc_cap     = metadata["encodings"]["market_cap_bucket"]
    enc_asset   = metadata["encodings"]["asset_class"]

    sym_entry = metadata["symbols"].get(symbol, {})
    sector_val = sym_entry.get("sector", "unknown")
    cap_val    = sym_entry.get("market_cap_bucket", "unknown")
    asset_val  = sym_entry.get("asset_class", "unknown")
    sector_code = int(enc_sector.get(sector_val,
                                       enc_sector.get("unknown",
                                                       _UNKNOWN_CODE)))
    cap_code = int(enc_cap.get(cap_val,
                                  enc_cap.get("unknown",
                                                _UNKNOWN_CODE)))
    asset_code = int(enc_asset.get(asset_val,
                                      enc_asset.get("unknown",
                                                      _UNKNOWN_CODE)))
    ipo = sym_entry.get("ipo_year", 0)
    try:
        ipo_year = int(ipo) if ipo is not None else 0
    except (TypeError, ValueError):
        ipo_year = 0

    etf_val = sym_entry.get("etf", None)
    if etf_val is None:
        is_etf = -1
    else:
        is_etf = 1 if bool(etf_val) else 0

    n = len(bars)
    out = pd.DataFrame(index=bars.index)
    out[f"{GROUP_NAME}.sector_code"]      = np.full(n, sector_code,
                                                       dtype=np.int8)
    out[f"{GROUP_NAME}.market_cap_code"]  = np.full(n, cap_code,
                                                       dtype=np.int8)
    out[f"{GROUP_NAME}.asset_class_code"] = np.full(n, asset_code,
                                                       dtype=np.int8)
    out[f"{GROUP_NAME}.ipo_year"]         = np.full(n, ipo_year,
                                                       dtype=np.int16)
    out[f"{GROUP_NAME}.is_etf"]           = np.full(n, is_etf,
                                                       dtype=np.int8)
    return align_to_bars(out, bars, group_name=GROUP_NAME)
