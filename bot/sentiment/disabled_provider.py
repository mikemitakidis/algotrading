"""
bot/sentiment/disabled_provider.py
Safe no-op sentiment provider — the default when sentiment is off.

Returns SentimentResult.disabled() for every symbol.
Never fetches data. Never raises. Zero latency.
"""
from bot.sentiment.base import SentimentProvider, SentimentResult


class DisabledProvider(SentimentProvider):
    """Sentiment is turned off. All results are 'disabled'."""

    @property
    def name(self) -> str:
        return 'disabled'

    def get_sentiment(self, symbol: str) -> SentimentResult:
        return SentimentResult.disabled()
