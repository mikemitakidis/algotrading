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
            ("reputable_etf_fallback",
             "https://www.ishares.com/ch/individual/en/products/291893/"
             "ishares-smi-ch-chf-acc/1495092304805.ajax"
             "?fileType=csv&fileName=CSSMI_holdings&dataType=fund",
             "iShares SMI (CH) holdings CSV (corrected product id)"),
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
            ("reputable_etf_fallback",
             "https://www.ishares.com/nl/particuliere-belegger/nl/producten/"
             "251779/ishares-aex-ucits-etf/1478358465952.ajax"
             "?fileType=csv&fileName=IAEX_holdings&dataType=fund",
             "iShares AEX UCITS ETF holdings CSV"),
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
            ("reputable_etf_fallback",
             "https://www.amundietf.fr/fr/particuliers/products/equity/"
             "amundi-cac-40-ucits-etf-dist/fr0007052782?download=holdings",
             "Amundi CAC 40 UCITS ETF holdings"),
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
            ("reputable_etf_fallback",
             "https://www.ishares.com/es/inversor-particular/es/productos/"
             "251773/ishares-ibex-35-ucits-etf/1478358465952.ajax"
             "?fileType=csv&fileName=IBEX_holdings&dataType=fund",
             "iShares IBEX 35 UCITS ETF holdings CSV"),
        ],
    },
}
