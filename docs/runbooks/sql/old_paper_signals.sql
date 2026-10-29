-- OP1.6a  Old paper system since 1 June 2026 — signals.db COPY, read-only (sqlite3 -readonly).
-- A row is a CLAIM made by our own code, never proof of execution. Proof = the IBKR statement (OP1.7).
-- The nine categories are reported separately, no section adds one category to another.
.print '== A. tables present'
SELECT name FROM sqlite_master WHERE type='table' AND name IN
  ('signals','candidate_snapshots','execution_intents','signal_outcomes','risk_decisions','broker_positions',
   'daily_state') ORDER BY name;
.print '== 2. CANDIDATE SIGNALS (scanner output; no order implied)'
SELECT direction, COALESCE(NULLIF(route,''),'-') AS route, strategy_version, COUNT(*) AS n,
       MIN(timestamp) AS first_utc, MAX(timestamp) AS last_utc
FROM signals WHERE timestamp >= '2026-06-01' GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;
SELECT stage, COUNT(*) AS n, MIN(timestamp) AS first_utc, MAX(timestamp) AS last_utc
FROM candidate_snapshots WHERE timestamp >= '2026-06-01' GROUP BY 1 ORDER BY 1;
.print '== 3. ORDER INTENTS by broker and status (what our code decided to attempt)'
SELECT broker, status, COUNT(*) AS n, MIN(timestamp) AS first_utc, MAX(timestamp) AS last_utc
FROM execution_intents WHERE timestamp >= '2026-06-01' GROUP BY 1, 2 ORDER BY 1, 2;
.print '== 1/4/5/6/9. classification of intents (claims only)'
SELECT CASE
    WHEN lower(broker) LIKE '%live%' OR lower(broker) IN ('etoro_real','etoro_live') THEN '9_LIVE_SCOPE_ATTEMPT_ESCALATE'
    WHEN status = 'paper_logged' OR lower(broker) = 'paper' THEN '1_LOCAL_SIMULATION_NO_BROKER'
    WHEN lower(broker) LIKE 'ibkr%' AND status = 'filled' AND fill_price IS NOT NULL THEN '6_EXECUTION_CLAIMED_NEEDS_STATEMENT'
    WHEN lower(broker) LIKE 'ibkr%' AND COALESCE(broker_order_id,'') <> '' THEN '5_BROKER_ACK_CLAIMED (order id recorded)'
    WHEN lower(broker) LIKE 'ibkr%' AND submitted_at IS NOT NULL THEN '4_SUBMISSION_REPORTED_NO_BROKER_ID'
    WHEN status IN ('risk_rejected','broker_unready','live_safety_blocked','account_mismatch',
                    'connection_failed','not_implemented') THEN '3b_INTENT_NOT_SUBMITTED'
    ELSE '0_UNCLASSIFIED_REVIEW' END AS category,
  broker, status, COUNT(*) AS n, MIN(timestamp) AS first_utc, MAX(timestamp) AS last_utc
FROM execution_intents WHERE timestamp >= '2026-06-01' GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;
.print '== 5/6. broker identity evidence recorded by our code (counts only, no values)'
SELECT broker, status, COUNT(*) AS n,
       SUM(COALESCE(broker_order_id,'') <> '') AS with_broker_order_id,
       SUM(submitted_at IS NOT NULL) AS with_submitted_at,
       SUM(fill_price IS NOT NULL) AS with_fill_price, SUM(filled_at IS NOT NULL) AS with_filled_at,
       SUM(lifecycle_json LIKE '%permId%' OR lifecycle_json LIKE '%perm_id%') AS mentions_perm_id,
       SUM(lifecycle_json LIKE '%execId%' OR lifecycle_json LIKE '%exec_id%') AS mentions_exec_id,
       SUM(lifecycle_json LIKE '%commission%') AS mentions_commission
FROM execution_intents WHERE timestamp >= '2026-06-01' GROUP BY 1, 2 ORDER BY 1, 2;
.print '== 7. CLOSED PAPER TRADES? signal_outcomes are PRICE-PATH outcomes, NOT fills'
SELECT COALESCE(o.resolution_method,'-') AS method, COALESCE(o.outcome,'-') AS outcome,
       (o.intent_id IS NOT NULL) AS linked_to_intent, COALESCE(i.broker,'-') AS intent_broker,
       COALESCE(i.status,'-') AS intent_status, COUNT(*) AS n
FROM signal_outcomes o LEFT JOIN execution_intents i ON i.id = o.intent_id
WHERE COALESCE(o.resolved_at,'') >= '2026-06-01' OR o.outcome = 'OPEN'
GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 2, 3, 4, 5;
.print '== 8. FEES / COMMISSIONS: this schema stores none; commissions come only from the statement (OP1.7)'
.print '== 9. LIVE-SCOPE activity recorded by our code (positions fetched and M14 decisions per scope)'
SELECT broker_scope, COUNT(DISTINCT exposure_batch_id) AS batches, MIN(fetched_at_utc) AS first_utc,
       MAX(fetched_at_utc) AS last_utc FROM broker_positions WHERE date >= '2026-06-01' GROUP BY 1 ORDER BY 1;
SELECT p.broker_scope, p.symbol, p.side, p.qty, ROUND(p.exposure_usd, 2) AS exposure_usd, p.fetched_at_utc
FROM broker_positions p JOIN (SELECT broker_scope, MAX(fetched_at_utc) AS m FROM broker_positions GROUP BY 1) l
  ON l.broker_scope = p.broker_scope AND l.m = p.fetched_at_utc ORDER BY 1, 2;
SELECT broker_scope, requested_action, result, source, COUNT(*) AS n, MIN(taken_at) AS first_utc,
       MAX(taken_at) AS last_utc FROM risk_decisions WHERE taken_at >= '2026-06-01' GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4;
.print '== 10. CURRENT M14 EXPOSURE AND P&L EVIDENCE (what M14 is collecting now; our records, not the statement)'
SELECT broker_scope, date, source, daily_pnl_source, daily_pnl_available, ROUND(realised_pnl_usd, 2) AS realised_pnl_usd,
       ROUND(realised_daily_loss, 2) AS realised_daily_loss, open_positions, ROUND(capital_deployed, 2) AS capital_deployed,
       ROUND(drawdown_from_peak, 4) AS drawdown_from_peak, daily_loss_block_active, fresh_reads_count, last_ingested_at
FROM daily_state_per_broker WHERE date >= date('now', '-14 day') ORDER BY broker_scope, date;
SELECT source, policy_version, COUNT(*) AS snapshots, MIN(taken_at) AS first_utc, MAX(taken_at) AS last_utc
FROM risk_snapshots WHERE taken_at >= '2026-06-01' GROUP BY 1, 2 ORDER BY 1, 2;
SELECT broker_scope, result, COUNT(*) AS n, MAX(taken_at) AS last_utc
FROM risk_decisions WHERE taken_at >= date('now', '-7 day') GROUP BY 1, 2 ORDER BY 1, 2;
