"""
bot/data.py
Fetches OHLCV bars using yfinance.
Single responsibility: return {symbol: DataFrame} dict.
No indicators. No scoring. No side effects beyond logging.
"""
import logging
import time
import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

# Minimum bars required for MACD (26) + buffer
MIN_BARS = 30


def fetch_bars(symbols: list, period: str, interval: str) -> dict:
    """
    Fetch OHLCV bars for a list of symbols.

    Args:
        symbols: list of ticker strings
        period:  yfinance period string e.g. '3mo', '5d', '15d'
        interval: yfinance interval string e.g. '1d', '1h', '15m'

    Returns:
        dict of {symbol: DataFrame} with lowercase column names.
        Only includes symbols with >= MIN_BARS rows.
        Empty dict if all fetches fail.
    """
    result = {}
    batch_size = 100

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(symbols) + batch_size - 1) // batch_size

        try:
            raw = yf.download(
                tickers=batch,
                period=period,
                interval=interval,
                group_by='ticker',
                auto_adjust=True,
                progress=False,
                threads=True,
            )

            if raw is None or raw.empty:
                log.debug(f"[DATA] Batch {batch_num}/{total_batches}: empty response")
                time.sleep(0.2)
                continue

            if isinstance(raw.columns, pd.MultiIndex):
                # Multi-symbol response
                got = 0
                for sym in batch:
                    try:
                        df = raw[sym].dropna()
                        df.columns = [c.lower() for c in df.columns]
                        if len(df) >= MIN_BARS:
                            result[sym] = df
                            got += 1
                    except (KeyError, AttributeError):
                        pass
                log.debug(f"[DATA] Batch {batch_num}/{total_batches} ({interval}): {got}/{len(batch)} symbols")
            else:
                # Single-symbol response
                if len(batch) == 1:
                    raw.columns = [c.lower() for c in raw.columns]
                    df = raw.dropna()
                    if len(df) >= MIN_BARS:
                        result[batch[0]] = df

        except Exception as e:
            log.warning(f"[DATA] Batch {batch_num}/{total_batches} ({interval}) error: {str(e)[:120]}")

        time.sleep(0.2)

    return result


def resample_to_4h(df: pd.DataFrame) -> pd.DataFrame:
    """Resample a 1H DataFrame to 4H bars."""
    return df.resample('4h').agg({
        'open':   'first',
        'high':   'max',
        'low':    'min',
        'close':  'last',
        'volume': 'sum',
    }).dropna()
