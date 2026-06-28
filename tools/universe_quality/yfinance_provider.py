"""M21.UQ yfinance provider adapter (read-only, lazy import).

Implements ProviderProtocol via yfinance. yfinance is imported LAZILY inside
fetch_ohlcv so this module imports cleanly even where yfinance is not installed
(e.g. the unit-test sandbox); unit tests inject a fake fetch and never hit the
network. The adapter is read-only: it fetches OHLCV bars and returns them as a
list[dict]; it NEVER writes configs, never mutates candidates, never sets
scan_ready.

On any failure (symbol unknown, network error, empty frame) it returns None or
[] so the existing evaluators map it to ohlcv_empty — provider failure is
report-only and never a reason to mutate records.
"""
from typing import Callable, List, Optional

from tools.universe_quality.quality_model import FetchResult

# Substrings (case-insensitive) that identify a rate-limit condition in an
# exception type name or message. yfinance raises YFRateLimitError with text
# like "Too Many Requests. Rate limited. Try after a while."
_RATE_LIMIT_MARKERS = (
    "ratelimit", "rate limit", "rate limited", "too many requests", "429",
)


def classify_exception(exc) -> str:
    """Classify a provider exception as 'rate_limited' or 'fetch_error'."""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    blob = name + " " + msg
    for marker in _RATE_LIMIT_MARKERS:
        if marker in blob:
            return "rate_limited"
    return "fetch_error"


class YFinanceProvider:
    """ProviderProtocol implementation backed by yfinance.

    Parameters
    ----------
    period : str
        yfinance history period (e.g. '3mo'). Chosen to comfortably exceed the
        min_bars threshold for daily bars.
    interval : str
        yfinance interval (e.g. '1d').
    timeout : int
        Per-request timeout seconds.
    pace_seconds : float
        Optional sleep between calls to be polite to the provider.
    _fetch_fn : callable or None
        Injectable fetch for tests: provider_symbol -> list[dict] | None. When
        provided, yfinance is NOT imported or called (fully offline).
    """

    def __init__(self, period: str = "3mo", interval: str = "1d",
                 timeout: int = 20, pace_seconds: float = 0.0,
                 _fetch_fn: Optional[Callable[[str],
                                              Optional[List[dict]]]] = None):
        self.period = period
        self.interval = interval
        self.timeout = timeout
        self.pace_seconds = pace_seconds
        self._fetch_fn = _fetch_fn

    def fetch_ohlcv(self, provider_symbol: str) -> Optional[List[dict]]:
        """Back-compat: return bars (or None/[]); discards error classification.
        Prefer fetch_ohlcv_result() for honest error reporting."""
        return self.fetch_ohlcv_result(provider_symbol).bars

    def fetch_ohlcv_result(self, provider_symbol: str) -> FetchResult:
        """Structured fetch: returns FetchResult(bars, error_kind, error_text).

        error_kind is None on success/true-empty; 'rate_limited' or
        'fetch_error' on a classified provider exception. An injected _fetch_fn
        may itself raise (to simulate provider errors) or return bars/None/[].
        """
        if self._fetch_fn is not None:
            try:
                return FetchResult(bars=self._fetch_fn(provider_symbol))
            except Exception as exc:  # noqa: BLE001
                kind = classify_exception(exc)
                return FetchResult(bars=None, error_kind=kind,
                                   error_text=str(exc))
        return self._fetch_via_yfinance(provider_symbol)

    def _fetch_via_yfinance(self, provider_symbol: str) -> FetchResult:
        # Lazy import: only when actually fetching live, so the module is
        # importable without yfinance installed.
        try:
            import yfinance  # noqa: WPS433 (intentional lazy import)
        except Exception as exc:  # noqa: BLE001
            return FetchResult(bars=None, error_kind="fetch_error",
                               error_text="yfinance import failed: %s" % exc)
        if self.pace_seconds:
            import time
            time.sleep(self.pace_seconds)
        try:
            t = yfinance.Ticker(provider_symbol)
            df = t.history(period=self.period, interval=self.interval,
                           timeout=self.timeout, auto_adjust=False)
        except Exception as exc:  # noqa: BLE001
            kind = classify_exception(exc)
            return FetchResult(bars=None, error_kind=kind,
                               error_text=str(exc))
        if df is None or len(df) == 0:
            return FetchResult(bars=[])  # true empty, no error
        out: List[dict] = []
        try:
            for idx, row in df.iterrows():
                date = getattr(idx, "date", lambda: idx)()
                out.append({
                    "date": str(date),
                    "open": float(row.get("Open")),
                    "high": float(row.get("High")),
                    "low": float(row.get("Low")),
                    "close": float(row.get("Close")),
                    "volume": float(row.get("Volume")),
                })
        except Exception as exc:  # noqa: BLE001
            # malformed frame -> classify as fetch_error (not empty), never raise
            return FetchResult(bars=None, error_kind="fetch_error",
                               error_text="malformed frame: %s" % exc)
        return FetchResult(bars=out)
