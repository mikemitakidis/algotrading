# M13.1 — Order Schema Mapping (OrderIntent ↔ eToro REST)

**Status:** Documentation only. No code in this commit.

This document maps every field of the existing M10 `OrderIntent`
dataclass to the corresponding eToro REST request body field. Source
of truth: official OpenAPI specs at `https://api-portal.etoro.com/`.

## Source: M10 OrderIntent (current code)

```
@dataclass
class OrderIntent:
    signal_id:    int
    symbol:       str
    direction:    str           # 'long' | 'short'
    route:        str           # 'IBKR' | 'ETORO' | 'WATCH'
    entry_price:  float
    stop_loss:    float
    target_price: float
    valid_count:  int
    strategy_version: int
    created_at:   str
    position_size:    Optional[float]    # from RiskManager
    risk_usd:         Optional[float]    # from RiskManager
    risk_checks:      dict
```

## OPEN — POST /api/v1/trading/execution/market-open-orders/by-amount

eToro request body schema (verbatim from OpenAPI):

```
{
  "InstrumentID":   int,        # required
  "IsBuy":          bool,       # required
  "Leverage":       int,        # required
  "Amount":         float,      # required (USD)
  "StopLossRate":   float|null, # optional, absolute price
  "TakeProfitRate": float|null, # optional, absolute price
  "IsTslEnabled":   bool|null,  # optional, trailing stop
  "IsNoStopLoss":   bool|null,  # optional
  "IsNoTakeProfit": bool|null   # optional
}
```

### Field mapping

| eToro field      | OrderIntent source                        | Resolution / conversion                                                                                                                                          |
|------------------|--------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `InstrumentID`   | derived from `symbol`                      | Resolve via `GET /market-data/search?search=<symbol>` once at adapter init, then cache. Cache lives in-process; refresh on `KeyError` only. Never re-resolved per order. |
| `IsBuy`          | `direction`                                | `direction == 'long'` → `True`, `direction == 'short'` → `False`. Any other value rejected by pre-execution validation.                                          |
| `Leverage`       | constant `1` (M13.5 default)               | The bot does not use leverage in v1. Hard-coded to 1. Future leverage support is its own milestone, not M13.                                                     |
| `Amount`         | `position_size`                            | `position_size` is already the USD notional from `RiskManager`. **However:** if `RiskManager` produces GBP for IBKR and USD for eToro is required, a currency normalisation step is required. M13.1 decision: position_size is interpreted as USD when `route == 'ETORO'`. RiskManager must produce USD for ETORO route (deferred to M13.3 to enforce).  |
| `StopLossRate`   | `stop_loss`                                | Already an absolute price. Must be on the worse side of current price at submit time (eToro enforces this). Pre-execution validation must compare to current rate from `/market-data/instruments/rates` before sending.  |
| `TakeProfitRate` | `target_price`                             | Already an absolute price. Must be on the better side of current price at submit time. Same pre-execution validation. |
| `IsTslEnabled`   | not used in v1                             | Always omit (null). Trailing stops are a future feature.                                                                                                       |
| `IsNoStopLoss`   | derived: `stop_loss in (None, 0)`          | If the bot has no stop loss for an intent, set to `True`. Should not happen in production (the strategy always emits a stop).                                  |
| `IsNoTakeProfit` | derived: `target_price in (None, 0)`       | Same logic for take-profit absence.                                                                                                                            |

### Pre-execution validation rules (enforced before POST)

The adapter must validate ALL of these before submitting. A failure
produces `status='rejected'` with `rejection_reason='etoro_validation_<rule>'`
and the intent is NOT submitted.

1. **Direction valid.** `direction in ('long', 'short')`. Anything else rejected.
2. **Stop on correct side.** For long: `stop_loss < current_bid`. For short: `stop_loss > current_ask`. Read current rate from `/market-data/instruments/rates` ≤ 5s old.
3. **Target on correct side.** Symmetric: long needs `target_price > current_ask`, short needs `target_price < current_bid`.
4. **Amount within bounds.** eToro minimum trade size varies by instrument and is in the instrument metadata. Default minimum: $10 USD. Reject below this.
5. **InstrumentID resolved.** `symbol` must have resolved to a known `instrumentId` via cache or fresh search.
6. **Currency normalisation.** `Amount` is USD. If `position_size` came from a GBP-base risk calc, abort with `rejection_reason='etoro_currency_mismatch'` until M13.6 resolves cross-broker portfolio math.
7. **Market hours.** Some instruments are only tradable during their exchange hours. eToro returns 4xx in that case — the adapter still pre-checks via instrument metadata where available to avoid burning the 20/min write budget on guaranteed rejections.

### eToro response on success (200)

```
{
  "orderForOpen": {
    "instrumentID": int, "amount": int, "isBuy": bool,
    "leverage": int, "stopLossRate": int, "takeProfitRate": int,
    "isTslEnabled": bool, "mirrorID": int, "totalExternalCosts": int,
    "orderID": int,        # ← persisted to execution_intents.broker_order_id
    "orderType": int,
    "statusID": int,       # 0=pending, 1=executed, 2=cancelled, 3=rejected, 4=partial
    "CID": int,
    "openDateTime": iso8601,
    "lastUpdate": iso8601
  },
  "token": uuid             # idempotency / correlation token
}
```

`orderID` is stored as `execution_intents.broker_order_id` (TEXT, so the
int is serialised as a string — same as IBKR currently does). `token`
is recorded in `lifecycle_json` for support diagnostics. `statusID` is
mapped to our internal `status` via the table below.

## CLOSE — POST /api/v1/trading/execution/market-close-orders/positions/{positionId}

eToro request:

```
URL path: positionId (int64, required)
Body: {
  "InstrumentId":   int,      # required
  "UnitsToDeduct":  float     # optional. omit/null = full close.
}
```

### Field mapping for close

| eToro field      | Source                                                                          |
|------------------|----------------------------------------------------------------------------------|
| `positionId`     | From the open position's `positionID` — read via `/trading/info/portfolio` or from the `orderForOpen` response after the open executes. |
| `InstrumentId`   | Same as open. Resolved from cache.                                              |
| `UnitsToDeduct`  | Omit for full close (the common case). Partial close requires explicit operator action — not auto-emitted by the strategy in v1. |

A close order is logged as a separate `execution_intents` row with a
synthetic `signal_id` reference and an entry in `lifecycle_json` linking
back to the parent open intent. See `M13_1_lifecycle.md`.

## STATUS CHECK — GET /api/v1/trading/info/real/orders/{orderId}

Used to poll an open or close order's status after submission.

Response (key fields):

```
{
  "orderID": int,
  "statusID": int,     # 0=Pending, 1=Executed, 2=Cancelled, 3=Rejected, 4=PartiallyExecuted
  "errorCode": int|null,
  "errorMessage": str|null,
  "instrumentID": int,
  "amount": float,     # USD requested
  "units": float,      # quantity actually traded (when executed)
  "positions": [       # one or more, populated after execution
    {
      "positionID": int,    # ← persisted to execution_intents.broker_order_id (refined)
      "rate": float,        # ← persisted to execution_intents.fill_price
      "units": float,       # ← persisted to execution_intents.fill_qty
      "amount": float,
      "occurred": iso8601,  # ← persisted to execution_intents.filled_at
      "isOpen": bool
    }
  ]
}
```

The polling cadence is constrained by the 60 GET/min rate limit
(see `M13_1_design.md`). The adapter polls open orders at most every
5 seconds per cycle, batched. WebSocket subscription is a M13.5+
optimisation, not in scope here.

## eToro statusID → our `status` mapping

| eToro `statusID` | Meaning            | Our `status`                | Notes |
|------------------|--------------------|-----------------------------|------|
| 0                | Pending            | `accepted`                  | submitted to broker, awaiting fill |
| 1                | Executed           | `filled`                    | populate `fill_price`, `fill_qty`, `filled_at` from `positions[0]` |
| 2                | Cancelled          | `cancelled`                 | populate `cancelled_at` |
| 3                | Rejected           | `rejected`                  | populate `rejection_reason` from `errorMessage` |
| 4                | Partially Executed | `accepted` + lifecycle note | first partial fill recorded in lifecycle_json; final status set only after full execution or cancel |

This mapping uses ONLY the existing M10/M12 status values from
`INTENT_SCHEMA`. No new status values are required.

## Symbol → InstrumentID resolution strategy

The eToro API uses `instrumentId` (int) everywhere. The bot tracks
symbols (string). Resolution rules:

1. On adapter startup, the adapter resolves all symbols in the bot's
   focus list (~1200 symbols) via `/market-data/search`. Cached in
   memory as a `dict[str, int]`. Rate-limited at 60 GET/min, so
   bootstrap may take ~20 minutes. M13.3 will design a more efficient
   bulk-resolution path; M13.2 starts with this simple loop.
2. On `KeyError` mid-run (new symbol added to focus list), the adapter
   does a single search and adds to cache. Logged.
3. The cache is **never** persisted across process restarts in v1. This
   is conservative: instrument IDs are stable but not guaranteed
   immortal, and bootstrap-from-scratch every restart is the safest
   default. M13.5 may introduce persistent caching with versioning.

## Idempotency

eToro returns a `token` (UUID) in every open/close response. The
adapter records `token` in `lifecycle_json` for support diagnostics.
**The bot does NOT use this token for client-side idempotency.** Each
POST is treated as a unique submission; pre-execution risk checks
prevent duplicate submissions for the same intent_id.

If a network error occurs after POST but before response, the safest
recovery is to query `/trading/info/portfolio` and check whether a
matching new position appeared, NOT to retry the POST. Retry of a
failed POST is explicitly forbidden in the v1 design.

## Out of scope for M13.1

- The unit-based open variant (`/market-open-orders/by-units`) —
  v1 uses amount-based only. Adding units-based is a future task.
- Market-if-touched orders. eToro supports them but the bot doesn't
  use them in v1.
- Mirror trades / copy trading. The bot ignores `mirrors` and copy
  positions when reading portfolio state.
- Leverage > 1. Hard-coded to 1 in v1.
- Trailing stops. Always `IsTslEnabled=null`.
