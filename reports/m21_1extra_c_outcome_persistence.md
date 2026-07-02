# M21.1extra-C — paper-lifecycle outcome persistence

- data_source: **mock_broker_structural_proof**
- table: **paper_lifecycles** (append-only, idempotent)
- record_kind: **mechanical_paper_lifecycle**
- is_edge_outcome: **0**
- exchange_timezone: **America/New_York**
- market_calendar_id: **US_EQ** (identity only; D maps to a calendar)
- total_lifecycles: **0**
- lifecycle_confirmed_count: **0**

> **C persists B2b-style MECHANICAL paper-lifecycle records (immediate-flatten) into a dedicated append-only table. These are NOT hold-to-exit P&L edge outcomes. Timestamps are persist-time only (timestamp_source=c_persist_time_only, event_timestamps_available=false); C does not claim to know exact broker submit/observe/flatten instants. market_session_date is derived from the UTC instant using the record's exchange_timezone via zoneinfo (DST-correct), default America/New_York for US_EQ, never a fixed offset. C does not schedule, hold trades open, or touch the dashboard — the market-clock guard is deferred to D (market_clock_checked=false).**

> **C does NOT check public holidays, early closes, weekends, lunch breaks, or market-open status. Those checks are explicitly deferred to D. C only stores DST-safe UTC timestamps, market identity fields (market_calendar_id, exchange_timezone), and a session date derived from the record's exchange_timezone — so D can enforce a real per-exchange market-open guard.**

---

## Real VPS proof (2026-07-02, persistence-only — no order, no gateway)

The fields above are a mock/structural summary. This section records the actual
light VPS proof of the persistence layer at commit
`07ca042244b099b6a314135576bc88c128cef717`. C places no orders and needs no
gateway; the proof only reads a B2b JSON and writes/reads a SQLite DB under
`/tmp`.

**Source.** The real B2b result JSON from the B2b proof was used:
`/tmp/m21_1extra_b2b_AAPL_retry.json` (the proven retry, real broker order id
`IB-PERM-106341206`, `lifecycle_confirmed=true`). No broker or IB gateway was
involved in the C proof.

**Persist + read-back.** The lifecycle persisted as one row:
`lifecycle_id=oid:IB-PERM-106341206`, `symbol=AAPL`, `account=DUP623346`,
`entry_order_id=IB-PERM-106341206`, `entry_result_status=accepted`,
`lifecycle_confirmed=1`, `record_kind=mechanical_paper_lifecycle`,
`is_edge_outcome=0`.

**Timestamp/session honesty verified.** `persisted_at_utc` and `created_at_utc`
both end in `+00:00` (genuine UTC); `submitted_at_utc` / `observed_at_utc` /
`flattened_at_utc` are null (B2b exposes no true event times);
`timestamp_source=c_persist_time_only`, `event_timestamps_available=0`;
`exchange_timezone=America/New_York`, `market_calendar_id=US_EQ`,
`market_session_date=2026-07-02` derived from the persist instant with
`market_session_date_source=persisted_at_utc_not_execution_time`.

**Market-clock still deferred to D.** `market_clock_checked=0`,
`market_clock_reason=not_checked_in_C_deferred_to_D`. C does not check holidays,
early closes, weekends, lunch breaks, or market-open status; those remain D's
responsibility (via a trusted online/broker market-status source, fail-closed if
unavailable).

**Idempotency proven.** Persisting the same JSON a second time returned
`inserted=false`, `duplicate=true`, and the table still had exactly one row.

**Result.** All C_VERIFY_CHECKS true; `M21_1EXTRA_C_VPS_VERIFY_RC=0`.
