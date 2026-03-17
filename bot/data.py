"""
bot/data.py
Fetches OHLCV bars using yfinance.
Includes retry logic with exponential backoff for rate limit errors.
"""
import logging
import time
import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

MIN_BARS   = 30
BATCH_SIZE = 100
MAX_RETRIES = 3
RETRY_WAIT  = [5, 15, 30]  # seconds between retries


def fetch_bars(symbols: list, period: str, interval: str) -> dict:
    """
    Fetch OHLCV bars for a list of symbols using yfinance.
    Returns {symbol: DataFrame} with lowercase columns.
    Only includes symbols with >= MIN_BARS rows.
    Retries on rate limit errors with backoff.
    """
    result = {}
    total_batches = (len(symbols) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(symbols), BATCH_SIZE):
        batch     = symbols[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1

        for attempt in range(MAX_RETRIES):
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
                    log.debug('[DATA] Batch %d/%d: empty', batch_num, total_batches)
                    break

                if isinstance(raw.columns, pd.MultiIndex):
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
                    log.debug('[DATA] Batch %d/%d (%s): %d/%d symbols',
                              batch_num, total_batches, interval, got, len(batch))
                else:
                    if len(batch) == 1:
                        raw.columns = [c.lower() for c in raw.columns]
                        df = raw.dropna()
                        if len(df) >= MIN_BARS:
                            result[batch[0]] = df
                break  # success — no retry needed

            except Exception as e:
                err_str = str(e)
                if 'Rate' in err_str or 'Too Many' in err_str or 'rate' in err_str.lower():
                    wait = RETRY_WAIT[min(attempt, len(RETRY_WAIT) - 1)]
                    log.warning('[DATA] Batch %d/%d: rate limited (attempt %d/%d). '
                                'Waiting %ds...', batch_num, total_batches,
                                attempt + 1, MAX_RETRIES, wait)
                    time.sleep(wait)
                else:
                    log.warning('[DATA] Batch %d/%d (%s): %s',
                                batch_num, total_batches, interval, err_str[:100])
                    break  # non-rate-limit error — no point retrying

        time.sleep(0.3)  # polite delay between batches

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
