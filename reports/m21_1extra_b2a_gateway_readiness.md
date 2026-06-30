# M21.1extra-B2a — IBKR PAPER gateway readiness

- data_source: **mock_broker_structural_proof**
- real_ibkr_gateway_connected: **false**
- vps_gateway_proof_required: **true**
- mode: **readiness**
- paper_mode_asserted: **true**
- connected: **true**
- account_verified: **true**
- connection_status_checked: **true**
- reconcile_succeeded: **true**
- positions_read_succeeded: **true**
- account: **DUP623346**
- port: **4002**
- kill_switch_active: **false**
- flatten_capability: **not_available_in_current_adapter**
- order_origination_attempted: **false**
- broker_submit_attempted: **false**
- order_result_created: **false**
- cancel_requested: **false**
- cancel_attempted: **false**
- cancel_confirmed: **None**

> **B2a is read-only readiness. Our code originated no order, attempted no broker submission, built no bracket, and created no OrderResult. The only optional mutation is cancelling exactly one operator-supplied order id, behind an explicit confirmation flag.**
>
> **Cleanup finding: no safe paper-only flatten/close-position primitive exists in the current adapter. Therefore a market-entry bracket in B2b is NOT yet safe to approve — B2b needs either a reviewed flatten primitive or a redesign to a cancel-before-fill order type.**

## Open orders (0)

## Positions (0)
