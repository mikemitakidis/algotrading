# M21.1extra-B1 — IBKR paper contract DRY-RUN proof

- data_source: **simulated_fixture**
- dry_run_only: **true**
- real_broker_order_attempted: **false**
- ib_gateway_connection_attempted: **false**
- paper_port_expected: **4002**
- paper_account_expected: **DUP623346**

> **B1 is dry-run only. No real IBKR paper order was submitted. No IB Gateway connection was attempted. No IBKR gateway/network/submission path was used (the live scan does use the Alpaca market-data path). No broker_order_id exists. This proves contract construction only, not real submission.**
>
> B2 remains required for single real IBKR paper submission. B2 must include an explicit cleanup/cancel/flatten plan before approval.

## Summary

- candidates_in: **2**
- eligible_count: **2**
- dry_run_contracts_built: **2**
- submit_ready_count: **2**

## Dry-run contracts

| symbol | dir | route | entry | stop | target | qty | account | port | would_transmit | exec_elig | gate_passed |
|---|---|---|---|---|---|---|---|---|---|---|---|
| WINNER | long | IBKR | 100.0 | 95.0 | 115.0 | 10.0 | DUP623346 | 4002 | false | false | false |
| LOSER | long | IBKR | 50.0 | 48.0 | 56.0 | 10.0 | DUP623346 | 4002 | false | false | false |
