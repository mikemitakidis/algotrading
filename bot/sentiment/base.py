"""
bot/sentiment/base.py
Abstract SentimentProvider interface for Milestone 8.

All sentiment providers must subclass SentimentProvider and implement
get_sentiment(). The scanner consumes a SentimentResult without knowing
which provider produced it.

SentimentResult fields:
  score   : float | None   -1.0 (very bearish) to +1.0 (very bullish). None = unavailable.
  label   : str             'bullish' | 'bearish' | 'neutral' | 'unavailable'
  source  : str             provider name / data source description
  status  : str             'ok' | 'unavailable' | 'error' | 'disabled'
  raw     : dict            provider-specific detail (for logging/debug only)
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SentimentResult:
    score:  Optional[float]  # -1.0 … +1.0; None if unavailable
    label:  str              # 'bullish' | 'bearish' | 'neutral' | 'unavailable'
    source: str              # e.g. 'disabled' | 'newsapi' | 'alpaca_news'
    status: str              # 'ok' | 'unavailable' | 'error' | 'disabled'
    raw:    dict = field(default_factory=dict)

    @classmethod
    def unavailable(cls, source: str = 'unknown', reason: str = '') -> 'SentimentResult':
        return cls(score=None, label='unavailable', source=source,
                   status='unavailable', raw={'reason': reason})

    @classmethod
    def disabled(cls) -> 'SentimentResult':
        return cls(score=None, label='unavailable', source='disabled',
                   status='disabled', raw={})


class SentimentProvider(ABC):
    """Abstract base class for all sentiment providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short provider name shown in logs and DB."""

    @abstractmethod
    def get_sentiment(self, symbol: str) -> SentimentResult:
        """
        Fetch sentiment for one symbol.
        Must NEVER raise — return SentimentResult.unavailable() on any error.
        """
