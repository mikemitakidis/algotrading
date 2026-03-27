"""
bot/sentiment/news_provider.py
Real news sentiment providers — Milestone 8.

Sources implemented:
  yfinance_news   — yfinance Ticker.news + keyword scorer (no API key)
  google_news     — Google News RSS + keyword scorer (no API key)
  alphavantage    — Alpha Vantage NEWS_SENTIMENT (requires ALPHAVANTAGE_KEY)

All providers:
  - Cache results for 1h to data/sentiment_cache/
  - Never raise — return SentimentResult with exact failure reason
  - Return raw dict with article_count, headlines, fetch_attempted, error

Score scale: -1.0 (very bearish) to +1.0 (very bullish)
Label thresholds: >= +0.15 bullish, <= -0.15 bearish, else neutral
"""
import json
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from bot.sentiment.base import SentimentProvider, SentimentResult

log = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / 'data' / 'sentiment_cache'
_CACHE_TTL = 3600   # 1 hour

BULL_THRESH =  0.15
BEAR_THRESH = -0.15

_SESSION_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/122.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


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


def _cache_load(sym: str, source: str, force_live: bool = False) -> Optional[dict]:
    if force_live:
        return None
    p = _cache_path(sym, source)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        if time.time() - d.get('ts', 0) < _CACHE_TTL:
            d['_cache_used'] = True
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


def _classify_error(err: str) -> str:
    """Classify a network/fetch error into a human-readable status."""
    e = err.lower()
    if 'proxy' in e or '403' in e or 'forbidden' in e:
        return 'blocked_by_proxy'
    if 'timeout' in e or 'timed out' in e:
        return 'timeout'
    if '429' in e or 'rate' in e or 'too many' in e:
        return 'rate_limited'
    if 'connection' in e or 'network' in e or 'resolve' in e:
        return 'network_error'
    if 'ssl' in e or 'certificate' in e:
        return 'ssl_error'
    return 'fetch_failed'


# ── Embedded financial keyword lexicon ────────────────────────────────────────

_BULL = {
    'beat': 0.4, 'beats': 0.4, 'surge': 0.5, 'surges': 0.5, 'rally': 0.4,
    'rallies': 0.4, 'soar': 0.5, 'soars': 0.5, 'record': 0.3, 'growth': 0.3,
    'profit': 0.3, 'profits': 0.3, 'upgrade': 0.4, 'upgraded': 0.4,
    'outperform': 0.4, 'buy': 0.3, 'bullish': 0.5, 'strong': 0.3,
    'exceed': 0.4, 'exceeds': 0.4, 'positive': 0.3, 'gain': 0.3, 'gains': 0.3,
    'rise': 0.3, 'rises': 0.3, 'higher': 0.2, 'boost': 0.3, 'boosted': 0.3,
    'recovery': 0.3, 'recover': 0.3, 'expansion': 0.3, 'optimistic': 0.4,
    'opportunity': 0.2, 'innovation': 0.2, 'breakthrough': 0.4, 'raised': 0.3,
}
_BEAR = {
    'miss': -0.4, 'misses': -0.4, 'drop': -0.4, 'drops': -0.4, 'fall': -0.3,
    'falls': -0.3, 'decline': -0.3, 'declines': -0.3, 'cut': -0.3, 'cuts': -0.3,
    'downgrade': -0.4, 'downgraded': -0.4, 'sell': -0.3, 'bearish': -0.5,
    'weak': -0.3, 'loss': -0.4, 'losses': -0.4, 'warning': -0.4, 'warn': -0.4,
    'below': -0.2, 'negative': -0.3, 'concern': -0.3, 'concerns': -0.3,
    'risk': -0.2, 'risks': -0.2, 'lawsuit': -0.4, 'investigation': -0.3,
    'recall': -0.4, 'scandal': -0.5, 'fraud': -0.5, 'bankruptcy': -0.5,
    'crash': -0.5, 'plunge': -0.5, 'plunges': -0.5, 'slump': -0.4,
    'disappointing': -0.4, 'disappoint': -0.4, 'lower': -0.2, 'lowered': -0.3,
    'layoff': -0.4, 'layoffs': -0.4, 'restructuring': -0.3, 'shortfall': -0.4,
}


def _keyword_score(text: str) -> float:
    words = text.lower().split()
    total, hits = 0.0, 0
    for w in words:
        w = w.strip('.,!?;:()"\'')
        if w in _BULL:
            total += _BULL[w]; hits += 1
        elif w in _BEAR:
            total += _BEAR[w]; hits += 1
    if hits == 0:
        return 0.0
    return max(-1.0, min(1.0, total / max(hits, 1)))


def _score_headlines(headlines: list) -> tuple:
    """Score a list of headline strings. Returns (avg_score, per_headline_scores)."""
    scores = []
    for h in headlines:
        s = _keyword_score(h)
        scores.append(s)
    if not scores:
        return 0.0, []
    return round(sum(scores) / len(scores), 4), scores


# ── YFinance News Provider ────────────────────────────────────────────────────

class YFinanceNewsProvider(SentimentProvider):
    """
    Yahoo Finance news via yfinance Ticker.news + keyword scorer.
    Requires Yahoo Finance to be reachable (fc.yahoo.com:443).
    Known to fail on servers behind proxies that block Yahoo.
    """

    @property
    def name(self) -> str:
        return 'yfinance_news'

    def get_sentiment(self, symbol: str, force_live: bool = False) -> SentimentResult:
        cached = _cache_load(symbol, 'yf_news', force_live=force_live)
        if cached:
            r = cached.get('raw', {})
            r['cache_used'] = True
            r['force_live'] = False
            return SentimentResult(
                score=cached['score'], label=cached['label'],
                source=self.name, status='ok', raw=r,
            )

        raw = {
            'fetch_attempted': True,
            'fetch_success':   False,
            'cache_used':      False,
            'force_live':      force_live,
            'article_count':   0,
            'headlines':       [],
            'error':           None,
            'error_class':     None,
            'fetched_at':      datetime.now(timezone.utc).isoformat(),
        }

        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            news   = ticker.news or []

            raw['article_count'] = len(news)

            if not news:
                raw['error']       = 'Yahoo returned 0 articles'
                raw['error_class'] = 'no_articles'
                return SentimentResult(
                    score=None, label='unavailable',
                    source=self.name, status='unavailable', raw=raw,
                )

            # yfinance news item keys vary by version:
            # newer: item['content']['title'] or item['title']
            # older: item['title']
            headlines = []
            for item in news[:20]:
                title = ''
                if isinstance(item.get('content'), dict):
                    title = item['content'].get('title', '') or ''
                if not title:
                    title = item.get('title', '') or ''
                if not title:
                    # Try any string value in the dict
                    for v in item.values():
                        if isinstance(v, str) and len(v) > 10:
                            title = v; break
                if title:
                    headlines.append(title[:120])

            if not headlines:
                # Articles fetched but couldn't extract titles — still score as neutral
                raw['fetch_success'] = True
                raw['error']         = f'Fetched {len(news)} articles but no extractable titles (yfinance schema may have changed)'
                raw['error_class']   = 'no_extractable_titles'
                raw['item_keys']     = list(news[0].keys()) if news else []
                _cache_save(symbol, 'yf_news', {'score': 0.0, 'label': 'neutral', 'raw': raw})
                return SentimentResult(
                    score=0.0, label='neutral',
                    source=self.name, status='ok', raw=raw,
                )

            score, _ = _score_headlines(headlines)
            label = _score_to_label(score)

            raw['fetch_success'] = True
            raw['headlines']     = headlines[:5]

            _cache_save(symbol, 'yf_news', {'score': score, 'label': label, 'raw': raw})
            return SentimentResult(
                score=score, label=label,
                source=self.name, status='ok', raw=raw,
            )

        except Exception as e:
            err_str = str(e)
            err_cls = _classify_error(err_str)
            raw['error']       = err_str[:200]
            raw['error_class'] = err_cls
            log.warning('[SENT] YFNews %s: %s — %s', symbol, err_cls, err_str[:80])
            return SentimentResult(
                score=None, label='unavailable',
                source=self.name, status=err_cls, raw=raw,
            )


# ── Google News RSS Provider ──────────────────────────────────────────────────

class GoogleNewsProvider(SentimentProvider):
    """
    Google News RSS feed + keyword scorer.
    No API key required. Uses Google's public RSS endpoint.
    Parses XML directly — no feedparser dependency.
    """

    _RSS = 'https://news.google.com/rss/search'

    @property
    def name(self) -> str:
        return 'google_news'

    def get_sentiment(self, symbol: str, force_live: bool = False) -> SentimentResult:
        cached = _cache_load(symbol, 'google_news', force_live=force_live)
        if cached:
            r = cached.get('raw', {}); r['cache_used'] = True; r['force_live'] = False
            return SentimentResult(score=cached['score'], label=cached['label'], source=self.name, status='ok', raw=r,)

        raw = {
            'fetch_attempted': True,
            'fetch_success':   False,
            'cache_used':      False,
            'force_live':      force_live,
            'article_count':   0,
            'headlines':       [],
            'error':           None,
            'error_class':     None,
            'fetched_at':      datetime.now(timezone.utc).isoformat(),
        }

        try:
            params = {'q': f'{symbol} stock', 'hl': 'en-US', 'gl': 'US', 'ceid': 'US:en'}
            resp   = requests.get(
                self._RSS, params=params,
                headers=_SESSION_HEADERS, timeout=10,
            )
            resp.raise_for_status()

            # Parse RSS XML
            root  = ET.fromstring(resp.text)
            items = root.findall('.//item')
            raw['article_count'] = len(items)

            if not items:
                raw['error']       = 'Google News returned 0 articles'
                raw['error_class'] = 'no_articles'
                return SentimentResult(
                    score=None, label='unavailable',
                    source=self.name, status='unavailable', raw=raw,
                )

            headlines = []
            for item in items[:20]:
                title = item.findtext('title') or ''
                if title:
                    headlines.append(title[:120])

            score, _ = _score_headlines(headlines)
            label = _score_to_label(score)

            raw['fetch_success'] = True
            raw['headlines']     = headlines[:3]

            _cache_save(symbol, 'google_news', {'score': score, 'label': label, 'raw': raw})
            return SentimentResult(
                score=score, label=label,
                source=self.name, status='ok', raw=raw,
            )

        except Exception as e:
            err_str = str(e)
            err_cls = _classify_error(err_str)
            raw['error']       = err_str[:200]
            raw['error_class'] = err_cls
            log.warning('[SENT] GoogleNews %s: %s — %s', symbol, err_cls, err_str[:80])
            return SentimentResult(
                score=None, label='unavailable',
                source=self.name,
                status=err_cls,
                raw=raw,
            )


# ── Alpha Vantage Provider ────────────────────────────────────────────────────

class AlphaVantageNewsProvider(SentimentProvider):
    """
    Alpha Vantage NEWS_SENTIMENT endpoint.
    Free tier: 25 requests/day. Pre-scored by AV's NLP.
    Requires ALPHAVANTAGE_KEY in .env.
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
                source=self.name, status='ok', raw=cached.get('raw', {}),
            )

        raw = {
            'fetch_attempted': True,
            'fetch_success':   False,
            'article_count':   0,
            'headlines':       [],
            'error':           None,
            'error_class':     None,
            'fetched_at':      datetime.now(timezone.utc).isoformat(),
        }

        try:
            resp = requests.get(
                self._BASE,
                params={'function': 'NEWS_SENTIMENT', 'tickers': symbol,
                        'limit': 50, 'sort': 'LATEST', 'apikey': self._key},
                headers=_SESSION_HEADERS, timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            if 'Information' in data or 'Note' in data:
                msg = data.get('Information', data.get('Note', ''))
                raw['error'] = msg[:200]
                raw['error_class'] = 'rate_limited'
                return SentimentResult(
                    score=None, label='unavailable',
                    source=self.name, status='rate_limited', raw=raw,
                )

            feed = data.get('feed', [])
            raw['article_count'] = len(feed)

            if not feed:
                raw['error'] = 'AV returned 0 articles'
                raw['error_class'] = 'no_articles'
                return SentimentResult(
                    score=None, label='unavailable',
                    source=self.name, status='unavailable', raw=raw,
                )

            weighted, headlines = [], []
            for article in feed:
                for ts in article.get('ticker_sentiment', []):
                    if ts.get('ticker', '').upper() == symbol.upper():
                        try:
                            s = float(ts['ticker_sentiment_score'])
                            r = float(ts['ticker_relevance_score'])
                            weighted.append((s, r))
                            headlines.append(article.get('title', '')[:80])
                        except (KeyError, ValueError):
                            pass

            if not weighted:
                raw['error'] = 'No per-ticker sentiment in AV response'
                raw['error_class'] = 'no_ticker_sentiment'
                return SentimentResult(
                    score=None, label='unavailable',
                    source=self.name, status='unavailable', raw=raw,
                )

            total_w = sum(r for _, r in weighted)
            score = (sum(s * r for s, r in weighted) / total_w
                     if total_w > 0
                     else sum(s for s, _ in weighted) / len(weighted))
            score = round(max(-1.0, min(1.0, score)), 4)
            label = _score_to_label(score)

            raw['fetch_success'] = True
            raw['headlines']     = headlines[:3]

            _cache_save(symbol, 'av', {'score': score, 'label': label, 'raw': raw})
            return SentimentResult(
                score=score, label=label,
                source=self.name, status='ok', raw=raw,
            )

        except Exception as e:
            err_str = str(e)
            err_cls = _classify_error(err_str)
            raw['error']       = err_str[:200]
            raw['error_class'] = err_cls
            return SentimentResult(
                score=None, label='unavailable',
                source=self.name, status=err_cls, raw=raw,
            )
