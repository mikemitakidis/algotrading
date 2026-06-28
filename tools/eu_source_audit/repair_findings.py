"""M21.U4 Europe source-repair findings (structured data, no network).

Each venue records the current audit result, why the current source failed, the
candidate better sources (official index owner / exchange export / ETF CSV /
manual official file) with a reachability assessment, whether an exact count is
obtainable, and a final classification.

Provenance of these findings:
  * audit_result / fallback counts: from the merged audit tool run on the VPS
    (the authoritative environment that reaches issuer endpoints).
  * endpoint reachability notes: from observed VPS probes (iShares ajax CSV
    endpoints returned 200 where the product id was correct; index-owner pages
    returned dynamic HTML; some issuer endpoints 404/403).
  * NO new live probing was performed to author this file (the authoring
    sandbox egress allow-list blocks issuer/index hosts). Re-verification of any
    endpoint must be done via the audit Action/VPS, not assumed here.

Classification vocabulary:
  ACCEPT_OFFICIAL            official index/exchange source, exact count, machine-usable
  ACCEPT_FALLBACK_EXACT      reputable ETF holdings, exact official count, membership unverified
  FALLBACK_INCOMPLETE        reputable ETF holdings reachable but sample != official count
  BLOCKED_NEEDS_MANUAL_SOURCE no machine-usable source; an official file must be supplied once
"""

FINDINGS = {
    "dax": {
        "index": "DAX", "exchange": "XETRA", "suffix": ".DE", "expected": 40,
        "audit_result": "REVIEW_NEEDED",
        "current_source": "iShares Core DAX UCITS ETF (DE) holdings CSV "
                          "(reputable_etf_fallback)",
        "why_failed": "ETF holdings sample the index: 38 equity Xetra/EUR "
                      "lines vs official 40. AIRBUS excluded (held on Boerse "
                      "Berlin line), QIAGEN excluded (Deutsche Boerse AG line); "
                      "plus cash/FX/futures rows. So the ETF is not a faithful "
                      "40-name membership snapshot.",
        "better_sources": [
            ("official_index",
             "STOXX / dax-indices.com DAX composition export",
             "Authoritative 40-name membership. Page is dynamic/JS; no stable "
             "machine-downloadable CSV/XLSX endpoint observed. Likely needs a "
             "one-time manual export or login."),
            ("official_exchange",
             "Deutsche Boerse / Xetra DAX factsheet (PDF)",
             "Authoritative; PDF is downloadable but layout-variable and was "
             "not auto-parsed by the audit (PDFs flagged MANUAL_REVIEW)."),
            ("reputable_etf_fallback",
             "iShares Core DAX ETF holdings CSV",
             "Machine-reachable from VPS (HTTP 200), but 38/40 -> incomplete."),
        ],
        "exact_count_possible": "Only from an official index/exchange file "
                                "(STOXX composition or Xetra factsheet). No "
                                "reachable ETF gives exactly 40.",
        "fallback_exact_or_incomplete": "INCOMPLETE (38/40)",
        "classification": "FALLBACK_INCOMPLETE",
        "manual_file_needed": "Official DAX 40 composition (STOXX/dax-indices "
                              "export CSV/XLSX) or the Xetra DAX factsheet PDF, "
                              "uploaded once to /tmp/m21u4_sources/.",
    },
    "smi": {
        "index": "SMI", "exchange": "SIX", "suffix": ".SW", "expected": 20,
        "audit_result": "BLOCKED",
        "current_source": "iShares SMI ETF holdings CSV "
                          "(reputable_etf_fallback)",
        "why_failed": "First endpoint id returned a global multi-asset fund "
                      "(429 rows, only 1 SIX/CHF equity) -> wrong product. "
                      "Corrected SMI product-id endpoint was UNREACHABLE from "
                      "the VPS audit run (404/▒). So no SMI holdings file was "
                      "obtained.",
        "better_sources": [
            ("official_index",
             "SIX Swiss Exchange SMI index page / factsheet",
             "Authoritative 20-name membership; page dynamic, no stable CSV "
             "endpoint observed."),
            ("reputable_etf_fallback",
             "iShares SMI (CH) / UBS ETF SMI / Amundi SMI holdings CSV",
             "SMI is fully replicated (20 names), so a CORRECT ETF endpoint "
             "should yield exactly 20. The challenge is the correct, reachable "
             "holdings URL; the ids tried so far 404'd. Worth one more "
             "endpoint-id correction via the audit tool."),
        ],
        "exact_count_possible": "Plausibly YES via a correct iShares/UBS SMI "
                                "holdings CSV (full replication -> 20), once "
                                "the right reachable endpoint id is found; "
                                "otherwise via SIX official file.",
        "fallback_exact_or_incomplete": "UNKNOWN (no holdings file obtained "
                                        "yet; corrected endpoint unreachable)",
        "classification": "BLOCKED_NEEDS_MANUAL_SOURCE",
        "manual_file_needed": "Either a corrected reachable iShares/UBS SMI "
                              "holdings CSV (preferred, likely exact 20), or "
                              "the SIX SMI official constituents file, uploaded "
                              "once.",
    },
    "aex": {
        "index": "AEX", "exchange": "AEX", "suffix": ".AS", "expected": 25,
        "audit_result": "BLOCKED",
        "current_source": "iShares AEX UCITS ETF holdings CSV "
                          "(reputable_etf_fallback)",
        "why_failed": "Endpoint UNREACHABLE in the audit run (no holdings file "
                      "obtained).",
        "better_sources": [
            ("official_index",
             "Euronext AEX composition (live.euronext.com)",
             "Authoritative 25-name membership; Euronext pages are dynamic and "
             "key on ISIN, no stable CSV endpoint observed."),
            ("reputable_etf_fallback",
             "iShares AEX UCITS ETF holdings CSV",
             "AEX (25 names) is small/replicated; a correct reachable iShares "
             "AEX endpoint should yield ~25. Endpoint id needs correction."),
        ],
        "exact_count_possible": "Plausibly YES via a correct iShares AEX "
                                "holdings CSV; otherwise via Euronext official "
                                "file.",
        "fallback_exact_or_incomplete": "UNKNOWN (no holdings file obtained)",
        "classification": "BLOCKED_NEEDS_MANUAL_SOURCE",
        "manual_file_needed": "Corrected reachable iShares AEX holdings CSV, or "
                              "the Euronext AEX official composition export.",
    },
    "cac": {
        "index": "CAC", "exchange": "EPA", "suffix": ".PA", "expected": 40,
        "audit_result": "BLOCKED",
        "current_source": "Amundi CAC 40 UCITS ETF holdings "
                          "(reputable_etf_fallback)",
        "why_failed": "Endpoint UNREACHABLE in the audit run (no holdings file "
                      "obtained).",
        "better_sources": [
            ("official_index",
             "Euronext CAC 40 composition (live.euronext.com)",
             "Authoritative 40-name membership; dynamic page, ISIN-keyed, no "
             "stable CSV endpoint observed. Watch multi-class tickers."),
            ("reputable_etf_fallback",
             "Amundi / Lyxor / iShares CAC 40 ETF holdings CSV",
             "CAC 40 is replicated; a correct reachable ETF endpoint should "
             "yield ~40. Endpoint id/host needs correction (Amundi URL 404'd "
             "pattern in prior DAX probe)."),
        ],
        "exact_count_possible": "Plausibly YES via a correct CAC 40 ETF "
                                "holdings CSV; otherwise via Euronext official "
                                "file.",
        "fallback_exact_or_incomplete": "UNKNOWN (no holdings file obtained)",
        "classification": "BLOCKED_NEEDS_MANUAL_SOURCE",
        "manual_file_needed": "Corrected reachable CAC 40 ETF holdings CSV, or "
                              "the Euronext CAC 40 official composition export.",
    },
    "ibex": {
        "index": "IBEX", "exchange": "BME", "suffix": ".MC", "expected": 35,
        "audit_result": "BLOCKED",
        "current_source": "iShares IBEX 35 UCITS ETF holdings CSV "
                          "(reputable_etf_fallback)",
        "why_failed": "Endpoint UNREACHABLE in the audit run (no holdings file "
                      "obtained).",
        "better_sources": [
            ("official_index",
             "BME / Bolsa de Madrid IBEX 35 composition",
             "Authoritative 35-name membership; dynamic page, no stable CSV "
             "endpoint observed."),
            ("reputable_etf_fallback",
             "iShares IBEX 35 UCITS ETF holdings CSV",
             "IBEX 35 is replicated; a correct reachable iShares IBEX endpoint "
             "should yield ~35. Endpoint id needs correction."),
        ],
        "exact_count_possible": "Plausibly YES via a correct iShares IBEX "
                                "holdings CSV; otherwise via BME official "
                                "file.",
        "fallback_exact_or_incomplete": "UNKNOWN (no holdings file obtained)",
        "classification": "BLOCKED_NEEDS_MANUAL_SOURCE",
        "manual_file_needed": "Corrected reachable iShares IBEX 35 holdings "
                              "CSV, or the BME IBEX 35 official composition.",
    },
}
