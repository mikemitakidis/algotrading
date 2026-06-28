#!/usr/bin/env python3
"""M21.U4.DE DAX inspector for the iShares German holdings layout (read-only).

Understands the iShares Core DAX UCITS ETF (DE) CSV layout:
  header: Emittententicker, Name, Sektor, Anlageklasse, Marktwert,
          Gewichtung (%), Nominalwert, Nominale, Kurs, Standort, Boerse,
          Marktwaehrung
  as-of line: ['Fondsposition per', '25.Juni2026']
Equity constituents = rows where Anlageklasse == Aktien, Boerse == Xetra,
Marktwaehrung == EUR.

Prints every non-empty data row with include/exclude reason, all tickers, and a
strict ACCEPT only at EXACTLY expected_count (default 40) with no dup tickers.
Read-only; git-gated; writes nothing.

Usage:
  /opt/algo-trader/venv/bin/python3 /tmp/dax_inspect.py <file> [expected]
"""
import csv
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


def _git(*a):
    p = subprocess.run(["git", "-C", str(REPO), *a],
                       capture_output=True, text=True)
    if p.returncode != 0:
        print("FAIL_CLOSED: git %s: %s" % (" ".join(a), p.stderr.strip()))
        sys.exit(2)
    return p.stdout.strip()


def gate():
    br, hd, dt = (_git("rev-parse", "--abbrev-ref", "HEAD"),
                  _git("rev-parse", "HEAD"), _git("status", "--porcelain"))
    print("branch=%s" % br)
    print("head=%s" % hd)
    print("git_clean=%s" % ("yes" if not dt else "NO"))
    if br != "main" or hd != EXPECT_HEAD or dt:
        print("FAIL_CLOSED: git gate"); sys.exit(2)


def main():
    if not (2 <= len(sys.argv) <= 3):
        print("usage: dax_inspect.py <file> [expected]"); sys.exit(2)
    path = Path(sys.argv[1])
    expected = int(sys.argv[2]) if len(sys.argv) == 3 else 40
    if not path.is_file():
        print("FILE_NOT_FOUND: %s" % path); sys.exit(2)
    gate()

    side = path.with_suffix(path.suffix + ".url")
    print("download_url=%s" % (side.read_text().strip() if side.is_file()
                               else "(none)"))
    b = path.read_bytes()
    print("file=%s" % path)
    print("sha256=%s" % hashlib.sha256(b).hexdigest())
    print("bytes=%d" % len(b))

    text = open(path, encoding="utf-8-sig", errors="replace").read()
    delim = ";" if text[:4000].count(";") > text[:4000].count(",") else ","
    rows = list(csv.reader(open(path, encoding="utf-8-sig",
                                errors="replace"), delimiter=delim))

    # locate as-of and header rows
    asof = "(not found)"
    hi = None
    for i, r in enumerate(rows[:25]):
        joined = ";".join(str(c) for c in r)
        if re.search(r"(?i)fondsposition per|holdings as of|as of", joined):
            asof = r[1].strip() if len(r) > 1 else joined
        if any("emittententicker" in str(c).strip().lower() for c in r) or \
           (any(str(c).strip().lower() == "name" for c in r) and
                any("anlageklasse" in str(c).strip().lower() or
                    "asset class" in str(c).strip().lower() for c in r)):
            hi = i
    print("as_of_raw=%s" % asof)
    if hi is None:
        print("RECOMMENDATION=REVIEW_NEEDED (no recognizable header)")
        _inv(); return
    header = [str(c).strip() for c in rows[hi]]
    print("header_row=%d" % hi)
    print("header=%s" % header)

    def col(*keys):
        for j, h in enumerate(header):
            hl = h.lower()
            if any(k in hl for k in keys):
                return j
        return None
    c_tick = col("emittententicker", "ticker")
    c_name = col("name")
    c_cls = col("anlageklasse", "asset class")
    c_exch = col("börse", "boerse", "exchange")
    c_ccy = col("marktwährung", "marktwahrung", "currency")
    print("cols: ticker=%s name=%s class=%s exch=%s ccy=%s"
          % (c_tick, c_name, c_cls, c_exch, c_ccy))

    included, excluded, tickers = [], [], []
    print("--- data rows (include/exclude reason) ---")
    for r in rows[hi + 1:]:
        if not any(str(c).strip() for c in r):
            continue  # blank
        g = lambda j: (str(r[j]).strip() if j is not None and len(r) > j
                       else "")
        tk, nm, cls, exch, ccy = (g(c_tick), g(c_name), g(c_cls),
                                  g(c_exch), g(c_ccy))
        if not nm or re.search(r"(?i)fondsposition|disclaimer|source|cash|"
                               r"eur cash|margin|futures", nm):
            excluded.append((tk, nm, "non-constituent/footer/cash"))
            print("  EXCL %-6s %-30s [%s]" % (tk, nm[:30], "footer/cash"))
            continue
        is_eq = cls.lower().startswith("aktien") or cls.lower() == "equity"
        is_xetra = "xetra" in exch.lower()
        is_eur = ccy.upper() == "EUR"
        if is_eq and is_xetra and is_eur and tk:
            included.append((tk, nm)); tickers.append(tk)
            print("  INCL %-6s %-30s" % (tk, nm[:30]))
        else:
            reason = []
            if not is_eq:
                reason.append("class=%s" % cls)
            if not is_xetra:
                reason.append("exch=%s" % exch)
            if not is_eur:
                reason.append("ccy=%s" % ccy)
            if not tk:
                reason.append("no-ticker")
            excluded.append((tk, nm, ",".join(reason)))
            print("  EXCL %-6s %-30s [%s]" % (tk, nm[:30],
                                              ",".join(reason)))

    print("--- summary ---")
    print("included_equity_xetra_eur=%d" % len(included))
    print("excluded=%d" % len(excluded))
    print("all_tickers=%s" % ",".join(sorted(tickers)))
    dup = sorted(t for t, c in Counter(tickers).items() if c > 1)
    print("duplicate_tickers=%s" % (dup or "none"))
    if len(included) == expected and not dup:
        print("RECOMMENDATION=ACCEPT (exact %d equity Xetra EUR, no dups)"
              % expected)
    else:
        print("RECOMMENDATION=REVIEW_NEEDED (%d != %d or dup tickers); this "
              "iShares ETF samples the index and need not equal the official "
              "%d-name DAX membership" % (len(included), expected, expected))
    _inv()


def _inv():
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
    print("git_status_clean=%s"
          % ("yes" if not _git("status", "--porcelain") else "NO"))


if __name__ == "__main__":
    main()
