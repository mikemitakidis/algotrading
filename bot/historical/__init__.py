"""bot.historical — M16 historical data engine.

Package layout (read by callers; written only via refresh.run):

  store.py        public read façade (get_bars, get_coverage, list_*)
  schema.py       SQL DDL + SCHEMA_VERSION + apply_schema()
  refresh.py      write orchestrator (backfill, incremental, repair,
                    force_rebuild)
  coverage.py     coverage state queries + updates
  quality.py      validation rules + quality_event writer
  timeframes.py   UTC arithmetic + 1H -> 4H resampling
  providers.py    BaseProvider ABC + ProviderCapability dataclass
  providers_yfinance.py  yfinance adapter
  cli.py          operator entry points (python -m bot.historical.cli ...)
  preview.py      M16.B: tiny local-read proof (SMA)

Hard invariants:
  * The only public read API is bot.historical.store.get_bars(...).
  * The only public write path is bot.historical.refresh.run(...).
  * All timestamps are UTC, tz-aware, ISO-8601.
  * No broker / order / live-trading imports anywhere in this package.
    (AST-asserted by test_m16_historical_data.TestNoBrokerImports.)
"""
