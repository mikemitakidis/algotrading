-- OP1.3a  M16 historical metadata — data/historical.db COPY, read-only (sqlite3 -readonly).
-- Coverage, gaps, duplicates, quality and refresh history per provider/timeframe/symbol. No bar values.
.print '== A. tables present'
SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'historical_%' ORDER BY name;
.print '== B. symbols in the registry (active vs inactive, by asset class)'
SELECT asset_class, is_active, COUNT(*) AS symbols, MIN(first_seen_at) AS first_seen, MAX(last_seen_at) AS last_seen
FROM historical_symbols GROUP BY 1, 2 ORDER BY 1, 2;
.print '== C. coverage by provider and timeframe (symbols, bars, window, gaps, duplicates)'
SELECT provider, timeframe, COUNT(*) AS symbols, SUM(bar_count) AS bars, MIN(first_ts_utc) AS earliest_utc,
       MAX(last_ts_utc) AS latest_utc, SUM(missing_count) AS missing_bars, SUM(duplicate_count) AS duplicate_bars,
       SUM(missing_count > 0) AS symbols_with_gaps, COALESCE(source_timeframe, '-') AS derived_from,
       COALESCE(derivation_method, '-') AS derivation
FROM historical_coverage GROUP BY provider, timeframe, source_timeframe, derivation_method ORDER BY 1, 2;
.print '== D. quality and freshness status by provider and timeframe'
SELECT provider, timeframe, quality_status, freshness_status, COUNT(*) AS symbols, MAX(last_refresh_at_utc) AS last_refresh_utc
FROM historical_coverage GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4;
.print '== E. per-symbol coverage (the full list is the inventory; one row per symbol and timeframe)'
SELECT provider, timeframe, symbol, bar_count, first_ts_utc, last_ts_utc, missing_count, duplicate_count,
       quality_status, freshness_status, COALESCE(provider_limit_note, '') AS provider_limit_note
FROM historical_coverage ORDER BY provider, timeframe, symbol;
.print '== F. quality events by severity and kind (all time, and since 1 June 2026)'
SELECT severity, kind, COUNT(*) AS n, SUM(created_at_utc >= '2026-06-01') AS since_june, MIN(created_at_utc) AS first_utc,
       MAX(created_at_utc) AS last_utc
FROM historical_quality_events GROUP BY 1, 2 ORDER BY 1, 2;
.print '== G. refresh runs (status, scale, last run)'
SELECT provider, mode, status, COUNT(*) AS runs, SUM(symbols_failed) AS symbols_failed,
       SUM(symbols_rate_limited) AS symbols_rate_limited, SUM(bars_fetched) AS bars_fetched,
       MIN(started_at_utc) AS first_utc, MAX(started_at_utc) AS last_utc
FROM historical_refresh_runs GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;
.print '== H. refresh lock (a lease held now means a refresh may be running: take the copy again later)'
SELECT owner_host IS NOT NULL AS held, acquired_at_utc, lease_expires_at_utc, last_heartbeat_utc FROM historical_refresh_lock;
