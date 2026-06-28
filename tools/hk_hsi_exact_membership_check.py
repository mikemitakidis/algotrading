#!/usr/bin/env python3
"""HK exact-membership cross-check (read-only, fail-closed).

Compares the 93 HK records committed in configs/universe/global_expanded.json
against the OFFICIAL Hang Seng Indexes constituent list supplied as an explicit
file argument (CSV/XLSX/PDF). Writes nothing; changes no repo file; commits
nothing.

Usage:
  PYTHONPATH=/opt/algo-trader /opt/algo-trader/venv/bin/python3 \
    /tmp/hk_hsi_exact_membership_check.py <official_file_path>

Fail-closed rules:
  * git must be on main, HEAD == the closed M21.U3.HK commit, tree clean.
  * official file is the EXPLICIT path given (no glob, no latest-file guess).
  * PDF: only the HSI Appendix 1 / Constituent List section is parsed; if the
    section can't be isolated or doesn't yield exactly 93 codes -> FAIL_CLOSED.
  * CSV/XLSX: prefer an explicit code/stock-code column; if official_count != 93
    -> FAIL_CLOSED.
  * Any failure prints FAIL_CLOSED and exits non-zero without diffing.
"""
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path("/opt/algo-trader")
GLOBAL = REPO / "configs" / "universe" / "global_expanded.json"
EXPECT_HEAD = "16d60d73ffb961cd167a1b91fd8afcf57e067434"
EXPECT_COUNT = 93


def fail(msg):
    print("FAIL_CLOSED: %s" % msg)
    sys.exit(2)


def _git(*args):
    p = subprocess.run(["git", "-C", str(REPO), *args],
                       capture_output=True, text=True)
    if p.returncode != 0:
        fail("git %s failed (rc=%d): %s"
             % (" ".join(args), p.returncode, (p.stderr or "").strip()))
    return p.stdout.strip()


def verify_git_state():
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    head = _git("rev-parse", "HEAD")
    dirty = _git("status", "--porcelain")
    print("branch=%s" % branch)
    print("head=%s" % head)
    print("git_clean=%s" % ("yes" if not dirty else "NO"))
    if branch != "main":
        fail("not on main (branch=%s)" % branch)
    if head != EXPECT_HEAD:
        fail("HEAD %s != expected %s" % (head, EXPECT_HEAD))
    if dirty:
        fail("working tree not clean")


def load_our_codes():
    s = json.loads(GLOBAL.read_text(encoding="utf-8"))["symbols"]
    codes = sorted(r["internal_symbol"].split(":")[1] for r in s
                   if "region:hk" in r.get("universe_tags", []))
    if len(codes) != EXPECT_COUNT:
        fail("ours_count=%d != %d (committed file changed?)"
             % (len(codes), EXPECT_COUNT))
    return set(codes)


def _norm4(token):
    # Accept HK stock codes safely:
    #   * exact 4-digit codes -> as-is
    #   * 5-digit codes ONLY if they are a leading-zero form (e.g. "00700")
    #     that reduces to a 4-digit code with no loss.
    # Any other token (3-digit, true 5-digit like 82800, non-numeric) -> None.
    s = str(token).strip()
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if len(digits) == 4:
        return digits
    if len(digits) == 5 and digits[0] == "0":
        return digits[1:]
    return None


def parse_pdf(path):
    try:
        import pdfplumber
    except Exception as e:
        fail("pdfplumber unavailable: %s" % e)
    pages_text = []
    with pdfplumber.open(path) as pdf:
        for p in pdf.pages:
            pages_text.append(p.extract_text() or "")
    full = "\n".join(pages_text)
    # Isolate the HSI constituent list / Appendix 1 section. Start at an
    # Appendix-1 + Hang Seng Index heading; stop at the next appendix or the
    # start of a different index's constituent block.
    low = full.lower()
    starts = [m.start() for m in re.finditer(
        r"appendix\s*1\b", low)]
    if not starts:
        # some factsheets use "constituents of the hang seng index"
        starts = [m.start() for m in re.finditer(
            r"constituents? of the hang seng index", low)]
    if not starts:
        fail("could not locate HSI Appendix 1 / constituent-list section")
    start = starts[0]
    rest = low[start + 1:]
    end_rel = re.search(r"appendix\s*2\b|hang seng china enterprises|"
                        r"hang seng tech\b|hscei\b", rest)
    end = (start + 1 + end_rel.start()) if end_rel else len(full)
    section = full[start:end]
    # within the section, constituent rows look like: <code> <name> ... ; the
    # code is a 4-5 digit token at a line start or before a capitalised name.
    codes = set()
    for line in section.splitlines():
        m = re.match(r"\s*(\d{4,5})\b\s+[A-Za-z(]", line)
        if m:
            c = _norm4(m.group(1))
            if c:
                codes.add(c)
    if len(codes) != EXPECT_COUNT:
        fail("PDF Appendix-1 parse yielded %d codes, not %d "
             "(parser not confident; paste the section so I can tighten it)"
             % (len(codes), EXPECT_COUNT))
    return codes


def _detect_code_col(header):
    for j, h in enumerate(header):
        if re.search(r"(?i)stock\s*code|^code$|ticker|sehk|symbol", str(h)):
            return j
    return None


def parse_tabular(path):
    rows = []
    if path.suffix.lower() == ".xlsx":
        try:
            import openpyxl
        except Exception as e:
            fail("openpyxl unavailable: %s" % e)
        ws = openpyxl.load_workbook(path, read_only=True, data_only=True).active
        for r in ws.iter_rows(values_only=True):
            rows.append(["" if c is None else str(c) for c in r])
    else:
        import csv
        rows = list(csv.reader(open(path, encoding="utf-8", errors="replace")))
    if not rows:
        fail("empty tabular file")
    # find a header row with an explicit code column
    code_col = None
    hdr_i = None
    for i, r in enumerate(rows[:15]):
        cc = _detect_code_col(r)
        if cc is not None:
            code_col, hdr_i = cc, i
            break
    codes = set()
    if code_col is not None:
        for r in rows[hdr_i + 1:]:
            if len(r) > code_col:
                c = _norm4(r[code_col])
                if c:
                    codes.add(c)
    else:
        # no explicit column; fail closed rather than guess across all cells
        fail("no explicit stock-code column detected in CSV/XLSX header")
    if len(codes) != EXPECT_COUNT:
        fail("tabular parse yielded %d codes, not %d" % (len(codes),
                                                         EXPECT_COUNT))
    return codes


def main():
    if len(sys.argv) != 2:
        fail("usage: hk_hsi_exact_membership_check.py <official_file_path>")
    path = Path(sys.argv[1])
    if not path.is_file():
        fail("official file not found: %s" % path)

    verify_git_state()
    ours = load_our_codes()

    b = path.read_bytes()
    print("official_file=%s" % path)
    print("official_sha256=%s" % hashlib.sha256(b).hexdigest())
    print("official_bytes=%d" % len(b))

    ext = path.suffix.lower()
    if ext == ".pdf":
        official = parse_pdf(path)
    elif ext in (".csv", ".xlsx"):
        official = parse_tabular(path)
    else:
        fail("unsupported file type: %s (use .pdf/.csv/.xlsx)" % ext)

    missing = sorted(official - ours)   # official codes not in our file
    extra = sorted(ours - official)     # our codes not in official
    exact = (not missing and not extra
             and len(ours) == len(official) == EXPECT_COUNT)

    print("ours_count=%d" % len(ours))
    print("official_count=%d" % len(official))
    print("missing_from_ours=%d %s" % (len(missing), missing))
    print("extra_in_ours=%d %s" % (len(extra), extra))
    print("EXACT_MATCH=%s" % exact)
    if exact:
        print("CLOSEOUT=HK exact membership cross-check PASSED: 93/93 codes "
              "match official HSIL constituents. No repo/data change required.")
    else:
        print("CLOSEOUT=MISMATCH -- STOP. Report only; no patch, no data "
              "change, no commit. Review missing/extra codes before deciding.")


if __name__ == "__main__":
    main()
