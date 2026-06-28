#!/usr/bin/env python3
"""M21.U4 Europe holdings-link extractor (read-only).

Replaces fragile hardcoded `fund/<number>.ajax` guessing. Given an ETF product
PAGE url, fetch the page HTML and extract the genuine holdings-CSV download link
(the per-fund ".ajax?fileType=csv...&dataType=fund" href that iShares embeds in
its product pages), then download THAT file.

No fabrication: every failure mode is reported distinctly. Writes only under the
provided outdir for provenance; no repo writes, no curation.

Status codes returned by extract_and_download():
  PAGE_UNREACHABLE          product page fetch failed / non-200
  NO_HOLDINGS_LINK          page fetched but no holdings-CSV link found
  HOLDINGS_LINK_UNREACHABLE link found but its download failed / non-200
  HOLDINGS_NOT_A_FILE       downloaded bytes are HTML/JS, not a CSV/XLSX
  OK                        a real holdings file was downloaded
"""
import datetime
import hashlib
import re
import urllib.parse
import urllib.request
from pathlib import Path

# Holdings-link patterns inside iShares product pages. The canonical iShares
# download href looks like:
#   /<locale path>/<productId>/fund/<number>.ajax?fileType=csv&fileName=
#   <CODE>_holdings&dataType=fund
# We capture any href containing fileType=csv AND dataType=fund (the holdings
# export), preferring fileName=*holdings*.
_HREF_RE = re.compile(
    r"""(?:href|data-link|data-url)\s*=\s*["']([^"']+?\.ajax\?[^"']*?"""
    r"""fileType=csv[^"']*?dataType=fund[^"']*)["']""",
    re.IGNORECASE)
# Fallback: any href to an .ajax csv export even if attribute differs.
_HREF_RE_LOOSE = re.compile(
    r"""["']([^"']+?\.ajax\?[^"']*?fileType=csv[^"']*)["']""",
    re.IGNORECASE)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ")


def _fetch(url, timeout=30):
    """Return (status, data_bytes, ok)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml,application/xml,*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), True
    except Exception as e:  # noqa: BLE001
        return None, ("ERR:%s" % e).encode(), False


def _is_html(data):
    head = data[:600].lower()
    return b"<html" in head or b"<!doctype" in head or b"<head" in head


def _is_holdings_file(data):
    if data[:5] == b"%PDF-":
        return True
    if _is_html(data):
        return False
    sample = data[:4000]
    if b"isin" in sample.lower():
        return True
    # CSV-ish: delimiter heavy and not html
    return sample.count(b";") > 5 or sample.count(b",") > 5


def extract_links_from_html(html_text, base_url):
    """Return absolute holdings-CSV URLs found in the page HTML, best first.

    Prefers links whose fileName contains 'holdings'. Deduplicates. Pure
    function (no network) so it is unit-testable with synthetic HTML.
    """
    found = []
    for rx in (_HREF_RE, _HREF_RE_LOOSE):
        for m in rx.finditer(html_text):
            href = m.group(1)
            href = href.replace("&amp;", "&")
            absu = urllib.parse.urljoin(base_url, href)
            if absu not in found:
                found.append(absu)
    # prefer fileName=*holdings* exports
    found.sort(key=lambda u: (0 if "holdings" in u.lower() else 1))
    return found


def extract_and_download(product_page_url, outdir, venue, idx=0,
                         timeout=30, _fetch_fn=None):
    """Fetch product page, extract holdings link, download it.

    _fetch_fn is injectable for tests (signature: url -> (status, bytes, ok)).
    Returns a dict with every field requested for the report.
    """
    fetch = _fetch_fn or _fetch
    rec = {
        "product_page_url": product_page_url,
        "page_http_status": None,
        "extracted_holdings_url": None,
        "holdings_http_status": None,
        "saved_file": None,
        "sha256": None,
        "bytes": 0,
        "status": None,
        "candidates": [],
    }
    pstatus, pdata, pok = fetch(product_page_url)
    rec["page_http_status"] = pstatus
    if not pok or pstatus != 200 or _is_holdings_file(pdata) is False and \
            not _is_html(pdata):
        # page not reachable or not a usable HTML page
        if not pok or pstatus != 200:
            rec["status"] = "PAGE_UNREACHABLE"
            return rec
    if not _is_html(pdata):
        # Some product "pages" might directly be the CSV (rare). If so, treat
        # as holdings.
        if _is_holdings_file(pdata):
            return _save(rec, product_page_url, pdata, outdir, venue, idx,
                         direct=True)
        rec["status"] = "PAGE_UNREACHABLE"
        return rec
    links = extract_links_from_html(pdata.decode("utf-8", "replace"),
                                    product_page_url)
    rec["candidates"] = links
    if not links:
        rec["status"] = "NO_HOLDINGS_LINK"
        return rec
    # try candidate links in order until one downloads a real file
    for link in links:
        hstatus, hdata, hok = fetch(link)
        rec["extracted_holdings_url"] = link
        rec["holdings_http_status"] = hstatus
        if not hok or hstatus != 200:
            rec["status"] = "HOLDINGS_LINK_UNREACHABLE"
            continue
        if not _is_holdings_file(hdata):
            rec["status"] = "HOLDINGS_NOT_A_FILE"
            continue
        return _save(rec, link, hdata, outdir, venue, idx)
    return rec  # last failure status retained


def _save(rec, url, data, outdir, venue, idx, direct=False):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    kind = "pdf" if data[:5] == b"%PDF-" else "csv"
    fpath = outdir / ("%s_pp%d_%s.%s" % (venue, idx, _now(), kind))
    fpath.write_bytes(data)
    fpath.with_suffix(fpath.suffix + ".url").write_text(url)
    rec["extracted_holdings_url"] = url if not direct else rec[
        "product_page_url"]
    rec["saved_file"] = str(fpath)
    rec["sha256"] = hashlib.sha256(data).hexdigest()
    rec["bytes"] = len(data)
    rec["status"] = "OK"
    return rec
