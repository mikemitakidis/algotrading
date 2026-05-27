# M13.3 — PaperEtoroBroker (dry-run, no live writes)

**Status:** Library code. `BROKER=etoro_paper` activates it as the
selected broker for the bot. No live eToro writes possible.

## Guarantees

This commit upholds three hard contracts:

### 1. No write capability anywhere in `bot/etoro/`

The M13.2 AST proof is extended to cover `paper_broker.py`,
`schema_validator.py`, and `instrument_cache.py`. No file in
`bot/etoro/` contains:

- A function or method named `post`/`delete`/`put`/`patch`
- A `subprocess` / `urlopen` call with `method='POST'` (or `DELETE`/`PUT`/`PATCH`)
- A `'POST'`/`'DELETE'`/`'PUT'`/`'PATCH'` string literal in executable code

Behaviourally: poisoning `urllib.request.urlopen` to raise on call,
then running `PaperEtoroBroker.submit()`, succeeds — proving the
paper broker never reaches the network.

### 2. No duplicate `execution_intents` rows

`PaperEtoroBroker.submit()`:
- Returns `OrderResult` only.
- Does NOT call `log_intent()` or `update_intent_status()` — neither
  via import nor via attribute access. AST-enforced.

The existing `main.py` execution loop is the SOLE writer to
`execution_intents`. The paper broker writes a supplemental JSONL
audit at `data/paper_etoro_orders.jsonl` (same role as
`data/paper_orders.jsonl` for the existing `PaperBroker`). Different
file, different schema, different purpose — not a duplicate of
`execution_intents`.

### 3. eToro schema validation failures are NOT `risk_rejected`

A failure to validate the eToro POST body is a broker/payload error,
not a portfolio risk rejection. `PaperEtoroBroker.submit()` returns:

| Outcome | `OrderResult.status` | `OrderResult.reason` |
|---|---|---|
| Valid intent | `paper_logged` | M13.3 dry-run message |
| Schema validation failure | `rejected` | `etoro_validation_<rule>` |
| Internal exception | `error` | `paper_etoro_internal_error:<class>` |

`risk_rejected` is reserved for portfolio risk failures from
`RiskManager` / `PortfolioRiskPolicy`. The paper broker NEVER emits it.

Validation reason codes (from `bot/etoro/schema_validator.py`):

| Code | Trigger |
|---|---|
| `etoro_validation_direction` | direction not 'long' or 'short' |
| `etoro_validation_currency` | position_size missing or non-positive |
| `etoro_validation_min_amount` | position_size below configured USD minimum |
| `etoro_validation_unresolved_symbol` | symbol → instrumentId not resolved |
| `etoro_validation_no_stop` | stop_loss missing or zero |
| `etoro_validation_no_rate` | no current rate available for side checks |
| `etoro_validation_stop_side` | stop_loss on wrong side of bid/ask |
| `etoro_validation_target_side` | target_price on wrong side of bid/ask |
| `etoro_validation_leverage` | leverage other than 1 (v1 hard-coded) |

## Activation

```
BROKER=etoro_paper
```

`BROKER=etoro_real` is **explicitly rejected** with a loud `ValueError`
(per ChatGPT correction). It does NOT silently fall back to paper —
that would have been unsafe.

## Wiring

`PaperEtoroBroker` takes everything via constructor injection:

- `read_adapter`: optional `EtoroReadAdapter`. If provided, used for
  symbol resolution (one GET per new symbol) and rate snapshots.
- `instrument_cache`: optional preloaded `InstrumentCache`. Tests pass
  a preloaded one to avoid any network call.
- `rates_provider`: optional `Callable[[int], Rate]`. Tests inject a
  static rate; production callers usually leave this `None` and let
  the `read_adapter.get_rates` path handle it.
- `audit_file_path`: where supplemental JSONL audit lines are written.
  Defaults to `data/paper_etoro_orders.jsonl`.
- `min_amount_usd`: configurable minimum trade size, default 10.

In M13.3 production runtime, `BROKER=etoro_paper` resolves the broker
via `bot/brokers/__init__.py` factory with default constructor
arguments (no read_adapter wired). That means symbol resolution will
return `None` for any non-preloaded symbol, and rate lookups will be
`None`. Most intents will therefore be `rejected` with
`etoro_validation_unresolved_symbol` or `etoro_validation_no_rate`
— which is the SAFE default for M13.3. A future milestone may wire
in the read adapter when configuration allows credentials.

## Where the M13.3 broker DOESN'T fit

`PaperEtoroBroker.submit()` only validates and logs. It does NOT:

- Place real eToro orders (M13.5)
- Cancel orders
- Close positions on eToro
- Read live portfolio state into `bot/risk.py`
- Handle currency conversion (M13.6 cross-broker risk pool)
- Touch the M15 gateway watchdog (that's IBKR-specific)
- Write to `execution_intents`

The bot's existing `main.py` flow remains the SOLE writer to
`execution_intents`, with exactly one row per submitted intent —
identical to the existing IBKR and paper flow.

## Tests

`python3 test_m13_3_etoro_paper.py` — 48 tests, ~30ms, all mocked.

The M13.2 read-adapter test suite (`test_m13_2_etoro_read.py`) was
updated in the same commit to reflect the new factory state:
`BROKER=etoro_paper` is now registered; `BROKER=etoro_real` still
must fail loudly. M13.2 still passes 42/42.
