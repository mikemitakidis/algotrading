#!/usr/bin/env python3
"""M21.U4 EU source inspector (read-only).

Inspects an OFFICIAL EU index constituent / ETF-holdings file (CSV/XLSX/PDF)
given as an EXPLICIT path argument. Reports structure, SHA-256, candidate
constituent rows, ISIN/ticker/name column detection, dup ISINs/tickers, and an
accept/review recommendation. Writes nothing; touches no repo file; commits
nothing.

Usage:
  PYTHONPATH=/opt/algo-trader /opt/algo-trader/venv/bin/python3 \
    /tmp/eu_source_inspect.py <official_file_path> [expected_count]

expected_count defaults to 40 (DAX). Pass 35 for IBEX, 25 for AEX, 20 for SMI,
40 for CAC. The script only REPORTS against the band; it does not gate hard here
(Stage A is inspection). Curation/build stages enforce exact counts later.
"""
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO = Path("/opt/algo-trader")
GLOBAL = REPO / "configs" / "universe" / "global_expanded.json"

_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")


def _read_rows(path):
    ext = path.suffix.lower()
    if ext == ".xlsx":
        import openpyxl
        ws = openpyxl.load_workbook(path, read_only=True, data_only=True).active
        return [["" if c is None else str(c) for c in r]
                for r in ws.iter_rows(values_only=True)]
    if ext == ".pdf":
        rows = []
        try:
            import pdfplumber
        except Exception as e:
            print("pdf_error=pdfplumber unavailable: %s" % e)
            return rows
        with pdfplumber.open(path) as pdf:
            for p in pdf.pages:
                for tb in (p.extract_tables() or []):
                    rows += [["" if c is None else str(c) for c in r]
                             for r in tb]
        return rows
    # CSV / TXT: sniff delimiter (EU files are often ';')
    import csv
    sample = open(path, encoding="utf-8", errors="replace").read(4000)
    delim = ";" if sample.count(";") > sample.count(",") else ","
    return list(csv.reader(open(path, encoding="utf-8", errors="replace"),
                           delimiter=delim))


def _detect(header):
    isin = ticker = name = None
    for j, h in enumerate(header):
        hl = str(h).strip().lower()
        if isin is None and "isin" in hl:
            isin = j
        if ticker is None and re.search(r"ticker|symbol|mnemonic|ric", hl):
            ticker = j
        if name is None and re.search(r"name|instrument|company|security",
                                      hl):
            name = j
    return isin, ticker, name


def main():
    if not (2 <= len(sys.argv) <= 3):
        print("usage: eu_source_inspect.py <file> [expected_count]")
        sys.exit(2)
    path = Path(sys.argv[1])
    expected = int(sys.argv[2]) if len(sys.argv) == 3 else 40
    if not path.is_file():
        print("FILE_NOT_FOUND: %s" % path)
        sys.exit(2)

    b = path.read_bytes()
    print("file=%s" % path)
    print("sha256=%s" % hashlib.sha256(b).hexdigest())
    print("bytes=%d" % len(b))

    rows = _read_rows(path)
    print("raw_rows=%d" % len(rows))
    for i, r in enumerate(rows[:6]):
        print("head[%d]=%s" % (i, r))

    # find header row (first row in the first 15 with isin/ticker/name)
    hi = 0
    for i, r in enumerate(rows[:15]):
        if any(re.search(r"(?i)isin|ticker|symbol|instrument|name", str(c))
               for c in r):
            hi = i
            break
    isin_i, tick_i, name_i = _detect(rows[hi]) if rows else (None, None, None)
    print("header_row=%d isin_col=%s ticker_col=%s name_col=%s"
          % (hi, isin_i, tick_i, name_i))

    data = rows[hi + 1:]
    isins, tickers, names = [], [], []
    for r in data:
        if isin_i is not None and len(r) > isin_i:
            v = str(r[isin_i]).strip()
            if _ISIN_RE.fullmatch(v):
                isins.append(v)
        if tick_i is not None and len(r) > tick_i:
            v = str(r[tick_i]).strip()
            if v and re.search(r"[A-Za-z0-9]", v):
                tickers.append(v)
        if name_i is not None and len(r) > name_i:
            v = str(r[name_i]).strip()
            if re.search(r"[A-Za-z]{2,}", v):
                names.append(v)

    print("isin_rows=%d ticker_rows=%d name_rows=%d"
          % (len(isins), len(tickers), len(names)))
    print("isin_present=%s ticker_present=%s name_present=%s"
          % (isin_i is not None, tick_i is not None, name_i is not None))
    if names:
        print("first5_names=%s" % names[:5])
        print("last5_names=%s" % names[-5:])
    dup_isin = sorted(x for x, c in Counter(isins).items() if c > 1)
    dup_tick = sorted(x for x, c in Counter(tickers).items() if c > 1)
    print("duplicate_isins=%s" % (dup_isin[:10] or "none"))
    print("duplicate_tickers=%s" % (dup_tick[:10] or "none"))

    n = len(isins) if isins else len(names)
    print("candidate_constituents=%d" % n)
    lo, hi_band = expected - 2, expected + 2
    if lo <= n <= hi_band and not dup_isin:
        print("RECOMMENDATION=ACCEPT_for_inspection (proceed to curation "
              "review)")
    else:
        print("RECOMMENDATION=REVIEW_NEEDED (count %d not ~%d or dup ISINs; "
              "paste head[] to map columns)" % (n, expected))

    # invariants (read-only)
    try:
        gs = len(json.loads(GLOBAL.read_text())["symbols"])
        print("global_symbols=%d" % gs)
    except Exception as e:
        print("global_symbols=ERROR:%s" % e)
    try:
        from bot.universe.active_selection import get_scan_ready_symbols
        print("scan_ready=%d" % len(get_scan_ready_symbols()))
    except Exception as e:
        print("scan_ready=ERROR:%s" % e)
    dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"],
                           capture_output=True, text=True).stdout.strip()
    print("git_status_clean=%s" % ("yes" if not dirty else "NO"))


if __name__ == "__main__":
    main()
