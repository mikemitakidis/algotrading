"""
M13.2 — Low-level eToro Public API HTTP client.

Hard contract:
  * Exactly ONE network-issuing method: `get(path, params)`.
  * NO `post`, NO `delete`, NO `put`, NO `patch`, NO generic
    `request(method, ...)` — by design.
  * No `urllib.request.Request(..., method='POST'|'DELETE'|...)` calls.
  * No `os.environ` / `os.getenv` reads — credentials are passed in.
  * Token-bucket rate limiter (60/min default) uses INJECTABLE clock
    + sleeper so unit tests are deterministic and never block.

Enforced by the AST-based test in test_m13_2_etoro_read.py.

Transport abstraction:
  * The client takes an optional `transport` callable. Default uses
    `urllib.request.urlopen`.
  * Tests inject a fake transport that returns canned
    `(status, headers, body_bytes)` tuples without any network I/O.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from .errors import (
    EtoroAuthError,
    EtoroError,
    EtoroRateLimitError,
    EtoroRouteError,
    EtoroTransientError,
    EtoroValidationError,
)

log = logging.getLogger(__name__)

# Type aliases
TransportResult = Tuple[int, Dict[str, str], bytes]   # status, headers, body
Transport = Callable[[str, Dict[str, str], float], TransportResult]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]


# ---------------------------------------------------------------------------
# Default transport — only place a real network call may originate.
# ---------------------------------------------------------------------------
def _default_transport(url: str, headers: Dict[str, str],
                       timeout: float) -> TransportResult:
    """Real network transport. Issues GET ONLY.

    No `method=` keyword — `urllib.request.Request` defaults to GET when
    `method` is omitted and `data` is None.
    """
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return resp.getcode(), hdrs, body
    except urllib.error.HTTPError as e:
        body = e.read() if hasattr(e, 'read') else b''
        hdrs = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        return e.code, hdrs, body
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise EtoroTransientError(f'network error: {type(e).__name__}: {e}')


# ---------------------------------------------------------------------------
# Token bucket — injectable clock + sleeper for deterministic tests
# ---------------------------------------------------------------------------
@dataclass
class TokenBucket:
    """60 GET/min by default. Refills continuously.

    Clock and sleeper are injectable so tests can advance virtual time
    without sleeping the test runner.
    """
    capacity: float
    refill_per_sec: float
    clock: Clock
    sleeper: Sleeper
    _tokens: float = 0.0
    _last_refill: float = 0.0

    def __post_init__(self):
        self._tokens = self.capacity
        self._last_refill = self.clock()

    def acquire(self, tokens: float = 1.0) -> None:
        """Block (via sleeper) until `tokens` are available, then deduct."""
        while True:
            now = self.clock()
            elapsed = now - self._last_refill
            if elapsed > 0:
                self._tokens = min(
                    self.capacity,
                    self._tokens + elapsed * self.refill_per_sec,
                )
                self._last_refill = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return
            # Sleep just long enough for the missing tokens to arrive
            deficit = tokens - self._tokens
            wait = deficit / self.refill_per_sec if self.refill_per_sec > 0 else 0.001
            self.sleeper(wait)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class EtoroClient:
    """GET-only HTTP client for eToro Public API.

    Construct with credentials. No env reads. No global state. Multiple
    instances each have their own independent rate-limit bucket
    (matches eToro's per-user-key limit semantics).
    """

    def __init__(
        self,
        api_key: str,
        user_key: str,
        base_url: str = 'https://public-api.etoro.com/api/v1',
        timeout_sec: float = 10.0,
        max_retries: int = 3,
        rate_limit_per_min: int = 60,
        transport: Optional[Transport] = None,
        clock: Optional[Clock] = None,
        sleeper: Optional[Sleeper] = None,
        request_id_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        if not api_key or not user_key:
            raise EtoroAuthError('api_key and user_key are required')
        self._api_key = api_key
        self._user_key = user_key
        self.base_url = base_url.rstrip('/')
        self.timeout_sec = float(timeout_sec)
        self.max_retries = int(max_retries)
        self._transport = transport or _default_transport
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._request_id_factory = request_id_factory or (lambda: str(uuid.uuid4()))
        self._bucket = TokenBucket(
            capacity=float(rate_limit_per_min),
            refill_per_sec=float(rate_limit_per_min) / 60.0,
            clock=self._clock,
            sleeper=self._sleeper,
        )

    # ---- public API: ONE method ----

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Issue a GET request. Raises typed errors on failure.

        This is the ONLY network-issuing method on the client. There is
        no `post`, no `delete`, no `put`, no `patch`, no generic
        `request()`. By design.
        """
        return self._issue_get(path, params)

    # ---- internals ----

    def _build_url(self, path: str, params: Optional[Dict[str, Any]]) -> str:
        if not path.startswith('/'):
            path = '/' + path
        url = self.base_url + path
        if params:
            # Drop None values; coerce lists to comma-separated per eToro convention
            cleaned: Dict[str, str] = {}
            for k, v in params.items():
                if v is None:
                    continue
                if isinstance(v, (list, tuple)):
                    cleaned[k] = ','.join(str(x) for x in v)
                else:
                    cleaned[k] = str(v)
            if cleaned:
                url = url + '?' + urllib.parse.urlencode(cleaned)
        return url

    def _build_headers(self) -> Dict[str, str]:
        return {
            'x-api-key':      self._api_key,
            'x-user-key':     self._user_key,
            'x-request-id':   self._request_id_factory(),
            'Accept':         'application/json',
        }

    def _redact(self, headers: Dict[str, str]) -> Dict[str, str]:
        """Headers safe for logging — credentials replaced with length only."""
        out = {}
        for k, v in headers.items():
            if k.lower() in ('x-api-key', 'x-user-key'):
                out[k] = f'<redacted:{len(v)}chars>'
            else:
                out[k] = v
        return out

    def _issue_get(self, path: str, params: Optional[Dict[str, Any]]) -> Any:
        url = self._build_url(path, params)
        attempt = 0
        last_transient: Optional[EtoroTransientError] = None
        last_rate_limit: Optional[EtoroRateLimitError] = None
        while attempt < self.max_retries:
            attempt += 1
            headers = self._build_headers()
            rid = headers['x-request-id']
            self._bucket.acquire(1.0)
            t0 = self._clock()
            try:
                status, resp_headers, body = self._transport(
                    url, headers, self.timeout_sec,
                )
            except EtoroTransientError as e:
                last_transient = e
                log.info(
                    '[ETORO] GET %s rid=%s transient_error=%s attempt=%d',
                    path, rid, e, attempt,
                )
                if attempt >= self.max_retries:
                    raise
                self._sleeper(self._backoff_seconds(attempt))
                continue
            elapsed_ms = int((self._clock() - t0) * 1000)
            log.info(
                '[ETORO] GET %s rid=%s status=%d latency_ms=%d',
                path, rid, status, elapsed_ms,
            )
            # 2xx success
            if 200 <= status < 300:
                try:
                    return json.loads(body.decode('utf-8') or 'null')
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    raise EtoroValidationError(
                        f'GET {path} returned non-JSON body: {e}')
            # 401 / 403 / 404 / 400-499 (other) — never retry
            if status in (401, 403):
                raise EtoroAuthError(
                    f'GET {path} {status} — credentials or scope not entitled')
            if status == 404:
                raise EtoroRouteError(f'GET {path} 404 — route not found')
            # 429 rate limit — retry with Retry-After
            if status == 429:
                retry_after = self._parse_retry_after(resp_headers)
                last_rate_limit = EtoroRateLimitError(
                    f'GET {path} 429 — rate limit exceeded',
                    retry_after=retry_after,
                )
                if attempt >= self.max_retries:
                    raise last_rate_limit
                self._sleeper(retry_after if retry_after > 0
                              else self._backoff_seconds(attempt))
                continue
            if 400 <= status < 500:
                raise EtoroValidationError(f'GET {path} {status} — client error')
            # 5xx — retry
            if 500 <= status < 600:
                last_transient = EtoroTransientError(
                    f'GET {path} {status} — server error')
                if attempt >= self.max_retries:
                    raise last_transient
                self._sleeper(self._backoff_seconds(attempt))
                continue
            raise EtoroError(f'GET {path} unexpected status {status}')
        # Loop exhausted — surface the most recent error
        if last_rate_limit is not None:
            raise last_rate_limit
        if last_transient is not None:
            raise last_transient
        raise EtoroTransientError(
            f'GET {path} exhausted {self.max_retries} retries')

    @staticmethod
    def _parse_retry_after(headers: Dict[str, str]) -> float:
        raw = headers.get('retry-after')
        if not raw:
            return 0.0
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _backoff_seconds(attempt: int) -> float:
        """Exponential: 2, 4, 8 seconds."""
        return float(2 ** attempt)
