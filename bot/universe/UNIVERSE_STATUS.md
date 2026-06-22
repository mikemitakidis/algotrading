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

## M20.UB — US universe expansion (S&P 500 + Nasdaq 100 + ETFs)

`configs/universe/us_expanded.json` adds **484** US records on top of the 89-symbol
seed for a **573**-symbol total universe:

- **436 equities** — union of S&P 500 and Nasdaq 100 constituents, deduplicated
  against each other and against the existing seed. Symbols already in the seed
  are NOT duplicated; instead their `universe_tags` are merged in place (84 seed
  records now carry `sp500`/`nasdaq100`/`m20_ub` tags). A symbol in both indices
  carries both `sp500` and `nasdaq100` tags on a single record.
- **48 ETFs** — curated major liquid US ETFs (broad index, sector SPDRs,
  bond/credit, commodity, international, volatility), deduped against the seed's
  existing ETFs (whose tags were merged rather than duplicated).

Full-universe tag coverage: `sp500`=499, `nasdaq100`=99, `us_etf`=56.

### Status flags

All M20.UB records are `active=true`, `scan_ready=false`,
`data_quality_status="unverified"`. `active=true` means a structurally valid
known US-universe member (constructs against the frozen `suffixes` table);
`scan_ready=false` is the activation gate. Liquidity fields
(`avg_volume_20d`, `avg_dollar_volume_20d`, `median_spread_bps`,
`min_liquidity_tier`) are **null** — never fabricated. M20.UC verifies liquidity
/ data quality and decides which records become scan-ready.

### Sources / provenance (as-of 2026-06-22)

- **S&P 500 / Nasdaq 100 equities:** the PyPI package `pytickersymbols==1.17.10`
  (offline, version-pinned constituent dataset — no runtime scraping, no live
  download). The sandbox network blocks the index publishers directly, so a
  pinned, reviewable package snapshot is used and recorded in each record's
  `source` field. Listing exchange is taken from the package's `traded_as`/
  `google` fields, restricted to NASDAQ/NYSE/ARCA; a small curated override map
  fixes a few well-known listings the package could not resolve. Symbols not
  confidently mappable to a supported exchange were excluded (e.g. `CBOE`, whose
  Cboe listing venue is not in the supported table) rather than mis-tagged.
- **ETFs:** hand-curated list of major liquid US ETFs (`source` recorded per
  record), as-of 2026-06-22.

The expansion JSON was authored once in the sandbox and committed as reviewed
static data; nothing in `bot/` fetches constituents at runtime. `country`,
`currency`, `timezone`, `trading_calendar`, and `region` are derived from the
frozen `suffixes` table per exchange (and validated by `SymbolRecord`), never
hand-typed.

## Deferred

Liquidity/quality gates (M20.UC), global inactive candidates — UK/EU/JP/HK/CN
(M20.UD), and runtime scanner migration off `FOCUS_SYMBOLS` (M20.UE, separate
approval). Session-aware candles, exchange-holiday calendars, and FX conversion
for non-USD paper PnL are prerequisites for global activation and are deferred to
later milestones.
