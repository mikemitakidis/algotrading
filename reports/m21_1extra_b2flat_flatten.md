# M21.1extra-B2flat — paper-only flatten proof

- data_source: **mock_broker_structural_proof**
- symbol: **AAA**
- flatten_confirmed: **false**
- kill_switch_active: **false**
- close_order_placed: **false**
- cancelled_order_ids: **[]**
- entry_order_originated: **false**

> **B2flat performs ONLY paper cleanup: it cancels the target symbol's open orders and places a single offsetting close for an existing paper position. It NEVER originates an entry order. flatten_confirmed is true only when the post-action reconcile shows the symbol genuinely flat (no position AND no open orders).**

## Warnings
- mock_broker_structural_proof: real flatten proven on VPS with an operator-placed paper position
