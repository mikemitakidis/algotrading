# M20.U — Universe Registry — Status (M20.UA)

Static hand-written reference. Not imported by any module. Not generated at
runtime.

## Purpose

`bot/universe/` is an isolated, read-only symbol registry. It answers "what
instruments are known to the system, and what provider ticker / exchange /
country / currency / timezone / calendar does each belong to" — not "should we
trade this." It is additive: the live runtime still uses
`bot.focus.FOCUS_SYMBOLS` until a later, separately-approved migration
milestone (M20.UE).

## Layered universe model

1. **Master registry** — everything known (eventually 1000+ global instruments).
2. **Active** (`active = true`) — structurally valid and provider-supported.
3. **Scan-ready** (`scan_ready = true`) — also passed quality/liquidity checks
   (the checks are introduced in M20.UC; nothing is scan-ready in M20.UA).
4. **Paper-routable** — scan-ready AND an M19 eligible/high-conviction signal
   AND M20 paper-routing gates passed (later M20 phases).

## M20.UA scope

Seeds only the current 89 symbols from `bot/focus.py`, all US, as
`SymbolRecord`s with `active = true`, `scan_ready = false`, and the
`legacy_focus` tag. Liquidity/quality fields are null by design and will be
populated in M20.UC. No global symbols, no expansion, no runtime wiring.

## Survivorship-bias limitation (important)

This registry is a **current / curated universe snapshot**. It is **not** a
point-in-time historical constituent database. Backtests using it may still
have survivorship bias unless they use historical constituent snapshots. Every
record carries `source`, `as_of_date`, and `first_seen_utc` for provenance.
When a symbol is removed from an index or delisted it must be marked
`active = false` and **retained, not deleted**, so historical analysis is not
silently corrupted.

## Determinism / isolation

The loader is pure and read-only: no network, no runtime scraping, no file
writes, no default path. Suffixes are mapped from a static table
(`bot/universe/suffixes.py`), never guessed. The package imports nothing from
`bot.paper`, brokers, live, risk, main, or dashboard.

## Deferred

S&P 500 / Nasdaq 100 / ETF expansion (M20.UB), liquidity/quality gates
(M20.UC), global inactive candidates — UK/EU/JP/HK/CN (M20.UD), and runtime
scanner migration off `FOCUS_SYMBOLS` (M20.UE, separate approval). Session-aware
candles, exchange-holiday calendars, and FX conversion for non-USD paper PnL are
prerequisites for global activation and are deferred to later milestones.
