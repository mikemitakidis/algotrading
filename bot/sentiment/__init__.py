"""
bot/sentiment/__init__.py
Sentiment factory and direction-aware filter logic — Milestone 8.

─── CONFIGURATION (.env) ────────────────────────────────────────────────────
SENTIMENT_MODE      off | confirm | block | ignore   (default: off)
SENTIMENT_PROVIDER  auto | alphavantage | yfinance_news | disabled
                    auto = use alphavantage if ALPHAVANTAGE_KEY set, else yfinance_news
ALPHAVANTAGE_KEY    free API key from alphavantage.co  (optional)
SENTIMENT_THRESHOLD positive float, default 0.15
                    used as the boundary between neutral and bullish/bearish

─── MODES ───────────────────────────────────────────────────────────────────
off      Sentiment not called. All signals pass. Zero overhead.
confirm  Signal blocked if sentiment CONTRADICTS direction (see table below).
block    Same as confirm — semantic alias kept for future stricter logic.
ignore   Sentiment fetched and logged, NEVER filters signals.

─── EXACT FILTER LOGIC (confirm / block modes) ───────────────────────────────
Direction  Score              Action
---------  -----------------  --------
long       >= -threshold      EMIT   (neutral or bullish — ok to go long)
long       < -threshold       BLOCK  (clearly bearish news contradicts long)
short      <= +threshold      EMIT   (neutral or bearish — ok to go short)
short      > +threshold       BLOCK  (clearly bullish news contradicts short)

Threshold default = 0.15. Set SENTIMENT_THRESHOLD in .env to adjust.

─── FAIL-OPEN GUARANTEE ──────────────────────────────────────────────────────
If provider raises OR returns status unavailable/error → signal always emitted.
The bot is never blocked by a broken sentiment provider.

─── SCORE SCALE ─────────────────────────────────────────────────────────────
-1.0  very bearish
-0.15 boundary (bear/neutral)
 0.0  neutral
+0.15 boundary (neutral/bull)
+1.0  very bullish
"""

import logging
import os
from bot.sentiment.base import SentimentProvider, SentimentResult

log = logging.getLogger(__name__)

VALID_MODES = ('off', 'confirm', 'block', 'ignore')


def get_sentiment_mode() -> str:
    mode = os.getenv('SENTIMENT_MODE', 'off').lower().strip()
    return mode if mode in VALID_MODES else 'off'


def get_sentiment_threshold() -> float:
    try:
        return float(os.getenv('SENTIMENT_THRESHOLD', '0.15'))
    except ValueError:
        return 0.15


def get_sentiment_provider() -> SentimentProvider:
    """
    Return the configured provider.
    Selection priority:
      1. SENTIMENT_PROVIDER=disabled → DisabledProvider
      2. SENTIMENT_MODE=off          → DisabledProvider (no-op)
      3. SENTIMENT_PROVIDER=alphavantage OR (auto AND ALPHAVANTAGE_KEY set)
                                     → AlphaVantageNewsProvider
      4. SENTIMENT_PROVIDER=yfinance_news OR auto (no key)
                                     → YFinanceNewsProvider
    """
    mode = get_sentiment_mode()
    if mode == 'off':
        from bot.sentiment.disabled_provider import DisabledProvider
        return DisabledProvider()

    prov_name = os.getenv('SENTIMENT_PROVIDER', 'auto').lower().strip()
    av_key    = os.getenv('ALPHAVANTAGE_KEY', '').strip()

    if prov_name == 'disabled':
        from bot.sentiment.disabled_provider import DisabledProvider
        return DisabledProvider()

    if prov_name == 'alphavantage' or (prov_name == 'auto' and av_key):
        if not av_key:
            log.warning('[SENT] SENTIMENT_PROVIDER=alphavantage but ALPHAVANTAGE_KEY not set — falling back to yfinance_news')
        else:
            from bot.sentiment.news_provider import AlphaVantageNewsProvider
            return AlphaVantageNewsProvider(av_key)

    # yfinance_news or auto fallback
    from bot.sentiment.news_provider import YFinanceNewsProvider
    return YFinanceNewsProvider()


def apply_sentiment(
    signal: dict,
    result: SentimentResult,
    mode: str,
) -> tuple[dict, bool]:
    """
    Attach sentiment fields to signal and decide whether to emit it.

    Direction-aware: long and short are filtered independently.
    See module docstring for exact logic.

    Returns: (signal_with_sentiment_fields, should_emit: bool)
    """
    enabled = 1 if (mode not in ('off',) and result.status != 'disabled') else 0

    signal['sentiment_enabled'] = enabled
    signal['sentiment_mode']    = mode
    signal['sentiment_score']   = result.score
    signal['sentiment_label']   = result.label
    signal['sentiment_source']  = result.source
    signal['sentiment_status']  = result.status

    # Off or disabled — always emit, no filtering
    if mode == 'off' or result.status == 'disabled':
        return signal, True

    # Provider failed — fail-open
    if result.status in ('unavailable', 'error'):
        log.warning('[SENT] %s: %s — fail-open, signal emitted',
                    signal.get('symbol', '?'), result.status)
        return signal, True

    # ignore — log but never filter
    if mode == 'ignore':
        return signal, True

    # confirm / block — direction-aware filter
    if mode in ('confirm', 'block'):
        score     = result.score
        threshold = get_sentiment_threshold()
        direction = signal.get('direction', 'long')

        if score is None:
            return signal, True   # no score → fail-open

        if direction == 'long':
            if score < -threshold:
                log.info('[SENT] %s LONG BLOCKED: bearish sentiment score=%.3f < -%.2f',
                         signal.get('symbol', '?'), score, threshold)
                return signal, False
        else:  # short
            if score > threshold:
                log.info('[SENT] %s SHORT BLOCKED: bullish sentiment score=%.3f > +%.2f',
                         signal.get('symbol', '?'), score, threshold)
                return signal, False

        return signal, True

    return signal, True   # unknown mode — fail-open


__all__ = [
    'SentimentResult', 'SentimentProvider',
    'get_sentiment_mode', 'get_sentiment_provider',
    'get_sentiment_threshold', 'apply_sentiment',
]
