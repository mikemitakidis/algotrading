"""OP1.3b — adjustment basis and corporate-action evidence of the M16 parquet store (read-only).

Usage (project venv, isolated mode, no bytecode written):
    PYTHONDONTWRITEBYTECODE=1 /opt/algo-trader/venv/bin/python3 -I op1_parquet_basis.py \
        /opt/algo-trader/data/historical /tmp/m21_op1/parquet_basis.csv

For every <provider>/<timeframe>/<SYMBOL>.parquet it records, without printing any price:
rows, first/last bar stamp, share of rows flagged is_adjusted, adj_close nulls, the number of distinct
adjustment ratios and the number of ratio CHANGE POINTS over time. M16 keeps raw OHLC plus a per-bar
adjustment_ratio; a ratio change point is where a split or dividend adjustment boundary sits, so the
count is evidence that corporate actions are present (it does not say which kind).

Read-only: files are opened for reading only; the only file written is the CSV path given, which must
not be inside the store. Standard library + pyarrow (already a project dependency).
"""
import csv
import os
import sys

import pyarrow.parquet as pq

COLS = ("ts_utc", "adj_close", "adjustment_ratio", "is_adjusted")


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    store, out = os.path.realpath(argv[0]), os.path.realpath(argv[1])
    if out.startswith(store + os.sep):
        print("refused: the output must not be inside the store")
        return 2
    rows = []
    for provider in sorted(os.listdir(store)):
        pdir = os.path.join(store, provider)
        if not os.path.isdir(pdir):
            continue
        for tf in sorted(os.listdir(pdir)):
            tdir = os.path.join(pdir, tf)
            if not os.path.isdir(tdir):
                continue
            for f in sorted(os.listdir(tdir)):
                if not f.endswith(".parquet"):
                    continue
                rec = {"provider": provider, "timeframe": tf, "symbol": f[:-8], "error": ""}
                try:
                    with open(os.path.join(tdir, f), "rb") as fh:
                        names = set(pq.ParquetFile(fh).schema_arrow.names)
                        fh.seek(0)
                        t = pq.read_table(fh, columns=[c for c in COLS if c in names])
                    d = {c: t.column(c).to_pylist() for c in t.column_names}
                    ts = d.get("ts_utc") or []
                    ratio = d.get("adjustment_ratio") or []
                    adj = d.get("is_adjusted") or []
                    changes = sum(1 for a, b in zip(ratio, ratio[1:]) if a is not None and b is not None and a != b)
                    rec.update(rows=len(ts), first_bar=str(min(ts)) if ts else "", last_bar=str(max(ts)) if ts else "",
                               missing_columns=";".join(c for c in COLS if c not in names),
                               share_is_adjusted=round(sum(1 for x in adj if x) / len(adj), 4) if adj else "",
                               adj_close_nulls=sum(1 for x in d.get("adj_close", []) if x is None),
                               distinct_ratios=len({x for x in ratio if x is not None}),
                               ratio_change_points=changes,
                               ratio_nulls=sum(1 for x in ratio if x is None))
                except Exception as exc:                     # reported, never fatal
                    rec["error"] = type(exc).__name__
                rows.append(rec)
    keys = ["provider", "timeframe", "symbol", "rows", "first_bar", "last_bar", "share_is_adjusted", "adj_close_nulls",
            "distinct_ratios", "ratio_change_points", "ratio_nulls", "missing_columns", "error"]
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    by = {}
    for r in rows:
        k = (r["provider"], r["timeframe"])
        s = by.setdefault(k, {"files": 0, "rows": 0, "with_ratio_changes": 0, "errors": 0})
        s["files"] += 1
        s["rows"] += r.get("rows") or 0
        s["with_ratio_changes"] += 1 if (r.get("ratio_change_points") or 0) > 0 else 0
        s["errors"] += 1 if r["error"] else 0
    print("provider timeframe files rows symbols_with_adjustment_boundaries read_errors")
    for (p, tf), s in sorted(by.items()):
        print(p, tf, s["files"], s["rows"], s["with_ratio_changes"], s["errors"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
