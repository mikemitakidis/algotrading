"""bot/historical/providers_yfinance.py — M16 yfinance adapter.

Wraps yfinance (already a project dependency via M6) into the M16
BaseProvider contract.

Capability:
  * supported_timeframes: 1D, 1H, 15m (4H is resampled from 1H in the
                              storage layer, not fetched natively)
  * lookback caps: 15m -> 60d, 1H -> 730d, 1D -> max (decades)
  * supports_adjusted: yfinance returns Adj Close alongside raw OHLC
  * polite_calls_per_minute: 60 conservative starting value
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pandas as pd

from bot.historical.providers import (BaseProvider, FETCH_NO_DATA, FETCH_OK,
                                  FETCH_PROVIDER_ERROR, FETCH_RATE_LIMITED,
                                  FetchResult, ProviderCapability)


log = logging.getLogger(__name__)


YFINANCE_CAPABILITY = ProviderCapability(
    name="yfinance",
    supported_timeframes=frozenset({"1D", "1H", "15m"}),
    lookback_caps={"1D": "max", "1H": "730d", "15m": "60d"},
    supports_adjusted=True,
    polite_calls_per_minute=60,
    bulk_symbols_per_call=1,
    notes=("yfinance is the V1 default provider. 4H bars are resampled "
            "from 1H in bot.historical.timeframes — not fetched natively from "
            "yfinance. Adjusted OHLC is approximated using "
            "adjustment_ratio = adj_close / close (yfinance does not "
            "expose adjusted open/high/low separately)."),
)


# yfinance interval string mapping.
_TF_TO_YF_INTERVAL = {
    "1D":  "1d",
    "1H":  "1h",
    "15m": "15m",
}


class YFinanceProvider(BaseProvider):
    """Concrete provider — calls yfinance.download."""

    def __init__(self):
        # Lazy-import to keep the module importable in tests that don't
        # need yfinance, and to keep AST scans happy.
        self._yf = None
        self._yf_rate_limit_exc = None  # populated by _yfinance()

    def _yfinance(self):
        if self._yf is None:
            import yfinance as yf
            self._yf = yf
            # Cache the rate-limit exception class (importable in
            # yfinance >= 0.2.x; None on older versions). Used to
            # classify exceptions cleanly without string heuristics
            # being the only signal.
            try:
                from yfinance.exceptions import YFRateLimitError
                self._yf_rate_limit_exc = YFRateLimitError
            except ImportError:
                self._yf_rate_limit_exc = None
        return self._yf

    @property
    def capability(self) -> ProviderCapability:
        return YFINANCE_CAPABILITY

    def fetch_bars(self, symbol: str, timeframe: str,
                    start_utc: datetime, end_utc: datetime,
                    ) -> FetchResult:
        if timeframe not in _TF_TO_YF_INTERVAL:
            return FetchResult(
                outcome=FETCH_PROVIDER_ERROR,
                message=f"unsupported timeframe for yfinance: {timeframe}",
            )
        interval = _TF_TO_YF_INTERVAL[timeframe]

        # yfinance: pass dates as YYYY-MM-DD; intraday needs `period` or
        # `start/end`. For consistency we use start/end.
        # yfinance is also picky: start <= end, both inclusive of the
        # window we want.
        s = start_utc.strftime("%Y-%m-%d")
        e = (end_utc + timedelta(days=1)).strftime("%Y-%m-%d")

        yf = self._yfinance()

        # Reset yfinance's per-symbol error registry before this call so
        # we can detect rate-limit signals AFTER yf.download returns.
        # yfinance >= 0.2 catches per-symbol exceptions internally and
        # returns an empty DataFrame; the original exception is recorded
        # in yf.shared._ERRORS[symbol]. Without this inspection we
        # cannot distinguish rate-limit-empty from genuinely-no-data-
        # empty (that was the M16.A.fix-1 bug).
        try:
            if hasattr(yf, "shared") and hasattr(yf.shared, "_ERRORS"):
                yf.shared._ERRORS.clear()
        except Exception:  # noqa: BLE001 - defensive
            pass

        try:
            df = yf.download(
                symbol, start=s, end=e, interval=interval,
                progress=False, auto_adjust=False, actions=False,
                threads=False,
            )
        except Exception as exc:  # noqa: BLE001 - provider-error wrapping
            return self._classify_exception(exc)

        # Inspect yfinance's per-symbol error registry. If it contains a
        # rate-limit signal for our symbol, classify the empty-DF
        # response as RATE_LIMITED (not NO_DATA).
        rl_msg = self._scan_yf_errors_for_rate_limit(yf, symbol)
        if rl_msg is not None:
            return FetchResult(outcome=FETCH_RATE_LIMITED, message=rl_msg)

        # Non-rate-limit per-symbol error: surface as provider_error.
        pe_msg = self._scan_yf_errors_for_provider_error(yf, symbol)
        if pe_msg is not None:
            return FetchResult(outcome=FETCH_PROVIDER_ERROR, message=pe_msg)

        if df is None or len(df) == 0:
            return FetchResult(outcome=FETCH_NO_DATA,
                                message="yfinance returned empty DataFrame")

        # Normalise columns. yfinance returns MultiIndex columns when
        # multiple symbols are requested; we always request one.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Expected columns: Open, High, Low, Close, Adj Close, Volume.
        try:
            out = pd.DataFrame({
                "ts_utc": _to_utc_index(df.index),
                "open":   df["Open"].astype(float).values,
                "high":   df["High"].astype(float).values,
                "low":    df["Low"].astype(float).values,
                "close":  df["Close"].astype(float).values,
                "volume": df["Volume"].fillna(0).astype("int64").values,
                "adj_close": df["Adj Close"].astype(float).values
                    if "Adj Close" in df.columns else df["Close"].astype(float).values,
            })
        except Exception as exc:  # noqa: BLE001
            return FetchResult(
                outcome=FETCH_PROVIDER_ERROR,
                message=f"failed to parse yfinance response: {exc}")

        # Compute adjustment_ratio = adj_close / close (yfinance only
        # provides adjusted close; we use this ratio uniformly for OHL).
        out["adjustment_ratio"] = out.apply(
            lambda r: r["adj_close"] / r["close"]
                if r["close"] not in (0, None) and pd.notna(r["close"])
                else 1.0,
            axis=1,
        )
        out["is_adjusted"] = True
        out["provider"] = "yfinance"
        out["quality_flags"] = 0

        # yfinance returns NaN rows for non-trading days for daily;
        # filter them.
        out = out.dropna(subset=["open", "high", "low", "close"])
        if len(out) == 0:
            return FetchResult(outcome=FETCH_NO_DATA,
                                message="all rows had NaN OHLC; treating as no_data")

        return FetchResult(outcome=FETCH_OK, df=out.reset_index(drop=True))

    # -- classification helpers -------------------------------------------

    # Substrings (lower-cased) that mean "rate limited" in either an
    # exception message or a yf.shared._ERRORS entry. We check by type
    # FIRST (YFRateLimitError) and fall back to substring match — the
    # substrings are belt-and-braces for upstream signal drift.
    _RATE_LIMIT_TOKENS = (
        "yfratelimiterror",     # repr of yfinance.exceptions.YFRateLimitError
        "rate limit",
        "rate-limit",
        "rate limited",
        "too many requests",
        "429",
    )

    def _is_rate_limit_signal(self, text: str) -> bool:
        if not text:
            return False
        lower = text.lower()
        return any(tok in lower for tok in self._RATE_LIMIT_TOKENS)

    def _classify_exception(self, exc: BaseException) -> FetchResult:
        """Map a raised exception to a FetchResult.

        Order of precedence:
          1. isinstance check against YFRateLimitError (if importable)
          2. exception type-name match for YFRateLimitError (in case
             import path differs across versions)
          3. substring match on str(exc)
        """
        if (self._yf_rate_limit_exc is not None
                and isinstance(exc, self._yf_rate_limit_exc)):
            return FetchResult(outcome=FETCH_RATE_LIMITED, message=str(exc))
        if type(exc).__name__ == "YFRateLimitError":
            return FetchResult(outcome=FETCH_RATE_LIMITED, message=str(exc))
        if self._is_rate_limit_signal(str(exc)):
            return FetchResult(outcome=FETCH_RATE_LIMITED, message=str(exc))
        return FetchResult(outcome=FETCH_PROVIDER_ERROR, message=str(exc))

    def _scan_yf_errors_for_rate_limit(self, yf, symbol: str):
        """If yf.shared._ERRORS contains a rate-limit signal, return its
        message; otherwise None.

        yfinance >= 0.2 catches per-symbol exceptions inside `download()`
        and stores the exception repr in `yf.shared._ERRORS[symbol]`.
        Without this scan, the empty DataFrame returned by `download()`
        was being misclassified as NO_DATA. (M16.A.fix-1.)
        """
        try:
            errors = getattr(yf.shared, "_ERRORS", None)
            if not errors:
                return None
        except Exception:  # noqa: BLE001
            return None
        # Try the exact key first, then any other key (the registry is
        # global across calls; we cleared it earlier so the entry, if
        # any, belongs to THIS call).
        candidates = []
        if symbol in errors:
            candidates.append((symbol, errors[symbol]))
        for k, v in errors.items():
            if k != symbol:
                candidates.append((k, v))
        for _, msg in candidates:
            if self._is_rate_limit_signal(str(msg)):
                return str(msg)
        return None

    def _scan_yf_errors_for_provider_error(self, yf, symbol: str):
        """If yf.shared._ERRORS has a NON-rate-limit error, return it.

        Called after _scan_yf_errors_for_rate_limit returns None, so
        any remaining error in the registry is a provider problem worth
        surfacing as provider_error (not silently treating as no_data).
        """
        try:
            errors = getattr(yf.shared, "_ERRORS", None)
            if not errors:
                return None
        except Exception:  # noqa: BLE001
            return None
        # If the only entries are about other symbols, ignore them.
        own = errors.get(symbol)
        if own:
            return str(own)
        return None


def _to_utc_index(idx) -> pd.Series:
    """Convert a (possibly tz-naive) DatetimeIndex to a tz-aware UTC Series.

    yfinance returns naive datetimes for daily data (date-only) and
    tz-naive datetimes localised to America/New_York for intraday.
    We normalise everything to UTC.
    """
    ser = pd.Series(idx)
    if ser.dt.tz is None:
        # Daily: dates are already UTC-equivalent (start-of-day).
        # Intraday: yfinance returns America/New_York-local naive ts.
        # Heuristic: if any timestamp has non-midnight hour, treat as
        # NY; otherwise as UTC midnight.
        has_intraday = ser.dt.hour.ne(0).any() or ser.dt.minute.ne(0).any()
        if has_intraday:
            ser = ser.dt.tz_localize("America/New_York",
                                       ambiguous="NaT",
                                       nonexistent="shift_forward")
            ser = ser.dt.tz_convert("UTC")
        else:
            ser = ser.dt.tz_localize("UTC")
    else:
        ser = ser.dt.tz_convert("UTC")
    return ser
