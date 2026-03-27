"""
bot/sentiment/news_provider.py
Real news sentiment provider for Milestone 8.

Two implementations in one file:

1. AlphaVantageNewsProvider  (preferred)
   - Uses Alpha Vantage NEWS_SENTIMENT endpoint
   - Free tier: 25 requests/day (enough for signal confirmation)
   - Returns pre-scored per-ticker sentiment from AV's NLP pipeline
   - Requires: ALPHAVANTAGE_KEY in .env

2. YFinanceNewsProvider  (fallback, no API key needed)
   - Fetches recent headlines via yfinance Ticker.news
   - Scores with an embedded financial keyword lexicon (no NLTK required)
   - Less accurate than AV but always available
   - Used automatically when ALPHAVANTAGE_KEY is not set

Selection: AlphaVantageNewsProvider if ALPHAVANTAGE_KEY present, else YFinanceNewsProvider.
The factory in bot/sentiment/__init__.py handles this automatically.

Score scale: -1.0 (very bearish) to +1.0 (very bullish)
  AV: maps their 0–1 bearish/bullish/neutral scores to -1…+1
  YF: compound score from keyword lexicon

Label thresholds (shared):
  score >= +0.15  → 'bullish'
  score <= -0.15  → 'bearish'
  otherwise       → 'neutral'
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from bot.sentiment.base import SentimentProvider, SentimentResult

log = logging.getLogger(__name__)

# ── Shared helpers ────────────────────────────────────────────────────────────

_CACHE_DIR  = Path(__file__).resolve().parent.parent.parent / 'data' / 'sentiment_cache'
_CACHE_TTL  = 3600   # 1 hour — don't re-fetch within same cycle

BULL_THRESH =  0.15
BEAR_THRESH = -0.15


def _score_to_label(score: Optional[float]) -> str:
    if score is None:
        return 'unavailable'
    if score >= BULL_THRESH:
        return 'bullish'
    if score <= BEAR_THRESH:
        return 'bearish'
    return 'neutral'


def _cache_path(sym: str, source: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f'{sym}_{source}.json'


def _cache_load(sym: str, source: str) -> Optional[dict]:
    p = _cache_path(sym, source)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        if time.time() - d.get('ts', 0) < _CACHE_TTL:
            return d
    except Exception:
        pass
    return None


def _cache_save(sym: str, source: str, data: dict) -> None:
    try:
        _cache_path(sym, source).write_text(
            json.dumps({**data, 'ts': time.time()})
        )
    except Exception:
        pass


# ── Embedded financial keyword lexicon (no NLTK required) ────────────────────
# Each word maps to a score contribution. Tuned for financial news.

_BULLISH_WORDS = {
    'beat': 0.4, 'beats': 0.4, 'surge': 0.5, 'surges': 0.5, 'rally': 0.4,
    'rallies': 0.4, 'soar': 0.5, 'soars': 0.5, 'record': 0.3, 'growth': 0.3,
    'profit': 0.3, 'profits': 0.3, 'upgrade': 0.4, 'upgraded': 0.4,
    'outperform': 0.4, 'buy': 0.3, 'bullish': 0.5, 'strong': 0.3,
    'exceed': 0.4, 'exceeds': 0.4, 'positive': 0.3, 'gain': 0.3, 'gains': 0.3,
    'up': 0.1, 'rise': 0.3, 'rises': 0.3, 'higher': 0.2, 'boost': 0.3,
    'boosted': 0.3, 'recovery': 0.3, 'recover': 0.3, 'expansion': 0.3,
    'optimistic': 0.4, 'opportunity': 0.2, 'innovation': 0.2,
}
_BEARISH_WORDS = {
    'miss': -0.4, 'misses': -0.4, 'drop': -0.4, 'drops': -0.4, 'fall': -0.3,
    'falls': -0.3, 'decline': -0.3, 'declines': -0.3, 'cut': -0.3, 'cuts': -0.3,
    'downgrade': -0.4, 'downgraded': -0.4, 'sell': -0.3, 'bearish': -0.5,
    'weak': -0.3, 'loss': -0.4, 'losses': -0.4, 'warning': -0.4, 'warn': -0.4,
    'below': -0.2, 'negative': -0.3, 'concern': -0.3, 'concerns': -0.3,
    'risk': -0.2, 'risks': -0.2, 'lawsuit': -0.4, 'investigation': -0.3,
    'recall': -0.4, 'scandal': -0.5, 'fraud': -0.5, 'bankruptcy': -0.5,
    'crash': -0.5, 'plunge': -0.5, 'plunges': -0.5, 'slump': -0.4,
    'disappointing': -0.4, 'disappoint': -0.4, 'lower': -0.2,
}

def _keyword_score(text: str) -> float:
    """Score a text string using the financial keyword lexicon. Returns -1..+1."""
    words  = text.lower().split()
    total  = 0.0
    hits   = 0
    for w in words:
        w_clean = w.strip('.,!?;:()"\'')
        if w_clean in _BULLISH_WORDS:
            total += _BULLISH_WORDS[w_clean]
            hits  += 1
        elif w_clean in _BEARISH_WORDS:
            total += _BEARISH_WORDS[w_clean]
            hits  += 1
    if hits == 0:
        return 0.0
    # Normalise: cap at ±1
    raw = total / max(hits, 1)
    return max(-1.0, min(1.0, raw))


# ── Alpha Vantage News Sentiment Provider ─────────────────────────────────────

class AlphaVantageNewsProvider(SentimentProvider):
    """
    Alpha Vantage NEWS_SENTIMENT endpoint.
    Free tier: 25 calls/day. Per-ticker sentiment already computed by AV's NLP.
    Requires ALPHAVANTAGE_KEY in .env.

    AV score mapping:
      ticker_sentiment_score: -1 (bearish) to +1 (bullish)
      ticker_relevance_score: 0 to 1 (how relevant the article is to the ticker)
    We take the relevance-weighted average across all articles.
    """

    _BASE = 'https://www.alphavantage.co/query'

    def __init__(self, api_key: str):
        self._key = api_key

    @property
    def name(self) -> str:
        return 'alphavantage_news'

    def get_sentiment(self, symbol: str) -> SentimentResult:
        cached = _cache_load(symbol, 'av')
        if cached:
            return SentimentResult(
                score=cached['score'], label=cached['label'],
                source=self.name, status='ok',
                raw=cached.get('raw', {}),
            )

        try:
            params = {
                'function':  'NEWS_SENTIMENT',
                'tickers':   symbol,
                'limit':     50,
                'sort':      'LATEST',
                'apikey':    self._key,
            }
            resp = requests.get(self._BASE, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if 'Information' in data or 'Note' in data:
                # Rate limit message from AV
                msg = data.get('Information', data.get('Note', 'rate_limited'))
                log.warning('[SENT] AV rate limited: %s', msg[:80])
                return SentimentResult.unavailable(self.name, 'rate_limited')

            feed = data.get('feed', [])
            if not feed:
                return SentimentResult.unavailable(self.name, 'no_articles')

            # Extract per-ticker sentiment scores from each article
            weighted_scores = []
            headlines = []
            for article in feed:
                for ts in article.get('ticker_sentiment', []):
                    if ts.get('ticker', '').upper() == symbol.upper():
                        try:
                            s = float(ts['ticker_sentiment_score'])
                            r = float(ts['ticker_relevance_score'])
                            weighted_scores.append((s, r))
                            headlines.append(article.get('title', '')[:80])
                        except (KeyError, ValueError):
                            pass

            if not weighted_scores:
                return SentimentResult.unavailable(self.name, 'no_ticker_sentiment')

            # Relevance-weighted average
            total_w = sum(r for _, r in weighted_scores)
            if total_w == 0:
                score = sum(s for s, _ in weighted_scores) / len(weighted_scores)
            else:
                score = sum(s * r for s, r in weighted_scores) / total_w

            score  = round(max(-1.0, min(1.0, score)), 4)
            label  = _score_to_label(score)
            raw    = {
                'article_count':    len(feed),
                'scored_articles':  len(weighted_scores),
                'headlines':        headlines[:3],
                'fetched_at':       datetime.now(timezone.utc).isoformat(),
            }

            _cache_save(symbol, 'av', {'score': score, 'label': label, 'raw': raw})
            log.info('[SENT] AV %s: score=%.3f label=%s articles=%d',
                     symbol, score, label, len(weighted_scores))
            return SentimentResult(score=score, label=label,
                                   source=self.name, status='ok', raw=raw)

        except Exception as e:
            log.warning('[SENT] AV %s error: %s', symbol, str(e)[:80])
            return SentimentResult.unavailable(self.name, str(e)[:80])


# ── YFinance News Provider (no API key) ───────────────────────────────────────

class YFinanceNewsProvider(SentimentProvider):
    """
    Yahoo Finance news headlines via yfinance + embedded keyword scorer.
    No API key required. Works as long as Yahoo is reachable.
    Less accurate than AV but always available as fallback.
    """

    @property
    def name(self) -> str:
        return 'yfinance_news'

    def get_sentiment(self, symbol: str) -> SentimentResult:
        cached = _cache_load(symbol, 'yf_news')
        if cached:
            return SentimentResult(
                score=cached['score'], label=cached['label'],
                source=self.name, status='ok',
                raw=cached.get('raw', {}),
            )

        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            news   = ticker.news or []

            if not news:
                return SentimentResult.unavailable(self.name, 'no_articles')

            scores    = []
            headlines = []
            for item in news[:20]:   # cap at 20 most recent
                title   = item.get('title', '') or ''
                summary = item.get('summary', '') or item.get('description', '') or ''
                text    = f'{title} {summary}'
                if not text.strip():
                    continue
                s = _keyword_score(text)
                scores.append(s)
                headlines.append(title[:80])

            if not scores:
                return SentimentResult.unavailable(self.name, 'no_scorable_text')

            score = round(sum(scores) / len(scores), 4)
            label = _score_to_label(score)
            raw   = {
                'article_count':   len(news),
                'scored_articles': len(scores),
                'headlines':       headlines[:3],
                'fetched_at':      datetime.now(timezone.utc).isoformat(),
            }

            _cache_save(symbol, 'yf_news', {'score': score, 'label': label, 'raw': raw})
            log.info('[SENT] YFNews %s: score=%.3f label=%s articles=%d',
                     symbol, score, label, len(scores))
            return SentimentResult(score=score, label=label,
                                   source=self.name, status='ok', raw=raw)

        except Exception as e:
            log.warning('[SENT] YFNews %s error: %s', symbol, str(e)[:80])
            return SentimentResult.unavailable(self.name, str(e)[:80])
