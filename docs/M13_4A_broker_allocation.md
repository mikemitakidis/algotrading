# M13.4A — Dashboard Broker Allocation + Budget Controls

Status: implemented.
Scope: configuration surface only. **Not** live trading. **Not** wired into `main.py`.
The policy persists; the M13.5 live writer (future) will consult
`is_auto_trading_allowed()` before any POST.

## What this adds

1. `bot/broker_allocation.py` — policy module:
   - `DEFAULT_POLICY` (version 1)
   - `validate_policy(policy) -> ValidationResult`
   - `load_policy(conn) -> dict`
   - `save_policy(conn, policy) -> None`
   - `is_broker_allowed(policy, broker_name) -> bool`
   - `is_auto_trading_allowed(policy, broker_name) -> (bool, reason)`
2. Two dashboard endpoints (auth-protected, same `@require_auth` pattern as
   the M14 risk endpoints):
   - `GET  /api/broker-allocation`
   - `POST /api/broker-allocation`
3. A dashboard panel under the **Risk** page titled
   *Broker Allocation & Budget Controls (M13.4A)* with sections:
   - Global
   - IBKR
   - eToro
   - Routing
4. `test_m13_4a_allocation.py` — 61 tests covering validator, persistence,
   read helpers, and endpoint auth/persistence/rejection paths.

## Persistence

The policy is stored as one JSON row in the existing
`portfolio_risk_state` KV table (key `broker_allocation_policy`).
**No DB schema change.** `load_policy()` creates the table if missing,
which mirrors the idempotent behaviour of `bot.flywheel.ensure_schema()`.

## Policy shape

```jsonc
{
  "version": 1,
  "global": {
    "auto_trading_enabled": false,
    "max_auto_trading_capital": 0.0,
    "kill_switch": false
  },
  "ibkr": {
    "auto_trading_enabled": false,
    "max_auto_trading_capital": 0.0,
    "max_single_trade_amount": 0.0,
    "max_daily_loss": 0.0,
    "max_open_positions": 0,
    "kill_switch": false
  },
  "etoro": {
    "auto_trading_enabled": false,
    "max_auto_trading_capital": 0.0,
    "max_single_trade_amount": 0.0,
    "max_daily_loss": 0.0,
    "max_open_positions": 0,
    "kill_switch": false
  },
  "routing": {
    "default_broker": "paper",
    "route_overrides": { "IBKR": "ibkr_live", "ETORO": "etoro_paper" },
    "allowed_brokers": ["paper", "ibkr_paper", "ibkr_live", "etoro_paper"],
    "etoro_live_enabled": false
  }
}
```

## Validation rules (server-side; this is the safety gate)

Every rule is enforced in `validate_policy()`. The client-side UI is
helpful but not authoritative.

| Rule                                                                    | Error code                     |
|-------------------------------------------------------------------------|--------------------------------|
| Top-level keys must be exactly `{version, global, ibkr, etoro, routing}` | `unknown_key` / `missing_key`  |
| `version` must equal `1`                                                | `version_mismatch`             |
| All money fields must be non-negative numbers                           | `value_error`                  |
| `max_open_positions` must be `int >= 0`                                 | `value_error`                  |
| Boolean fields must be real `bool` (not `"true"`, not `1`)              | `type_error`                   |
| Per broker: `max_single_trade_amount <= max_auto_trading_capital`       | `single_trade_exceeds_capital` |
| When global cap > 0: `broker.max_auto_trading_capital <= global.max_auto_trading_capital` | `exceeds_global_capital` |
| `routing.default_broker` must be in `routing.allowed_brokers`           | `not_in_allowed`               |
| `routing.allowed_brokers` may only contain `paper`, `ibkr_paper`, `ibkr_live`, `etoro_paper` | `unknown_broker` |
| `etoro_real` must never appear in `allowed_brokers`, `default_broker`, or `route_overrides` | `forbidden_broker` |
| `routing.etoro_live_enabled = true` is rejected in M13.4A               | `etoro_live_forbidden`         |

Validation errors are returned as a list of
`{"path": "...", "code": "...", "msg": "..."}` objects with HTTP 400.

## Read helpers (for M13.5+)

`is_auto_trading_allowed(policy, broker_name)` returns
`(False, reason)` for each blocker, in order:

1. policy missing/malformed -> `policy_missing`
2. `global.kill_switch == true` -> `global_kill_switch`
3. `global.auto_trading_enabled != true` -> `global_disabled`
4. `broker_name == "etoro_real"` while `etoro_live_enabled` is false
   -> `etoro_live_disabled`
5. broker not in `routing.allowed_brokers` -> `broker_not_allowed`
6. broker block missing -> `broker_block_missing`
7. `<broker>.kill_switch == true` -> `broker_kill_switch`
8. `<broker>.auto_trading_enabled != true` -> `broker_disabled`

`paper` has no broker block and is gated only by the global checks.

## What this does NOT change

- `main.py`
- `bot/risk.py`
- `bot/flywheel.py` schema (table is reused, **not** altered)
- M15 modules (`bot/gateway_watchdog.py`, `bot/recovery_executor.py`,
  `bot/heartbeat.py`)
- `bot/etoro/*`
- `bot/brokers/*`
- Any live trading code path
- `.env.example`
- No new pip dependencies

## What is left for future milestones

- **M13.4B** — design doc only for the live write path.
- **M13.5** — live eToro write implementation. Must call
  `is_auto_trading_allowed()` and the per-broker caps **before** any POST.
  Enabling `etoro_live_enabled` will require lifting the M13.4A guard.
- **M13.6** — cross-broker risk pool. `daily_state` schema decision
  (broker_mode column vs sibling table vs composite key) still parked.

## M13.4A.1 — UX polish (no policy change)

Pure UI refinement of the Broker Allocation panel. **Policy schema,
validation rules, persistence, and endpoint behaviour are unchanged.**
DOM input IDs are preserved (`ba_g_*`, `ba_i_*`, `ba_e_*`, `ba_r_*`,
`data-ba-allowed`), so `_baReadForm()` and the test suite are untouched.

Visible changes:
- Card-style layout (header bar + body) for Global / IBKR / eToro / Routing
- Coloured status badges on each card: `ENABLED` (green) / `DISABLED` (grey) /
  `⚠ KILL SWITCH ACTIVE` (red)
- "Effective Status" summary at the top of the panel showing Global / IBKR /
  eToro states. Broker effective state factors in the global state — a broker
  shows DISABLED if global is disabled, even if the broker's own toggle is on.
- Kill-switch toggles now render as a distinct red-bordered control with an
  explicit `⚠ KILL SWITCH ACTIVE` label when on
- Money inputs use a `$`-prefixed input group; numeric labels no longer
  carry the `($)` suffix
- Short helper text under each field explaining what it controls
- Yellow warning banner when all three capital caps (global, IBKR, eToro)
  are `$0.00`
- Allowed-broker chips show a green outline when selected
- `eToro live enabled` rendered as a clearly locked red pill
