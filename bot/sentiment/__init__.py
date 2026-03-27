"""
bot/sentiment/__init__.py
Sentiment factory and mode logic for Milestone 8.

Configuration (all in .env):
  SENTIMENT_MODE=off          — sentiment disabled entirely (default)
  SENTIMENT_MODE=confirm      — signal emitted only if sentiment >= 0 (non-bearish)
  SENTIMENT_MODE=block        — signal blocked if sentiment is bearish (score < -THRESHOLD)
  SENTIMENT_MODE=ignore       — sentiment fetched and logged, never filters signals

  SENTIMENT_PROVIDER=disabled — safe no-op (default)
  SENTIMENT_THRESHOLD=0.1     — score threshold used in confirm/block modes

Usage:
  from bot.sentiment import get_sentiment_provider, apply_sentiment, SENTIMENT_MODE
"""
import logging
import os
from bot.sentiment.base import SentimentProvider, SentimentResult

log = logging.getLogger(__name__)

VALID_MODES     = ('off', 'confirm', 'block', 'ignore')
VALID_PROVIDERS = ('disabled',)   # extend as real providers are added

# ── Factory ───────────────────────────────────────────────────────────────────

def get_sentiment_mode() -> str:
    mode = os.getenv('SENTIMENT_MODE', 'off').lower().strip()
    if mode not in VALID_MODES:
        log.warning('[SENT] Unknown SENTIMENT_MODE=%r — defaulting to off', mode)
        return 'off'
    return mode


def get_sentiment_provider() -> SentimentProvider:
    """Return the configured provider. Always falls back to DisabledProvider."""
    provider = os.getenv('SENTIMENT_PROVIDER', 'disabled').lower().strip()
    if provider == 'disabled' or get_sentiment_mode() == 'off':
        from bot.sentiment.disabled_provider import DisabledProvider
        return DisabledProvider()
    # Future: add newsapi, alpaca_news, etc. here
    log.warning('[SENT] Unknown SENTIMENT_PROVIDER=%r — using disabled', provider)
    from bot.sentiment.disabled_provider import DisabledProvider
    return DisabledProvider()


def get_sentiment_threshold() -> float:
    try:
        return float(os.getenv('SENTIMENT_THRESHOLD', '0.1'))
    except ValueError:
        return 0.1


# ── Mode application ──────────────────────────────────────────────────────────

def apply_sentiment(
    signal: dict,
    result: SentimentResult,
    mode: str,
) -> tuple[dict, bool]:
    """
    Attach sentiment fields to signal dict and decide whether to emit it.

    Returns:
        (signal_with_sentiment_fields, should_emit: bool)

    Signal fields added:
        sentiment_enabled : int   1 if mode != off/disabled, else 0
        sentiment_mode    : str   current mode
        sentiment_score   : float | None
        sentiment_label   : str
        sentiment_source  : str
        sentiment_status  : str
    """
    enabled = 1 if (mode not in ('off',) and result.status != 'disabled') else 0

    signal['sentiment_enabled'] = enabled
    signal['sentiment_mode']    = mode
    signal['sentiment_score']   = result.score
    signal['sentiment_label']   = result.label
    signal['sentiment_source']  = result.source
    signal['sentiment_status']  = result.status

    # Decide emit
    if mode == 'off' or result.status == 'disabled':
        return signal, True   # sentiment off — pass all signals

    if result.status in ('unavailable', 'error'):
        # Provider failed — do not block signal, log warning
        log.warning('[SENT] %s: provider %s — passing signal (fail-open)',
                    signal.get('symbol', '?'), result.status)
        return signal, True

    threshold = get_sentiment_threshold()

    if mode == 'ignore':
        return signal, True   # logged but never filters

    if mode == 'confirm':
        # Block if sentiment is clearly bearish
        if result.score is not None and result.score < -threshold:
            log.info('[SENT] %s: BLOCKED (confirm mode, score=%.2f < -%.2f)',
                     signal.get('symbol', '?'), result.score, threshold)
            return signal, False
        return signal, True

    if mode == 'block':
        # Block only if explicitly bearish
        if result.score is not None and result.score < -threshold:
            log.info('[SENT] %s: BLOCKED (block mode, score=%.2f)',
                     signal.get('symbol', '?'), result.score)
            return signal, False
        return signal, True

    return signal, True   # unknown mode — fail open


__all__ = [
    'SentimentResult', 'SentimentProvider',
    'get_sentiment_mode', 'get_sentiment_provider',
    'get_sentiment_threshold', 'apply_sentiment',
]
