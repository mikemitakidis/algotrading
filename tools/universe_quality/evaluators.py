"""M21.UQ quality evaluators (read-only, deterministic).

Each evaluator is a pure function returning reason codes. The orchestrator
`evaluate_candidate` composes them into a QualityResult. No network, no writes.
"""
import datetime
from typing import Dict, List, Optional

from tools.universe_quality.providers import (
    bars_all_finite, normalize_bars)
from tools.universe_quality.quality_model import (
    FATAL_CODES, LIQUIDITY_UNKNOWN, OHLCV_EMPTY, OHLCV_NON_FINITE,
    OHLCV_STALE, OHLCV_TOO_FEW_BARS, OHLCVConfig, PROVIDER_FETCH_ERROR,
    PROVIDER_RATE_LIMITED, PROVIDER_SUFFIX_INVALID,
    PROVIDER_SYMBOL_MISSING, ProviderBar, QualityResult,
    VOLUME_MISSING_OR_ZERO, WARNING_CODES)

# Canonical exchange -> yfinance suffix, sourced from the merged suffixes map so
# this stays consistent with the normaliser. Imported lazily to avoid a hard
# dependency if the module layout changes.
try:
    from bot.universe.suffixes import EXCHANGES as _EXCHANGES
    _EXCHANGE_SUFFIX = {k: v.yfinance_suffix for k, v in _EXCHANGES.items()}
except Exception:  # pragma: no cover - fallback for isolated test runs
    _EXCHANGE_SUFFIX = {
        "LSE": ".L", "TSE": ".T", "HKEX": ".HK", "XETRA": ".DE",
        "EPA": ".PA", "AEX": ".AS", "BME": ".MC", "SIX": ".SW",
    }


def check_provider_symbol(record: dict) -> List[str]:
    """Provider symbol present?"""
    yf = (record.get("provider_symbols") or {}).get("yfinance")
    if not yf or not isinstance(yf, str) or not yf.strip():
        return [PROVIDER_SYMBOL_MISSING]
    return []


def check_suffix(record: dict) -> List[str]:
    """yfinance suffix matches the canonical suffix for the exchange.

    US venues (no suffix) are allowed only if the exchange has empty suffix.
    """
    yf = (record.get("provider_symbols") or {}).get("yfinance")
    exch = record.get("exchange")
    if not yf or exch is None:
        return []  # provider-missing handled elsewhere
    expected = _EXCHANGE_SUFFIX.get(exch)
    if expected is None:
        # unknown exchange -> cannot validate suffix -> treat as invalid
        return [PROVIDER_SUFFIX_INVALID]
    if expected == "":
        return [] if "." not in yf else [PROVIDER_SUFFIX_INVALID]
    return [] if yf.endswith(expected) else [PROVIDER_SUFFIX_INVALID]


def check_ohlcv(bars: Optional[List[ProviderBar]], as_of: str,
                cfg: OHLCVConfig) -> List[str]:
    """OHLCV quality: empty / too-few / stale / non-finite.

    bars is None  -> empty (unfetchable treated as empty data).
    bars == []    -> empty.
    """
    codes: List[str] = []
    if bars is None or len(bars) == 0:
        return [OHLCV_EMPTY]
    if len(bars) < cfg.min_bars:
        codes.append(OHLCV_TOO_FEW_BARS)
    if not bars_all_finite(bars):
        codes.append(OHLCV_NON_FINITE)
    # staleness: last bar date vs as_of
    try:
        last = max(datetime.date.fromisoformat(b.date) for b in bars)
        asof_d = datetime.date.fromisoformat(as_of)
        if (asof_d - last).days > cfg.max_staleness_days:
            codes.append(OHLCV_STALE)
    except (ValueError, TypeError):
        codes.append(OHLCV_NON_FINITE)  # unparseable dates = bad data
    return codes


def check_volume(bars: Optional[List[ProviderBar]]) -> List[str]:
    """Volume present and non-zero across bars (any all-zero/negative/missing/
    non-finite volume fails)."""
    import math
    if not bars:
        return [VOLUME_MISSING_OR_ZERO]
    total = 0.0
    saw_valid = False
    for b in bars:
        try:
            v = float(b.volume)
        except (TypeError, ValueError):
            return [VOLUME_MISSING_OR_ZERO]
        if not math.isfinite(v):
            return [VOLUME_MISSING_OR_ZERO]
        if v < 0:
            return [VOLUME_MISSING_OR_ZERO]
        if v > 0:
            saw_valid = True
        total += v
    if not saw_valid or total <= 0:
        return [VOLUME_MISSING_OR_ZERO]
    return []


def check_liquidity(record: dict) -> List[str]:
    """Liquidity known? For inactive candidates these fields are intentionally
    null, so this returns LIQUIDITY_UNKNOWN as a WARNING (non-fatal)."""
    fields = ("avg_volume_20d", "avg_dollar_volume_20d", "median_spread_bps",
              "min_liquidity_tier")
    if all(record.get(f) is None for f in fields):
        return [LIQUIDITY_UNKNOWN]
    return []


def evaluate_candidate(record: dict, provider=None,
                       cfg: Optional[OHLCVConfig] = None,
                       as_of: Optional[str] = None) -> QualityResult:
    """Compose all evaluators into a QualityResult for one candidate.

    provider: a ProviderProtocol (e.g. FixtureProvider) or None. If None, OHLCV
    checks are SKIPPED (symbol/suffix/liquidity still run) so a provider-less
    structural pass is possible.
    """
    cfg = cfg or OHLCVConfig()
    as_of = as_of or datetime.date.today().isoformat()
    reasons: List[str] = []
    warnings: List[str] = []
    details: Dict = {}

    yf = (record.get("provider_symbols") or {}).get("yfinance")
    reasons += check_provider_symbol(record)
    reasons += check_suffix(record)

    if provider is not None and yf:
        # Prefer the structured result so a provider exception (rate-limit /
        # fetch error) is classified honestly rather than swallowed into
        # ohlcv_empty / volume_missing_or_zero.
        if hasattr(provider, "fetch_ohlcv_result"):
            fr = provider.fetch_ohlcv_result(yf)
            error_kind = fr.error_kind
            raw = fr.bars
            if fr.error_text:
                details["provider_error_text"] = fr.error_text
        else:
            error_kind = None
            raw = provider.fetch_ohlcv(yf)

        if error_kind == "rate_limited":
            # could not evaluate due to provider rate limit; do NOT run OHLCV/
            # volume checks (they'd mislabel it ohlcv_empty / volume_missing).
            reasons.append(PROVIDER_RATE_LIMITED)
            details["bar_count"] = None
        elif error_kind == "fetch_error":
            reasons.append(PROVIDER_FETCH_ERROR)
            details["bar_count"] = None
        else:
            bars = normalize_bars(raw)
            details["bar_count"] = 0 if not bars else len(bars)
            reasons += check_ohlcv(bars, as_of, cfg)
            reasons += check_volume(bars)

    for code in check_liquidity(record):
        warnings.append(code)

    # de-dup, split fatal vs warning
    fatal = [c for c in dict.fromkeys(reasons) if c in FATAL_CODES]
    warns = [c for c in dict.fromkeys(warnings) if c in WARNING_CODES]
    passed = not fatal
    return QualityResult(
        internal_symbol=record.get("internal_symbol", "?"),
        provider_symbol=yf, region=record.get("region", "?"),
        exchange=record.get("exchange", "?"), passed=passed,
        reason_codes=fatal, warnings=warns, details=details)


def find_duplicate_provider_symbols(records: List[dict]) -> Dict[str, int]:
    """Return {provider_symbol: count} for provider symbols used more than
    once across the supplied records."""
    from collections import Counter
    yfs = [(r.get("provider_symbols") or {}).get("yfinance") for r in records]
    yfs = [y for y in yfs if y]
    return {y: c for y, c in Counter(yfs).items() if c > 1}
