# M21.1extra-C — paper-lifecycle outcome persistence

- data_source: **mock_broker_structural_proof**
- table: **paper_lifecycles** (append-only, idempotent)
- record_kind: **mechanical_paper_lifecycle**
- is_edge_outcome: **0**
- exchange_timezone: **America/New_York**
- market_calendar_id: **US_EQ** (identity only; D maps to a calendar)
- total_lifecycles: **0**
- lifecycle_confirmed_count: **0**

> **C persists B2b-style MECHANICAL paper-lifecycle records (immediate-flatten) into a dedicated append-only table. These are NOT hold-to-exit P&L edge outcomes. Timestamps are persist-time only (timestamp_source=c_persist_time_only, event_timestamps_available=false); C does not claim to know exact broker submit/observe/flatten instants. The exchange session date is derived from the UTC instant via America/New_York (zoneinfo, DST-correct), never a fixed offset. C does not schedule, hold trades open, or touch the dashboard — the market-clock guard is deferred to D (market_clock_checked=false).**

> **C does NOT check public holidays, early closes, weekends, lunch breaks, or market-open status. Those checks are explicitly deferred to D. C only stores DST-safe UTC timestamps, an America/New_York session date, and market-identity fields (market_calendar_id, exchange_timezone) so D can enforce a real per-exchange market-open guard.**
