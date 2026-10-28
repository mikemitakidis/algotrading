-- OP1.6a  Old paper system since 1 June 2026 — signals.db COPY, read-only (sqlite3 -readonly).
-- A row is a CLAIM made by our own code, never proof of execution.
.print '== A. tables present'
SELECT name FROM sqlite_master WHERE type='table' AND name IN
  ('signals','execution_intents','signal_outcomes','risk_decisions','broker_positions','daily_state') ORDER BY name;
.print '== B. intents by broker and status'
SELECT broker, status, COUNT(*) AS n, MIN(timestamp) AS first_utc, MAX(timestamp) AS last_utc,
       SUM(COALESCE(broker_order_id,'') <> '') AS with_broker_order_id,
       SUM(submitted_at IS NOT NULL) AS with_submitted_at,
       SUM(fill_price IS NOT NULL) AS with_fill_price, SUM(filled_at IS NOT NULL) AS with_filled_at
FROM execution_intents WHERE timestamp >= '2026-06-01' GROUP BY broker, status ORDER BY broker, status;
.print '== C. classification (claims only)'
SELECT CASE
    WHEN lower(broker) LIKE '%live%' OR lower(broker) IN ('etoro_real','etoro_live') THEN '1_LIVE_ACCOUNT_SCOPE_ESCALATE'
    WHEN status = 'paper_logged' OR lower(broker) = 'paper' THEN '2_LOCAL_SIMULATION_NO_BROKER'
    WHEN lower(broker) LIKE 'ibkr%' AND status = 'filled' AND fill_price IS NOT NULL THEN '3_BROKER_PAPER_FILL_CLAIMED'
    WHEN lower(broker) LIKE 'ibkr%' AND COALESCE(broker_order_id,'') <> '' THEN '4_BROKER_PAPER_SUBMITTED'
    WHEN status IN ('risk_rejected','broker_unready','live_safety_blocked','account_mismatch',
                    'connection_failed','not_implemented') THEN '5_NOT_SUBMITTED'
    ELSE '6_UNCLASSIFIED_REVIEW' END AS class,
  broker, COUNT(*) AS n, MIN(timestamp) AS first_utc, MAX(timestamp) AS last_utc
FROM execution_intents WHERE timestamp >= '2026-06-01' GROUP BY 1, 2 ORDER BY 1, 2;
.print '== D. broker identity evidence inside lifecycle_json (counts only, no values)'
SELECT broker, status, COUNT(*) AS n,
       SUM(lifecycle_json LIKE '%permId%' OR lifecycle_json LIKE '%perm_id%') AS mentions_perm_id,
       SUM(lifecycle_json LIKE '%execId%' OR lifecycle_json LIKE '%exec_id%') AS mentions_exec_id,
       SUM(lifecycle_json LIKE '%commission%') AS mentions_commission
FROM execution_intents WHERE timestamp >= '2026-06-01' GROUP BY broker, status ORDER BY broker, status;
.print '== E. signal_outcomes = price-path outcomes, NOT fills'
SELECT COALESCE(o.resolution_method,'-') AS method, COALESCE(o.outcome,'-') AS outcome,
       (o.intent_id IS NOT NULL) AS linked_to_intent, COALESCE(i.broker,'-') AS intent_broker,
       COALESCE(i.status,'-') AS intent_status, COUNT(*) AS n
FROM signal_outcomes o LEFT JOIN execution_intents i ON i.id = o.intent_id
WHERE COALESCE(o.resolved_at,'') >= '2026-06-01' OR o.outcome = 'OPEN'
GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 2, 3, 4, 5;
.print '== F. broker position snapshots by scope (open positions as last fetched)'
SELECT broker_scope, COUNT(DISTINCT exposure_batch_id) AS batches, MIN(fetched_at_utc) AS first_utc,
       MAX(fetched_at_utc) AS last_utc FROM broker_positions WHERE date >= '2026-06-01' GROUP BY 1 ORDER BY 1;
SELECT p.broker_scope, p.symbol, p.side, p.qty, ROUND(p.exposure_usd, 2) AS exposure_usd, p.fetched_at_utc
FROM broker_positions p JOIN (SELECT broker_scope, MAX(fetched_at_utc) AS m FROM broker_positions GROUP BY 1) l
  ON l.broker_scope = p.broker_scope AND l.m = p.fetched_at_utc ORDER BY 1, 2;
.print '== G. M14 risk decisions by scope'
SELECT broker_scope, requested_action, result, source, COUNT(*) AS n, MIN(taken_at) AS first_utc,
       MAX(taken_at) AS last_utc FROM risk_decisions WHERE taken_at >= '2026-06-01' GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4;
