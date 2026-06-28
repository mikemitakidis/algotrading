#!/usr/bin/env python3
"""M21.U4 EU source direct-downloader (read-only acquisition).

Attempts MACHINE downloads of EU index constituent / ETF-holdings files from
documented endpoints, in priority order. Saves only real CSV/XLSX/PDF bytes
(rejects HTML/JS shells). Writes ONLY under /tmp/m21u4_sources/. Records the
download URL in a sidecar <file>.url and labels the source role
(official_index vs reputable_etf_fallback) so policy is never silently
downgraded.

Usage:
  /opt/algo-trader/venv/bin/python3 /tmp/eu_download.py <venue>
  venue in: dax  (more added per batch)

This does NOT inspect membership; run eu_source_inspect.py afterwards on the
saved file. No repo changes, no commit.
"""
import datetime
import hashlib
import sys
import urllib.request
from pathlib import Path

OUTDIR = Path("/tmp/m21u4_sources")

# Per-venue documented endpoints, in priority order. Each entry:
#   (role, url)
# role: "official_index" (index owner / primary exchange) or
#       "reputable_etf_fallback" (large physically-replicating ETF holdings).
# iShares product-page holdings CSVs use the AjaxData fileType=csv pattern; the
# productPageNumber / fileName are the stable bits. These are the documented
# machine-download endpoints (not guessed dated paths).
ENDPOINTS = {
    "dax": [
        # iShares Core DAX UCITS ETF (DE) holdings CSV (reputable ETF fallback)
        ("reputable_etf_fallback",
         "https://www.ishares.com/de/privatanleger/de/produkte/251464/"
         "ishares-dax-ucits-etf-de-fund/1478358465952.ajax"
         "?fileType=csv&fileName=DAXEX_holdings&dataType=fund"),
        # iShares Core DAX UCITS ETF (UK/EN locale variant)
        ("reputable_etf_fallback",
         "https://www.ishares.com/uk/individual/en/products/251464/"
         "ishares-dax-ucits-etf-de-fund/1478358465952.ajax"
         "?fileType=csv&fileName=DAXEX_holdings&dataType=fund"),
        # Xtrackers DAX UCITS ETF holdings (reputable ETF fallback)
        ("reputable_etf_fallback",
         "https://etf.dws.com/en-gb/IE00BXXSC512-xtrackers-dax-ucits-etf-1c/"
         "?download=constituents"),
        # Amundi/Lyxor DAX UCITS ETF holdings (reputable ETF fallback)
        ("reputable_etf_fallback",
         "https://www.amundietf.de/privatkunden/product/download/holdings"
         "?isin=LU0274211480"),
    ],
}


def _try(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except Exception as e:
        return None, ("ERR:%s" % e).encode()


def _classify(data):
    if data[:5] == b"%PDF-":
        return "pdf"
    head = data[:300].lower()
    if b"<html" in head or b"<!doctype" in head:
        return None
    # CSV-ish: has ISIN-like token or a delimiter-heavy first lines
    if b"isin" in head or data[:2000].count(b";") > 5 \
            or data[:2000].count(b",") > 5:
        return "csv"
    return None


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ENDPOINTS:
        print("usage: eu_download.py <venue>  (venues: %s)"
              % ",".join(ENDPOINTS))
        sys.exit(2)
    venue = sys.argv[1]
    OUTDIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ")
    saved = []
    for idx, (role, url) in enumerate(ENDPOINTS[venue]):
        print("=== trying [%s]: %s" % (role, url))
        status, data = _try(url)
        print("http_status=%s bytes=%d" % (status, len(data)))
        if status != 200 or data.startswith(b"ERR:"):
            print("  not_usable (status/err): %s" % data[:120])
            continue
        kind = _classify(data)
        if not kind:
            print("  not_usable (html/js/unknown)")
            continue
        out = OUTDIR / ("%s_src%d_%s.%s" % (venue, idx, ts, kind))
        out.write_bytes(data)
        out.with_suffix(out.suffix + ".url").write_text(url, encoding="utf-8")
        print("SAVED file=%s role=%s bytes=%d sha256=%s"
              % (out, role, len(data), hashlib.sha256(data).hexdigest()))
        saved.append(str(out))
    if saved:
        print("--- saved files (inspect each) ---")
        for s in saved:
            print(s)
    else:
        print("DIRECT_DOWNLOAD_FAILED: no usable file from documented "
              "endpoints for %s. Likely VPS egress allow-list blocks the host, "
              "or endpoints are dynamic. Manual upload is last-resort." % venue)


if __name__ == "__main__":
    main()
