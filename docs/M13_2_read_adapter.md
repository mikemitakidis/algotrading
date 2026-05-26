# M13.2 — eToro Read Adapter

**Status:** Library code. Dormant in production runtime. Exercised
only by tests in M13.2.

## What this is

A read-only typed Python wrapper over six eToro Public API endpoints:

- `GET /me` (live-verified during M13 discovery)
- `GET /trading/info/portfolio` (live-verified)
- `GET /trading/info/real/pnl` (live-verified)
- `GET /market-data/search` (live-verified)
- `GET /trading/info/trade/history` (live-verified)
- `GET /market-data/instruments/rates` (docs-corrected from R4
  404 — verified path via the official OpenAPI spec, not live)

**Five live-verified endpoints plus one docs-corrected rates endpoint.**

## What this is NOT

- Not a `BrokerAdapter` subclass. It cannot place orders.
- Not registered in the broker factory. `BROKER=etoro_real` is not a
  recognised value in `main.py` in M13.2.
- Not called from `main.py`, `dashboard/app.py`, or any other runtime
  path.
- No `POST`, `DELETE`, `PUT`, or `PATCH` capability — the low-level
  client exposes exactly one method, `get()`.

## Usage (for future M13.3+ implementation only)

```
from bot.etoro.client import EtoroClient
from bot.etoro.read_adapter import EtoroReadAdapter

client  = EtoroClient(api_key=API_KEY, user_key=USER_KEY)
adapter = EtoroReadAdapter(client)

identity  = adapter.get_identity()
portfolio = adapter.get_portfolio()
real_pnl  = adapter.get_real_pnl()
matches   = adapter.search_instrument('AAPL')
rates     = adapter.get_rates([1001, 1002, 1003])
history   = adapter.get_trade_history(min_date='2025-01-01')
```

Credentials are passed in via the `EtoroClient` constructor. The
adapter package does NOT read `os.environ` or `os.getenv` anywhere —
callers are responsible for sourcing credentials (typically from
`.env` via the same loader the rest of the project already uses).

In M13.2 the only callers are the test suite. Production wiring is
deferred to M13.3+.

## Auth model

Every request sends three headers:

| Header | Source | Logged? |
|--------|--------|---------|
| `x-api-key` | constructor `api_key` | redacted (length only) |
| `x-user-key` | constructor `user_key` | redacted (length only) |
| `x-request-id` | fresh `uuid.uuid4()` per call | yes (for traceability) |

Credentials are never logged. The `_redact()` helper replaces
credential header values with `<redacted:Nchars>` in any debug
output.

## Rate limits

Per the official eToro docs, GET endpoints are limited to **60
requests per minute per user key**. The client implements a token
bucket sized to that limit, with:

- Injectable `clock` and `sleeper` so unit tests are deterministic
  (no real `time.sleep()`).
- Independent buckets per `EtoroClient` instance — matches eToro's
  per-user-key semantics.
- `get_rates()` automatically chunks instrument-ID lists into batches
  of ≤100 (eToro's per-call cap).

## Error model

Five typed exceptions, all subclasses of `EtoroError`:

| HTTP | Exception | Retried? |
|------|-----------|----------|
| 401 / 403 | `EtoroAuthError` | No |
| 404 | `EtoroRouteError` | No |
| 429 | `EtoroRateLimitError` (carries `retry_after`) | Yes, respects `Retry-After` |
| 4xx other | `EtoroValidationError` | No |
| 5xx / network / timeout | `EtoroTransientError` | Yes, exponential backoff (2s, 4s, 8s) |

Retry policy is conservative: deterministic 4xx errors are never
retried (they waste budget against the 60/min limit). Only 5xx and
429 retry, both with backoff.

## Testing

Run: `python3 test_m13_2_etoro_read.py`

42 tests, all mocked, ~40ms total runtime. The test suite injects a
fake transport and a virtual clock — the live eToro API is never
called from tests, ever.
