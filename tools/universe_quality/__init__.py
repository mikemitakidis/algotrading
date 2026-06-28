"""M21.UQ — Global Quality Collectors / Gates (read-only).

A read-only quality framework that evaluates EXISTING global candidates in
configs/universe/global_expanded.json (UK 100 + HK 93 = 193) for provider
validity, OHLCV quality, staleness, liquidity, and duplicate/suffix integrity.

Strictly read-only: it NEVER writes global_expanded.json or source_registry.json,
NEVER sets scan_ready, NEVER touches runtime/scanner/broker code. Unit tests use
fixtures/mocks only; no live network. A provider interface is defined so a real
yfinance fetch can be plugged in later behind the same contract, but the default
unit path is offline.
"""
