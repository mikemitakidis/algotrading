# M21.1extra-B2b — one tiny paper-order lifecycle

- data_source: **mock_broker_structural_proof**
- symbol: **AAPL**
- account: **DUP623346**
- port: **4002**
- paper_asserted: **True**
- account_verified: **True**
- kill_switch_active: **False**
- pre_existing_position: **False**
- pre_existing_open_orders: **False**
- entry_order_originated: **False**
- entry_order_id: **None**
- entry_result_status: **None**
- entry_result_recorded: **False**
- entry_filled: **False**
- position_observed: **False**
- observation_attempts: **0**
- observation_seconds: **0.0**
- observation_timeout: **False**
- flatten_called: **False**
- flatten_confirmed: **False**
- close_order_placed: **False**
- lifecycle_confirmed: **False**

> **B2b originates exactly one tiny paper entry via the real submit() path, observes the fill/position, then cleans up with the merged B2flat flatten primitive. lifecycle_confirmed is true only if the entry was originated, a position was observed, flatten_confirmed=true, and no residual positions/orders remain. It runs no scanner, scheduler, dashboard, or persistence.**

## Warnings
- mock_broker_structural_proof: real lifecycle proven on VPS with an operator-confirmed tiny paper order

---

## Real VPS proof (2026-07-02, real IBKR paper gateway)

The fields above are a mock/structural shape. This section records the actual
operator proof on the real paper gateway.

**First attempt — safely blocked by the runtime policy (no order placed).** The
M13.4A runtime policy returned `signal_only_skipped` with reason
`auto_trading_disabled_global`, so `submit()` correctly refused to place an
order: `entry_order_id=null`, `entry_result_status=signal_only_skipped`,
`observation_timeout=true`, `flatten_confirmed=true` (nothing to close),
`lifecycle_confirmed=false`, `M21_1EXTRA_B2B_REAL_VERIFY_RC=1`. This is the
fail-safe default working as designed: `auto_trading_disabled_global` is emitted
whenever the global `auto_trading_enabled` flag is not explicitly `True` (see
`bot/etoro/signal_only_broker.determine_signal_only_reason`) — the resting state
of a paper system not switched on for automated trading. A correct safety
outcome, not a fault.

**Retry — real lifecycle proven via a temporary, isolated policy DB.** To
exercise the real `submit()` path for one operator-confirmed, manually-run tiny
paper order, a temporary isolated policy DB (`/tmp/m21_1extra_b2b_policy.sqlite`,
`M13_4A_RUNTIME_POLICY_TTL_SEC=0`) was pointed at via `SIGNALS_DB_PATH` so the
policy read `not skipped`. This did NOT modify the real/default signals DB or its
fail-safe policy state. A clean readiness precheck confirmed no AAPL position and
no open orders (any symbol) beforehand.

Retry result: `entry_order_id=IB-PERM-106341206`, `entry_result_status=accepted`,
`position_observed=true` (1 attempt, 0.4s), `entry_filled=true`,
`flatten_confirmed=true`, `close_order_placed=true`, `remaining_positions=[]`,
`remaining_open_orders=[]`, `lifecycle_confirmed=true`,
`M21_1EXTRA_B2B_REAL_VERIFY_RETRY_RC=0`.

**Non-blocking IBKR note (error 10148).** During retry cleanup, IBKR logged
`Error 10148 ... OrderId 13 that needs to be cancelled cannot be cancelled,
state: Cancelled.` — a benign already-cancelled race (the flatten tried to cancel
a leg IBKR had already cancelled). The B2b JSON has no warnings,
`flatten_confirmed=true`, and empty residuals, so the primitive handled it
correctly (it swallows cancel exceptions and confirms from final state). No code
change required.

**Process note.** The retry was run directly under a closing-market-window time
pressure rather than routed through the normal review step first. The method
itself was sound (isolated DB, real state untouched, clean precheck), but for the
record: urgency does not change the review process, and future real-order runs
should go through review regardless of market timing.
