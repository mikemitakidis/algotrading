# M21.1extra-A — run-once simulation paper loop (read-only proof)

- data_source: **simulated_fixture**
- simulation_only: **true**
- real_broker_order_attempted: **false**
- profile: **RESEARCH** (execution_eligible False, gate not passed)

> Simulation only. No real broker, no real money, no eToro, no Telegram, no scheduler. Orders/fills are produced by the frozen M20 simulate_paper_fill. Research-paper eligibility never sets execution_eligible or hard_gate_passed and never edits M19/M20 truth fields.

## Summary

- signals_in: **2**
- scored_count: **2**
- research_paper_eligible_count: **2**
- simulated_orders: **2**
- simulated_fills: **2**
- opened_positions: **2**
- closed_positions: **2**
- wins: **1**
- losses: **1**
- average_win: **3000.0**
- average_loss: **-640.0**
- win_loss_ratio: **4.6875**
- max_drawdown: **not_available_in_A**

## Per-signal outcomes

| symbol | stage | eligible | exit | realized_pnl | r_multiple |
|---|---|---|---|---|---|
| WINNER | closed | true | TP | 3000.0 | 3.0 |
| LOSER | closed | true | SL | -640.0 | -1.0 |
