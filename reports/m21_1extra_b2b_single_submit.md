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
