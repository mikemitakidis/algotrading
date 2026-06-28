#!/usr/bin/env python3
"""M21.U4 EU source inspector (read-only, strict).

Inspects an OFFICIAL EU index constituent / ETF-holdings file (CSV/XLSX/PDF)
given as an EXPLICIT path. Reports structure, SHA-256, candidate constituent
rows, ISIN/ticker/name columns, dup ISINs, and an accept/review recommendation.
Writes nothing; touches no repo file; commits nothing.

Strict rules (per review):
  * git gate: branch == main, HEAD == the closed M21.U3.HK commit, tree clean.
  * ACCEPT requires EXACT expected count (no +/-2 band) AND no duplicate ISINs
    AND a usable ticker column (else REVIEW_NEEDED).
  * CSV read with utf-8-sig (strips BOM).
  * header row must have stronger evidence: ISIN column PLUS (name or ticker),
    not just any one keyword.
  * source_url / download_url printed if recorded in a sidecar .url file.

Usage:
  PYTHONPATH=/opt/algo-trader /opt/algo-trader/venv/bin/python3 \
    /tmp/eu_source_inspect.py <official_file_path> <expected_count>
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
EXPECT_HEAD = "16d60d73ffb961cd167a1b91fd8afcf57e067434"
_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")


def _git(*args):
    p = subprocess.run(["git", "-C", str(REPO), *args],
                       capture_output=True, text=True)
    if p.returncode != 0:
        print("FAIL_CLOSED: git %s failed: %s"
              % (" ".join(args), (p.stderr or "").strip()))
        sys.exit(2)
    return p.stdout.strip()


def verify_git_state():
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    head = _git("rev-parse", "HEAD")
    dirty = _git("status", "--porcelain")
    print("branch=%s" % branch)
    print("head=%s" % head)
    print("git_clean=%s" % ("yes" if not dirty else "NO"))
    if branch != "main":
        print("FAIL_CLOSED: not on main"); sys.exit(2)
    if head != EXPECT_HEAD:
        print("FAIL_CLOSED: HEAD %s != %s" % (head, EXPECT_HEAD)); sys.exit(2)
    if dirty:
        print("FAIL_CLOSED: tree not clean"); sys.exit(2)


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
            print("pdf_error=%s" % e); return rows
        with pdfplumber.open(path) as pdf:
            for p in pdf.pages:
                for tb in (p.extract_tables() or []):
                    rows += [["" if c is None else str(c) for c in r]
                             for r in tb]
        return rows
    import csv
    sample = open(path, encoding="utf-8-sig", errors="replace").read(4000)
    delim = ";" if sample.count(";") > sample.count(",") else ","
    return list(csv.reader(open(path, encoding="utf-8-sig", errors="replace"),
                           delimiter=delim))


def _detect(header):
    isin = ticker = name = None
    for j, h in enumerate(header):
        hl = str(h).strip().lower()
        if isin is None and "isin" in hl:
            isin = j
        if ticker is None and re.search(r"ticker|symbol|mnemonic|ric", hl):
            ticker = j
        if name is None and re.search(r"name|instrument|company|security", hl):
            name = j
    return isin, ticker, name


def main():
    if len(sys.argv) != 3:
        print("usage: eu_source_inspect.py <file> <expected_count>")
        sys.exit(2)
    path = Path(sys.argv[1])
    expected = int(sys.argv[2])
    if not path.is_file():
        print("FILE_NOT_FOUND: %s" % path); sys.exit(2)

    verify_git_state()

    # optional sidecar .url file recording the download URL
    side = path.with_suffix(path.suffix + ".url")
    if side.is_file():
        print("download_url=%s" % side.read_text(encoding="utf-8").strip())
    else:
        print("download_url=(none recorded)")

    b = path.read_bytes()
    print("file=%s" % path)
    print("sha256=%s" % hashlib.sha256(b).hexdigest())
    print("bytes=%d" % len(b))

    rows = _read_rows(path)
    print("raw_rows=%d" % len(rows))
    for i, r in enumerate(rows[:6]):
        print("head[%d]=%s" % (i, r))

    # header row needs ISIN + (name or ticker)
    hi = None
    for i, r in enumerate(rows[:20]):
        isin_i, tick_i, name_i = _detect(r)
        if isin_i is not None and (name_i is not None or tick_i is not None):
            hi = i
            break
    if hi is None:
        print("RECOMMENDATION=REVIEW_NEEDED (no header row with ISIN + "
              "name/ticker; paste head[] to map columns)")
        _invariants()
        return
    isin_i, tick_i, name_i = _detect(rows[hi])
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
    print("duplicate_isins=%s" % (dup_isin[:10] or "none"))

    n = len(isins)
    print("candidate_constituents=%d" % n)
    # strict ACCEPT: exact count, no dup ISIN, usable ticker column present
    if n == expected and not dup_isin and tick_i is not None and \
            len(tickers) == expected:
        print("RECOMMENDATION=ACCEPT_for_inspection (exact %d, ticker column "
              "present)" % expected)
    elif n == expected and not dup_isin and tick_i is None:
        print("RECOMMENDATION=REVIEW_NEEDED (exact %d ISINs but NO ticker "
              "column; needs a reviewed ISIN->ticker mapping source)"
              % expected)
    else:
        print("RECOMMENDATION=REVIEW_NEEDED (count %d != %d or dup ISINs or "
              "ticker mismatch)" % (n, expected))

    _invariants()


def _invariants():
    try:
        print("global_symbols=%d"
              % len(json.loads(GLOBAL.read_text())["symbols"]))
    except Exception as e:
        print("global_symbols=ERROR:%s" % e)
    try:
        from bot.universe.active_selection import get_scan_ready_symbols
        print("scan_ready=%d" % len(get_scan_ready_symbols()))
    except Exception as e:
        print("scan_ready=ERROR:%s" % e)
    dirty = _git("status", "--porcelain")
    print("git_status_clean=%s" % ("yes" if not dirty else "NO"))


if __name__ == "__main__":
    main()
