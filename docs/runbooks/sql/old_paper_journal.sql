-- OP1.6b  WS2/D2 loop journal (paper_lifecycles.db COPY), read-only. data_source separates simulation from IBKR paper.
.print '== 1/3/4/5/6. loop orders by data_source and state (simulated vs IBKR-paper claims)'
SELECT data_source, state, COUNT(*) AS n, MIN(planned_at_utc) AS first_utc, MAX(planned_at_utc) AS last_utc,
       SUM(broker_perm_id IS NOT NULL) AS with_perm_id, SUM(entry_fill_price IS NOT NULL) AS with_entry_fill,
       SUM(exit_fill_price IS NOT NULL) AS with_exit_fill, SUM(entry_fees IS NOT NULL OR exit_fees IS NOT NULL) AS with_fees
FROM loop_orders GROUP BY 1, 2 ORDER BY 1, 2;
.print '== 7/8. closed-trade outcomes by source (fees as recorded by our code; statement is the proof)'
SELECT data_source, record_kind, is_edge_outcome, COUNT(*) AS n, SUM(fees IS NOT NULL) AS with_fees,
       ROUND(SUM(gross_pnl), 2) AS gross_pnl, ROUND(SUM(realized_pnl), 2) AS realized_pnl,
       MIN(exit_filled_at_utc) AS first_exit_utc, MAX(exit_filled_at_utc) AS last_exit_utc
FROM paper_trade_outcomes GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;
