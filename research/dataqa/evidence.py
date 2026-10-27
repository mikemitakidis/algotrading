"""research.dataqa.evidence — lane F report: repository inventory, quality-audit calibration, snapshots.

    python -m research.dataqa.evidence --repo . --out docs/research/lane_f/lane_f_data_report

1. **Repository inventory (real):** what the repository actually holds. The
   production M16 store is on the VPS and is NOT read from here.
2. **Quality-audit calibration (synthetic, known truth):** recall and
   precision of the audit for unadjusted splits, missing bars and delistings,
   plus its false-alarm rate on clean markets.
3. **Snapshot manifests:** a manifest of a synthetic store, and proof that
   each kind of tampering is detected.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional, Sequence

import numpy as np
import pandas as pd

from research.common.hashing import digest
from research.common.synthetic import MarketSpec, generate
from research.dataqa import inventory as INV
from research.dataqa import quality as Q
from research.dataqa import snapshot as SN


def calibrate(seeds: Sequence[int] = (21, 22, 23, 24, 25), n_symbols: int = 40, n_sessions: int = 750) -> Dict:
    tp = fn = fp = 0
    miss_exact = miss_sym = 0
    delist_ok = delist_n = 0
    clean_syms = clean_clean = 0
    for sd in seeds:
        m = generate(MarketSpec(n_symbols=n_symbols, n_sessions=n_sessions, seed=sd, split_prob=0.3,
                                missing_bar_prob=0.01, delist_prob=0.25))
        rep = Q.audit_panel(m.bars)
        union = pd.DatetimeIndex(sorted(set().union(*[set(pd.to_datetime(b["ts_utc"], utc=True))
                                                       for b in m.bars.values()])))
        sy = m.symbols.set_index("symbol")
        for s, q in rep["per_symbol"].items():
            truth = sy.loc[s, "split_at"]
            dl = sy.loc[s, "delisted_at"]
            has = pd.notna(truth) and (pd.isna(dl) or truth <= dl)
            found = {pd.Timestamp(x["ts_utc"]) for x in q["suspected_splits"]}
            if has:
                tp += int(pd.Timestamp(truth) in found)
                fn += int(pd.Timestamp(truth) not in found)
            fp += len(found - ({pd.Timestamp(truth)} if has else set()))
            tr = m.truth[s]
            obs = tr["observed"].to_numpy()
            idx = np.flatnonzero(obs)
            ts = pd.to_datetime(tr["ts_utc"], utc=True)
            exp = int(sum((not obs[i]) and ts.iloc[i] in union for i in range(idx.min(), idx.max() + 1)))
            miss_exact += int(q["missing_bars"] == exp)
            miss_sym += 1
            delist_n += 1
            delist_ok += int(q["ended_early"] == bool(pd.notna(dl)))
        clean = generate(MarketSpec(n_symbols=n_symbols, n_sessions=n_sessions, seed=sd + 1000))
        crep = Q.audit_panel(clean.bars)
        clean_syms += crep["symbols"]
        clean_clean += crep["grades"]["clean"]
    return {"seeds": list(seeds), "n_symbols": n_symbols, "n_sessions": n_sessions,
            "splits": {"true_positive": tp, "false_negative": fn, "false_positive": fp,
                       "recall": tp / (tp + fn) if tp + fn else float("nan"),
                       "precision": tp / (tp + fp) if tp + fp else float("nan")},
            "missing_bars": {"symbols_exact": miss_exact, "symbols": miss_sym},
            "delistings": {"symbols_correct": delist_ok, "symbols": delist_n},
            "clean_markets": {"symbols": clean_syms, "graded_clean": clean_clean}}


def snapshot_demo() -> Dict:
    m = generate(MarketSpec(n_symbols=5, n_sessions=250, seed=8))
    with tempfile.TemporaryDirectory() as d:
        for s, b in m.bars.items():
            p = Path(d) / "synthetic" / "1D" / ("%s.parquet" % s)
            p.parent.mkdir(parents=True, exist_ok=True)
            b.to_parquet(p, index=False)
        man = SN.build(d, name="synthetic-demo")
        clean = SN.verify(man, d)
        f = Path(d) / "synthetic" / "1D" / "S000.parquet"
        raw = bytearray(f.read_bytes())
        raw[len(raw) // 3] ^= 0x01
        f.write_bytes(bytes(raw))
        (Path(d) / "synthetic" / "1D" / "S004.parquet").unlink()
        (Path(d) / "synthetic" / "1D" / "S999.parquet").write_bytes(b"x")
        tampered = SN.verify(man, d)
        inv = INV.inventory_store(d, audit=False)
    return {"files": len(man["files"]), "snapshot_digest": man["snapshot_digest"], "clean_verify": clean,
            "tampered_verify": tampered, "example_entry": man["files"][0], "inventory_files": inv["files"]}


def run(repo: Path) -> Dict:
    t0 = time.time()
    j = {"lane": "F", "repo_inventory": INV.inventory_repo(repo), "calibration": calibrate(),
         "snapshot": snapshot_demo()}
    j["report_digest"] = digest(j)
    j["runtime_seconds"] = round(time.time() - t0, 1)
    return j


def _pct(x):
    return "—" if x is None or (isinstance(x, float) and not math.isfinite(x)) else "%.1f %%" % (100 * x)


def render(j: Dict) -> str:
    r, c, s = j["repo_inventory"], j["calibration"], j["snapshot"]
    uni = r.get("universe", {})
    meta = r.get("metadata", {})
    L = ["# Lane F — data inventory, quality audit and snapshots", "",
         "> Section 1 describes the **real repository**. Sections 2–3 use **synthetic** data with known defects "
         "to measure the tools themselves. Nothing was read from, exported from or written to the production "
         "host, and no data was purchased.", "",
         "Generated by `python -m research.dataqa.evidence`. Runtime %.0f s. Report digest `%s`." % (
             j["runtime_seconds"], j["report_digest"][:16]), "",
         "## 1. What the repository holds (B01)", "",
         "- **Finding:** %s." % r["finding"],
         "- Files under `data/`: %s." % (", ".join("`%s`" % f for f in r["files"]) or "none"),
         "- Symbol universe: %d symbols (%d duplicates): %s." % (uni.get("symbols", 0), uni.get("duplicates", 0),
                                                                ", ".join(uni.get("list", []))),
         "- Symbol metadata: %d rows (%d duplicates); sectors %s; blank industry %s." % (
             meta.get("rows", 0), meta.get("duplicates", 0),
             ", ".join("%s %d" % kv for kv in meta.get("sectors", {}).items()), meta.get("blank_industry")),
         "- Universe symbols missing from metadata: %s." % (", ".join(uni.get("missing_metadata", [])) or "none"),
         "",
         "**Consequence.** No real-data research result is possible from this repository alone. The real "
         "inventory (`research.dataqa.inventory.inventory_store`) must be run READ-ONLY against the M16 parquet "
         "root on the production host. That is a VPS action and needs the operator's approval; no export of "
         "bars leaves the host without separate approval. See the runbook in "
         "`docs/research/lane_f/vendor_licensing_memo.md` §5.", "",
         "## 2. Quality-audit calibration (synthetic, known truth)", "",
         "%d synthetic markets × %d symbols × %d sessions were generated with known corruption: unadjusted "
         "splits, 1 %% missing bars and delistings after a decline. The audit was scored against the truth. "
         "Paired clean markets measure its false alarms." % (len(c["seeds"]), c["n_symbols"], c["n_sessions"]),
         "", "| defect | result |", "|---|---|",
         "| unadjusted splits | recall %s, precision %s (%d found, %d missed, %d false alarms) |" % (
             _pct(c["splits"]["recall"]), _pct(c["splits"]["precision"]), c["splits"]["true_positive"],
             c["splits"]["false_negative"], c["splits"]["false_positive"]),
         "| missing bars (count per symbol exact) | %d / %d symbols |" % (c["missing_bars"]["symbols_exact"],
                                                                       c["missing_bars"]["symbols"]),
         "| delistings flagged as ended early | %d / %d symbols correct |" % (c["delistings"]["symbols_correct"],
                                                                           c["delistings"]["symbols"]),
         "| clean markets graded clean | %d / %d symbols |" % (c["clean_markets"]["graded_clean"],
                                                              c["clean_markets"]["symbols"]), "",
         "Limits: synthetic splits are exact ratios (2, 3, 4) with clean volume inversion. Real feeds mix "
         "splits with news gaps, reverse splits and partial adjustments, so expect lower precision on real "
         "data. A detected split is a flag for review, never an automatic adjustment.", "",
         "## 3. Snapshot manifests (B02)", "",
         "A manifest of a %d-file synthetic store (digest `%s`) verified clean: %s. After one flipped bit, one "
         "deleted file and one added file, verification reported:" % (
             s["files"], s["snapshot_digest"][:16], "no problems" if not s["clean_verify"] else s["clean_verify"]), ""]
    L += ["- `%s`" % p for p in s["tampered_verify"]]
    L += ["", "Example manifest entry:", "", "```json", json.dumps(s["example_entry"], indent=1), "```", "",
          "Every lane B trial records a `data_digest`. For real data that digest must be a snapshot manifest "
          "digest, so any later change to the bars is detectable.", ""]
    return "\n".join(L)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    j = run(Path(a.repo))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(j, indent=1, sort_keys=True, default=str) + "\n")
    out.with_suffix(".md").write_text(render(j))
    print("wrote %s.{md,json}" % out, file=sys.stderr)
    return 0


__all__ = ["calibrate", "snapshot_demo", "run", "render", "main"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
