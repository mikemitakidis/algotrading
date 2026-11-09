# IBKR PAPER gateway acceptance tests (real gateway, phases P0–P3)

**Status:** required before automated IBKR PAPER trading is activated. Each phase is a separate, operator-approved step on the VPS (see `docs/reviews/M21_IBKR_Paper_Operations_Plan_2026-11-07.md`). Nothing here is run by tests, CI, `deploy.sh` or the bot.

## Why this exists
`test_m21_paper_e2e_ibkr_paper.py` (PR #41) runs the real loop, gates, readers, writers, M14, journal and monitor against a **simulated** IBKR gateway. That is evidence about **our code's behaviour under modelled gateway answers**, not about the gateway itself. It cannot establish anything in the "Proves" column below. Those facts are established only by the tests on this page, against the real IBKR PAPER Gateway on the VPS. Passing the offline suite does not replace any of them (review: Issue #3 comment 6733991637, finding 5).

## Tests

| # | Phase | Method | Proves (cannot be shown offline) | Pass criterion | Offline counterpart (simulated) |
|---|---|---|---|---|---|
| A1 | P0 | Read-only VPS inspection: IBC/`jts.ini` time zone, gateway process `TZ` / `-Duser.timezone`, host `timedatectl` | The gateway's day boundary, which defines the start of `reqExecutions` "today" | Zone recorded. `IBKR_GATEWAY_TZ` is set to it only under a separate approval. The PR-b reader proves a UTC day only if that zone's day begins at or before UTC midnight; otherwise P&L stays UNKNOWN (trading blocked). | `Period` matrix in `test_m21_paper_b_pnl_source.py`; `test_gateway_zone_not_covering_the_utc_day_blocks` |
| A2 | P0 | Read-only: listening ports, IBC trading mode | Paper-only gateway. LIVE ports 4001 and 7496 are closed. | 4002 listening; 4001 and 7496 not listening; IBC mode paper | `test_gateway_in_live_mode`, `Isolation` tests |
| B1 | P2 | Read-only API session (client id 16, `readonly=True`), no orders | Account ids visible to the paper session | `managedAccounts` = the paper account only; no live account | `test_live_account_visible_blocks_without_reading_it` |
| B2 | P2 | Same session on a day with no paper trades | The `reqPnL` realised figure, base currency and `ExchangeRate` rows exist and behave as modelled | The PR-b reader returns a verified zero-trade day. Base currency and USD rate recorded. | `ZeroDayAndCoverage`, `CrossCheck` |
| B3 | P2 | Read-only session (client id 15) | The M15.5 exposure reader works against the real portfolio snapshot | Exposure known; the LIVE account is not read | `HappyPath` exposure refresh |
| B4 | P2 | Read-only | Client ids 15, 16 and 18 are free; no clash with other sessions | Both readers connect and disconnect cleanly | — |
| B5 | P2 | Read-only | Market-data policy for the loop's quote checks (real-time or delayed) | Recorded; the loop's quote freshness check passes or blocks as designed | `D23LiveQuote` (loop tests) |
| C1 | P3 | One supervised paper round trip (approved separately) | IBKR's accounting: closing `realizedPNL` includes both legs' commissions, and `reqPnL` `realizedPnL` equals their sum | Agreement within $1 (USD base); commission reports present; figures recorded | `ToleranceBound`, `test_after_the_fill_…` |
| C2 | P3 | Observe `reqPnL` across the gateway's day boundary | `reqPnL` reset time | Reset time recorded; no execution falls between it and UTC midnight during regular hours | `test_reset_basis_mismatch_is_unknown` |
| C3 | P3 | Gateway restart after C1 (approved separately) | Whether `reqExecutions` re-lists the day after a restart | Either the reader still proves the day, or it fails closed (`execution_disappeared_or_corrected`). Whichever happens is recorded; a fail-closed result keeps trading blocked for the rest of that day. | `test_gateway_restart_that_loses_the_day_blocks`, `RestartPersistence` |
| C4 | P3 | Loop restart with a working paper order (approved separately) | No duplicate order at the real broker | One parent order (`orderRef` `M21L:`) | `Duplication` |
| C5 | P3 | Throughout | The LIVE account is untouched | No LIVE port, session, read or order at any point; the LIVE holding is unchanged | `LiveIsolation` |
| C6 | P4 | Services installed disabled, then enabled under approval | Alarms fire on the real host | Heartbeat, gateway-down and risk-data-block alarms observed | `test_m21_paper_d_services.py` |

## Open items before activation (not covered by any test yet)
- **Broker-side duplicate monitoring:** reported as `duplicate_orders_broker` = UNKNOWN / NOT_EVALUATED by the monitoring runner (PR #39). An independent broker open-orders reader, or an explicit operator decision, is needed before P4.
- **Source of WP5 observations** for the data-freshness check. Without one, that check reports UNKNOWN.

## Recording results
Post each phase's redacted output on Issue #3, with the commit and the exact commands. Never post account ids, credentials or the VPS address.
