"""
bot/focus.py
Curated focus list for V1 shadow mode.

Skips Tier A dynamic ranking — uses fixed 100 liquid large-caps.
First scan: ~20 min. After cache warms: ~2 min per cycle.
Tier A re-enabled in V2.
"""

FOCUS_SYMBOLS = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "AMD", "ORCL",
    # Large-cap tech / software
    "CRM", "ADBE", "QCOM", "TXN", "INTC", "AMAT", "MU", "LRCX", "KLAC", "SNPS",
    "CDNS", "NOW", "INTU", "PANW", "CRWD", "NET", "DDOG", "ZS", "OKTA", "SNOW",
    # Financials
    "JPM", "BAC", "GS", "MS", "WFC", "C", "BLK", "SCHW", "AXP", "V", "MA", "PYPL",
    # Healthcare
    "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "AMGN", "GILD", "BMY", "ISRG",
    # Consumer
    "WMT", "COST", "TGT", "HD", "LOW", "MCD", "SBUX", "NKE", "PG", "NFLX",
    # Energy
    "XOM", "CVX", "COP", "EOG", "SLB",
    # Industrials
    "CAT", "DE", "HON", "GE", "RTX", "BA", "UPS", "FDX",
    # ETFs
    "SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "XLI",
    # High-vol growth
    "DIS", "CMCSA", "UBER", "COIN", "PLTR", "SOFI",
]

# Deduplicate
seen = set()
FOCUS_SYMBOLS = [s for s in FOCUS_SYMBOLS if s not in seen and not seen.add(s)]
