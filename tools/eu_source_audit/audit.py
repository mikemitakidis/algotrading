"""Audit engine: per-venue download + inspect, returning structured results.

Read-only. Saves downloaded files under an output dir (default
/tmp/m21u4_sources) for provenance, but performs no repo writes.
"""
import csv
import datetime
import hashlib
import io
import re
import urllib.request
from pathlib import Path

from tools.eu_source_audit.holdings_link_extractor import extract_and_download

_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ")


def try_download(url, timeout=30):
    """Return (status, data_bytes_or_errmsg, ok_bool)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), True
    except Exception as e:  # noqa: BLE001
        return None, ("ERR:%s" % e).encode(), False


def classify(data):
    if data[:5] == b"%PDF-":
        return "pdf"
    head = data[:400].lower()
    if b"<html" in head or b"<!doctype" in head:
        return None
    if b"isin" in head or data[:3000].count(b";") > 5 \
            or data[:3000].count(b",") > 5:
        return "csv"
    return None


def _read_rows(data, kind):
    if kind == "pdf":
        return []  # audit treats PDFs as "manual review" (no auto parse here)
    text = data.decode("utf-8-sig", errors="replace")
    delim = ";" if text[:4000].count(";") > text[:4000].count(",") else ","
    return list(csv.reader(io.StringIO(text), delimiter=delim))


def _find_asof(rows):
    for r in rows[:25]:
        joined = ";".join(str(c) for c in r)
        if re.search(r"(?i)fondsposition per|holdings as of|as of", joined):
            return r[1].strip() if len(r) > 1 else joined
    return "(not found)"


def _find_header(rows):
    for i, r in enumerate(rows[:25]):
        hl = [str(c).strip().lower() for c in r]
        has_tk = any(re.search(r"ticker|emittententicker", c) for c in hl)
        has_cls = any(re.search(r"anlageklasse|asset class", c) for c in hl)
        if has_tk and has_cls:
            return i
    return None


def _col(header, *keys):
    for j, h in enumerate(header):
        hl = str(h).strip().lower()
        if any(k in hl for k in keys):
            return j
    return None


def inspect_rows(rows, vmeta):
    """Apply the venue equity filter; return dict of inspection facts."""
    out = {
        "raw_rows": len(rows), "as_of": _find_asof(rows),
        "header_row": None, "included": [], "excluded_summary": {},
        "all_tickers": [], "duplicate_tickers": [],
    }
    hi = _find_header(rows)
    out["header_row"] = hi
    if hi is None:
        return out
    header = [str(c).strip() for c in rows[hi]]
    out["header"] = header
    c_tk = _col(header, "emittententicker", "ticker")
    c_nm = _col(header, "name")
    c_cls = _col(header, "anlageklasse", "asset class")
    c_ex = _col(header, "börse", "boerse", "exchange")
    c_cc = _col(header, "marktwährung", "marktwahrung", "market currency",
                "currency")
    flt = vmeta["equity_filter"]
    from collections import Counter
    excl = Counter()
    for r in rows[hi + 1:]:
        if not any(str(c).strip() for c in r):
            continue
        g = lambda j: (str(r[j]).strip() if j is not None and len(r) > j
                       else "")
        tk, nm, cls, ex, cc = (g(c_tk), g(c_nm), g(c_cls), g(c_ex), g(c_cc))
        if not nm or re.search(r"(?i)cash|collateral|future|fx|/usd|margin|"
                               r"disclaimer|source", nm + " " + cls):
            excl["cash/derivative/fx/footer"] += 1
            continue
        is_eq = any(cls.lower().startswith(a) or cls.lower() == a
                    for a in flt["asset_class"])
        is_ex = any(s in ex.lower() for s in flt["exchange_substr"])
        is_cc = cc.upper() in flt["currency"]
        if is_eq and is_ex and is_cc and tk:
            out["included"].append((tk, nm))
            out["all_tickers"].append(tk)
        else:
            why = []
            if not is_eq:
                why.append("class")
            if not is_ex:
                why.append("exch=%s" % (ex or "-"))
            if not is_cc:
                why.append("ccy=%s" % (cc or "-"))
            if not tk:
                why.append("no-ticker")
            excl[",".join(why) or "other"] += 1
    out["excluded_summary"] = dict(excl)
    dup = sorted(t for t, c in Counter(out["all_tickers"]).items() if c > 1)
    out["duplicate_tickers"] = dup
    return out


def audit_venue(venue, vmeta, outdir):
    """Try each endpoint; inspect; return a structured per-venue record."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    ts = _now()
    attempts = []
    best = None
    for idx, (role, url, note) in enumerate(vmeta["endpoints"]):
        status, data, ok = try_download(url)
        rec = {"role": role, "url": url, "note": note,
               "http_status": status, "saved": False, "sha256": None,
               "bytes": len(data) if ok else 0, "kind": None,
               "inspection": None, "recommendation": None}
        if not ok or status != 200:
            rec["recommendation"] = "UNREACHABLE"
            attempts.append(rec)
            continue
        kind = classify(data)
        rec["kind"] = kind
        if not kind:
            rec["recommendation"] = "NOT_A_FILE (html/js/dynamic)"
            attempts.append(rec)
            continue
        fpath = outdir / ("%s_src%d_%s.%s" % (venue, idx, ts, kind))
        fpath.write_bytes(data)
        fpath.with_suffix(fpath.suffix + ".url").write_text(url)
        rec["saved"] = True
        rec["sha256"] = hashlib.sha256(data).hexdigest()
        rec["file"] = str(fpath)
        if kind == "pdf":
            rec["recommendation"] = "MANUAL_REVIEW (pdf not auto-parsed)"
            attempts.append(rec)
            continue
        ins = inspect_rows(_read_rows(data, kind), vmeta)
        rec["inspection"] = ins
        n = len(ins["included"])
        exp = vmeta["expected"]
        is_official = role in ("official_index", "official_exchange")
        if n == exp and not ins["duplicate_tickers"]:
            if is_official:
                rec["recommendation"] = ("ACCEPT_OFFICIAL (exact %d; role=%s)"
                                         % (exp, role))
            else:
                rec["recommendation"] = ("ACCEPT_FALLBACK (exact %d; role=%s; "
                                         "ETF holdings, membership unverified)"
                                         % (exp, role))
        else:
            rec["recommendation"] = (
                "REVIEW_NEEDED (%d != %d%s)"
                % (n, exp, "; dup tickers" if ins["duplicate_tickers"]
                   else "; ETF samples index" if role.endswith("fallback")
                   else ""))
        attempts.append(rec)
        if best is None or (n == exp and not ins["duplicate_tickers"]):
            best = rec
    # --- product-page link extraction (preferred over guessed endpoints) ---
    for idx, (role, page_url, note) in enumerate(
            vmeta.get("product_pages", [])):
        ex = extract_and_download(page_url, outdir, venue, idx=idx)
        rec = {"role": role, "url": ex.get("extracted_holdings_url") or
               page_url, "note": "via product page: %s" % note,
               "via": "product_page_extraction",
               "product_page_url": page_url,
               "page_http_status": ex.get("page_http_status"),
               "extracted_holdings_url": ex.get("extracted_holdings_url"),
               "holdings_http_status": ex.get("holdings_http_status"),
               "extract_status": ex.get("status"),
               "http_status": ex.get("holdings_http_status"),
               "saved": False, "sha256": ex.get("sha256"),
               "bytes": ex.get("bytes", 0), "kind": None,
               "inspection": None, "recommendation": None}
        if ex.get("status") != "OK" or not ex.get("saved_file"):
            # map the distinct extractor status to a recommendation
            rec["recommendation"] = {
                "PAGE_UNREACHABLE": "PAGE_UNREACHABLE",
                "NO_HOLDINGS_LINK": "NO_HOLDINGS_LINK",
                "HOLDINGS_LINK_UNREACHABLE": "HOLDINGS_LINK_UNREACHABLE",
                "HOLDINGS_NOT_A_FILE": "HOLDINGS_NOT_A_FILE",
            }.get(ex.get("status"), "EXTRACT_FAILED")
            attempts.append(rec)
            continue
        fpath = ex["saved_file"]
        data = Path(fpath).read_bytes()
        kind = "pdf" if fpath.endswith(".pdf") else "csv"
        rec["saved"] = True
        rec["file"] = fpath
        rec["kind"] = kind
        if kind == "pdf":
            rec["recommendation"] = "MANUAL_REVIEW (pdf not auto-parsed)"
            attempts.append(rec)
            continue
        ins = inspect_rows(_read_rows(data, kind), vmeta)
        rec["inspection"] = ins
        n = len(ins["included"])
        exp = vmeta["expected"]
        if n == exp and not ins["duplicate_tickers"]:
            rec["recommendation"] = ("ACCEPT_FALLBACK (exact %d; role=%s; ETF "
                                     "holdings via product page, membership "
                                     "unverified)" % (exp, role))
        else:
            rec["recommendation"] = (
                "REVIEW_NEEDED (%d != %d%s)"
                % (n, exp, "; dup tickers" if ins["duplicate_tickers"]
                   else "; ETF samples index"))
        attempts.append(rec)
        if best is None or (n == exp and not ins["duplicate_tickers"]):
            best = rec
    # venue-level verdict
    verdict = "BLOCKED"
    for rec in attempts:
        r = rec["recommendation"] or ""
        if r.startswith("ACCEPT_OFFICIAL"):
            verdict = "ACCEPT_OFFICIAL"
            break
    if verdict == "BLOCKED":
        for rec in attempts:
            if (rec["recommendation"] or "").startswith("ACCEPT_FALLBACK"):
                verdict = "ACCEPT_FALLBACK"
                break
    if verdict == "BLOCKED":
        if any(r["saved"] and r["inspection"] for r in attempts):
            verdict = "REVIEW_NEEDED"
    return {"venue": venue, "meta": {k: vmeta[k] for k in
            ("index", "exchange", "suffix", "expected")},
            "attempts": attempts, "verdict": verdict}
