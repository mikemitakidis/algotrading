"""Per-venue source registry for the M21.U4 Europe audit.

Each venue declares: expected constituent count, exchange/suffix, the filter
predicate for "is this a constituent equity line", and an ordered list of
candidate source endpoints with an explicit source_role label so policy is
never silently downgraded.

source_role values:
  official_index     - index owner (STOXX, Euronext, SIX, BME, Deutsche Boerse)
  official_exchange  - primary exchange listing export
  reputable_etf_fallback - large physically-replicating ETF holdings (sample)

NOTE: official_index / official_exchange endpoints for these venues are
typically dynamic / login-gated and not machine-downloadable from a server.
They are listed so the report records that they were ATTEMPTED and why they
are unusable, not because we expect them to succeed unattended.
"""

VENUES = {
    "dax": {
        "index": "DAX", "exchange": "XETRA", "suffix": ".DE",
        "expected": 40,
        "equity_filter": {
            "asset_class": ("aktien", "equity"),
            "exchange_substr": ("xetra",),
            "currency": ("EUR",),
        },
        "endpoints": [
            ("official_index",
             "https://www.dax-indices.com/index-details/?indexId=DAX",
             "STOXX/DAX composition page (dynamic; not a file)"),
            ("reputable_etf_fallback",
             "https://www.ishares.com/de/privatanleger/de/produkte/251464/"
             "ishares-dax-ucits-etf-de-fund/1478358465952.ajax"
             "?fileType=csv&fileName=DAXEX_holdings&dataType=fund",
             "iShares Core DAX UCITS ETF (DE) holdings CSV"),
        ],
    },
    "smi": {
        "index": "SMI", "exchange": "SIX", "suffix": ".SW",
        "expected": 20,
        "equity_filter": {
            "asset_class": ("aktien", "equity"),
            "exchange_substr": ("six", "swiss"),
            "currency": ("CHF",),
        },
        "endpoints": [
            ("official_index",
             "https://www.six-group.com/en/products-services/the-swiss-stock-"
             "exchange/market-data/indices/equity-indices/smi.html",
             "SIX SMI index page (dynamic; not a file)"),
            # UBS ETF (CH) SMI - issuer holdings CSV (full replication -> 20)
            ("reputable_etf_fallback",
             "https://www.ubs.com/etf-tools/api/etf/holdings/csv?isin="
             "CH0017142719",
             "UBS ETF (CH) SMI CHF A-dis holdings CSV (candidate id)"),
            # iShares SMI (CH) - product 270048, retry default AjaxData no.
            ("reputable_etf_fallback",
             "https://www.ishares.com/ch/individual/en/products/270048/"
             "fund/1495092304805.ajax"
             "?fileType=csv&fileName=CSSMI_holdings&dataType=fund",
             "iShares SMI (CH) holdings CSV (product 270048 retry)"),
            # iShares Core SPI / SMI alt locale (DE retail)
            ("reputable_etf_fallback",
             "https://www.ishares.com/de/privatanleger/de/produkte/270048/"
             "fund/1478358465952.ajax"
             "?fileType=csv&fileName=CSSMI_holdings&dataType=fund",
             "iShares SMI (DE locale) holdings CSV (candidate)"),
        ],
        # Product LANDING pages for link-extraction (the extractor scrapes the
        # real holdings-CSV ajax link from the page HTML, no guessed numbers).
        # SMI target = the SMI 20 blue-chip index, NOT SPI / broad Swiss market.
        "product_pages": [
            ("reputable_etf_fallback",
             "https://www.ishares.com/ch/individual/en/products/251882/"
             "ishares-smi-ch",
             "iShares SMI(R) (CH) product page (SMI 20)"),
        ],
    },
    "aex": {
        "index": "AEX", "exchange": "AEX", "suffix": ".AS",
        "expected": 25,
        "equity_filter": {
            "asset_class": ("aktien", "equity"),
            "exchange_substr": ("amsterdam", "euronext"),
            "currency": ("EUR",),
        },
        "endpoints": [
            ("official_index",
             "https://live.euronext.com/en/product/indices/NL0000000107-XAMS",
             "Euronext AEX composition page (dynamic; not a file)"),
            # iShares AEX UCITS ETF - product 251779; correct AjaxData number
            # is the per-fund one, try the DAX-style working pattern + NL.
            ("reputable_etf_fallback",
             "https://www.ishares.com/nl/particuliere-belegger/nl/producten/"
             "251779/fund/1478358465952.ajax"
             "?fileType=csv&fileName=IAEX_holdings&dataType=fund",
             "iShares AEX UCITS ETF holdings CSV (product 251779, NL)"),
            ("reputable_etf_fallback",
             "https://www.ishares.com/uk/individual/en/products/251779/"
             "fund/1478358465952.ajax"
             "?fileType=csv&fileName=IAEX_holdings&dataType=fund",
             "iShares AEX UCITS ETF holdings CSV (UK locale candidate)"),
        ],
        "product_pages": [
            ("reputable_etf_fallback",
             "https://www.ishares.com/nl/particuliere-belegger/nl/producten/"
             "251779/ishares-aex-ucits-etf",
             "iShares AEX UCITS ETF product page (NL)"),
            ("reputable_etf_fallback",
             "https://www.ishares.com/uk/individual/en/products/251779/"
             "ishares-aex-ucits-etf",
             "iShares AEX UCITS ETF product page (UK)"),
        ],
    },
    "cac": {
        "index": "CAC", "exchange": "EPA", "suffix": ".PA",
        "expected": 40,
        "equity_filter": {
            "asset_class": ("aktien", "equity"),
            "exchange_substr": ("paris", "euronext"),
            "currency": ("EUR",),
        },
        "endpoints": [
            ("official_index",
             "https://live.euronext.com/en/product/indices/FR0003500008-XPAR",
             "Euronext CAC 40 composition page (dynamic; not a file)"),
            # iShares CAC 40 UCITS ETF - product 251786 (FR/Amundi-Lyxor also
            # candidates). Try iShares product-page CSV first.
            ("reputable_etf_fallback",
             "https://www.ishares.com/fr/particuliers/fr/produits/251786/"
             "fund/1478358465952.ajax"
             "?fileType=csv&fileName=CAC_holdings&dataType=fund",
             "iShares CAC 40 UCITS ETF holdings CSV (product 251786, FR)"),
            # Amundi CAC 40 ETF holdings (issuer download API candidate)
            ("reputable_etf_fallback",
             "https://www.amundietf.fr/fr/professionnels/api/funds/holdings/"
             "FR0007052782/csv",
             "Amundi CAC 40 UCITS ETF holdings CSV (candidate id)"),
        ],
        "product_pages": [
            ("reputable_etf_fallback",
             "https://www.ishares.com/fr/particuliers/fr/produits/251786/"
             "ishares-cac-40-ucits-etf",
             "iShares CAC 40 UCITS ETF product page (FR)"),
            ("reputable_etf_fallback",
             "https://www.ishares.com/uk/individual/en/products/251786/"
             "ishares-cac-40-ucits-etf",
             "iShares CAC 40 UCITS ETF product page (UK)"),
        ],
    },
    "ibex": {
        "index": "IBEX", "exchange": "BME", "suffix": ".MC",
        "expected": 35,
        "equity_filter": {
            "asset_class": ("aktien", "equity"),
            "exchange_substr": ("madrid", "bolsa", "bme"),
            "currency": ("EUR",),
        },
        "endpoints": [
            ("official_index",
             "https://www.bolsasymercados.es/bme-exchange/en/Indices/Ibex/"
             "Ibex-35",
             "BME IBEX 35 composition page (dynamic; not a file)"),
            # iShares IBEX 35 UCITS ETF - product 251773; ES retail locale
            ("reputable_etf_fallback",
             "https://www.ishares.com/es/inversor-particular/es/productos/"
             "251773/fund/1478358465952.ajax"
             "?fileType=csv&fileName=IBEX_holdings&dataType=fund",
             "iShares IBEX 35 UCITS ETF holdings CSV (product 251773, ES)"),
            ("reputable_etf_fallback",
             "https://www.ishares.com/uk/individual/en/products/251773/"
             "fund/1478358465952.ajax"
             "?fileType=csv&fileName=IBEX_holdings&dataType=fund",
             "iShares IBEX 35 UCITS ETF holdings CSV (UK locale candidate)"),
        ],
        "product_pages": [
            ("reputable_etf_fallback",
             "https://www.ishares.com/es/inversor-particular/es/productos/"
             "251773/ishares-ibex-35-ucits-etf",
             "iShares IBEX 35 UCITS ETF product page (ES)"),
            ("reputable_etf_fallback",
             "https://www.ishares.com/uk/individual/en/products/251773/"
             "ishares-ibex-35-ucits-etf",
             "iShares IBEX 35 UCITS ETF product page (UK)"),
        ],
    },
}
