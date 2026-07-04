# M21.1extra-D2 — Real D0 / D1 Gate Adapters (no orders)

Branch `m21-1extra-d2-gates-no-orders`, off main `0f1f97f`.

This is the gate-integration step of D2. It replaces the D2.0 dry-run's
**injected mock** gates with **real** adapters over the already-merged D0
authorization gate and D1 market-clock guard, behind the identical callable
shapes the dry-run state machine already consumes. It places no orders, opens
no gateway, writes no DB, and imports no broker code.

## Adapters

`bot/paper_loop_d2/gates.py`:

- `make_d0_gate(db_path=None, ttl_sec=None, now=None) -> d0_gate()` — wraps
  `runtime_policy.get_signal_only_reason("ibkr_paper", ...)`, which returns
  `(skip, reason)`. The gate returns `(allowed, reason)` with
  `allowed = (skip is False)`. `db_path`/`ttl_sec` are injected; tests use a
  temp DB and never touch the live `signals.db`. The adapter is read-only — it
  never mutates the policy.
- `make_d1_gate(sources=None, now_utc=None, max_age_sec=None, symbol=None)
  -> d1_gate()` — wraps `evaluate_market_clock(market_calendar_id="US_EQ",
  sources=...)`; returns `(decision.may_submit is True, decision.reason)`.
  Sources are injected; when omitted, a default `AlpacaClockSource()` is used,
  which itself fails closed on missing credentials / network problems, so no
  live credentials are required to build or import this gate.

Both adapters are additionally defensive: any exception inside the adapter
returns the blocking (`allowed=False` / `may_submit=False`) shape, so the state
machine records a blocked/failed cycle rather than raising.

## Safety matrix proven (temp DB / stubbed sources; no orders)

```
D0 authorized            -> allowed
D0 disabled              -> D0_BLOCKED
D0 expired               -> D0_BLOCKED (auto_trading_*_expired)
D0 malformed expiry      -> D0_BLOCKED (fail closed)
D0 kill switch           -> D0_BLOCKED (kill_switch_active_*)
D0 unreadable DB         -> D0_BLOCKED (fail closed)
D1 open                  -> may_submit
D1 closed                -> D1_BLOCKED (market_closed)
D1 unavailable           -> D1_BLOCKED (market_status_source_unavailable)
D1 stale                 -> D1_BLOCKED (market_status_source_stale)
D1 no credentials        -> D1_BLOCKED (market_status_no_credentials)
D1 malformed             -> D1_BLOCKED (market_status_source_malformed)
D0 allowed + D1 open + valid candidate -> DRY_RUN_WOULD_SUBMIT (no order)
D0 blocked short-circuits D1 (d1_checked == False)
```

The D0 adapter is proven not to mutate the policy. The state machine reaches
`DRY_RUN_WOULD_SUBMIT` only — there is no `ORDER_SUBMITTED` state in this
milestone; the real paper lifecycle wiring is the next D2 step.

## Scope

```
This step proves real D0/D1 gate adaptation and blocked-case handling only.
It does NOT place orders.
It does NOT open an IBKR gateway.
It does NOT call B2b/B2flat/C lifecycle primitives.
It does NOT write lifecycle rows.
It does NOT run a scheduler / unattended loop.
It does NOT touch the live signals.db (tests use a temp DB).
It does NOT require live Alpaca credentials or network (tests stub the source).
```

## Next in D2

The next step wires the real one-shot **paper** lifecycle by reusing the
existing B2b (`run_lifecycle`) + B2flat (`run_flatten`) + C
(`persist_lifecycle`) primitives, triggered only when both gates are
affirmative — still paper-only, no new broker path. Then the unattended
scheduler wrapper. Then D3 (dashboard). Then M21.1extra closes.
