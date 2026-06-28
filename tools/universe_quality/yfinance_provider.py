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
        if self._fetch_fn is not None:
            return self._fetch_fn(provider_symbol)
        return self._fetch_via_yfinance(provider_symbol)

    def _fetch_via_yfinance(self,
                            provider_symbol: str) -> Optional[List[dict]]:
        # Lazy import: only when actually fetching live, so the module is
        # importable without yfinance installed.
        try:
            import yfinance  # noqa: WPS433 (intentional lazy import)
        except Exception:  # noqa: BLE001
            return None
        if self.pace_seconds:
            import time
            time.sleep(self.pace_seconds)
        try:
            t = yfinance.Ticker(provider_symbol)
            df = t.history(period=self.period, interval=self.interval,
                           timeout=self.timeout, auto_adjust=False)
        except Exception:  # noqa: BLE001
            return None
        if df is None or len(df) == 0:
            return []
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
        except Exception:  # noqa: BLE001
            # malformed frame -> treat as empty (report-only), never raise
            return []
        return out
