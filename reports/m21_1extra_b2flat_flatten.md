# M21.1extra-B2flat — paper-only flatten proof

- data_source: **mock_broker_structural_proof**
- symbol: **AAA**
- account: **DUP623346**
- port: **4002**
- paper_asserted: **true**
- account_verified: **false**
- flatten_confirmed: **false**
- already_flat: **false**
- post_cancel_open_orders_cleared: **None**
- kill_switch_active: **false**
- close_order_placed: **false**
- cancelled_order_ids: **[]**
- entry_order_originated: **false**

> **B2flat performs ONLY paper cleanup: it cancels the target symbol's open orders (contract-aware) and places a single offsetting close for an existing paper position. It NEVER originates an entry order. flatten_confirmed is true only when the same-connection final proof (contract-aware openTrades + positions) shows the symbol genuinely flat — no target/ambiguous open trades and no residual position. It does not open a second IB connection.**

## Warnings
- mock_broker_structural_proof: real flatten proven on VPS with an operator-placed paper position
