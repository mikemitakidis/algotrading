# M21.1extra-D1 — Market-Clock Guard (US_EQ slice)

Branch `m21-1extra-d1-market-clock`, off main `1cf0f61`.

## What D1 is

A **read-only, fail-closed market-clock guard**. Given a `market_calendar_id`
and an instant, it answers one question — *may an order be submitted right now?*
— and defaults to **do not submit**. `may_submit=True` is returned only on a
**fresh, supported (US_EQ), regular-session, affirmative-open** reading from a
trusted source (and, when a second source is present, only when the sources
agree).

D1 is a sibling gate to D0. D0 is the time-boxed per-lane authorization master
switch; D1 is the market-open check. A later step (D2) will AND them together
before any unattended submission. D1 does not call D0 and does not place orders.

## Scope statement (what D1 proves and does NOT prove)

```
D1 proves the market-clock guard only.
D1 does NOT prove an unattended scheduler.
D1 does NOT prove order placement.
D1 does NOT prove D2/D3.
D1 does NOT prove strategy edge or profitability.
D1 does NOT write lifecycle outcomes.
D1 returns C-compatible market-clock fields only; D2/C persistence happens later.
```

## Design

- **Source priority (this slice):** Alpaca Clock/Calendar is the primary and
  only live source. IBKR is a mock/stub interface only — D1 never opens or owns
  a gateway connection. A second source can be injected later (D1b/D2) using an
  already-existing connection context; when two sources are present the guard
  requires agreement and treats disagreement as fail-closed (`inconsistent`).
- **Regular session only.** Pre-market and after-hours never produce
  `market_open`. The regular-session window comes from the Alpaca **calendar**
  (`open`/`close` for the day), converted from local ET to UTC via `zoneinfo`
  (DST-correct, no hardcoded 09:30/16:00). Early closes are honored because the
  calendar's own close time drives the window.
- **Freshness.** Default `max_age_sec = 30` (injectable). Freshness is measured
  on our own fetch time (and on the source `as_of` if present). A small future
  clock-skew tolerance (5s) prevents a source clock marginally ahead of ours
  from reading as stale; a large future value is still rejected.
- **Cache.** An injectable `MarketClockCache`, **default off**. A cached OPEN is
  served only within freshness; a stale cached open returns do-not-submit, and
  the cache never hides a source failure once freshness expires (no stale-allow).
- **Tightening rule.** If the clock says open but the calendar is
  missing/unavailable/malformed, the guard **fails closed**. If the clock says
  closed, it may return `market_closed` even without a calendar (documented and
  tested).
- **Secrets.** Credentials come from `ALPACA_KEY` / `ALPACA_SECRET` and are sent
  only as Alpaca auth headers — never printed, returned, logged, or placed
  (even masked) in `raw_source_summary`.

## Reason codes (stable)

`market_open` (the only `may_submit=True`), `market_closed`,
`market_calendar_unsupported`, `market_status_no_credentials`,
`market_status_source_unavailable`, `market_status_source_stale`,
`market_status_source_malformed`, `market_status_source_inconsistent`,
`market_extended_hours_refused`, `market_clock_internal_error`.

## C-compatible fields (returned, not written)

`MarketClockDecision.c_fields()` returns `market_calendar_id`,
`market_clock_checked`, `market_clock_reason`, `as_of_utc`. D1 writes nothing —
a later D2/C wiring step persists these onto the lifecycle record.

## Tests

- `test_m21_1extra_d1_guard.py` — the full fail-closed matrix on mock sources
  (open/closed/weekend/holiday/early-close both sides/pre+after-hours/
  unsupported/no-creds/unavailable/malformed/missing-is_open/stale (fetch and
  as_of)/future-skew tolerance/cache within-and-past freshness/inconsistent/
  boundaries/DST Mar+Nov/property "every failure blocks"/decision invariants).
- `test_m21_1extra_d1_alpaca_source.py` — the Alpaca adapter with mocked HTTP
  (no live network): credentials, all HTTP failures, malformed shapes, date
  parsing + UTC/DST normalization, explicit timeout, no-secret-leak.
- `test_m21_1extra_d1_vps_proof.py` — the read-only proof runner contract.

## VPS proof (read-only)

`tools/paper_loop/m21_1extra_d1_market_clock_proof.py` runs offline/safe checks
(mock sources + guard) plus a credential-gated live Alpaca check. It performs
**no** order placement, DB write, `paper_lifecycles` write, scheduler, D0
enable/disable, D2 loop, or IBKR gateway connection, and prints no secrets.

- If `ALPACA_KEY`/`ALPACA_SECRET` are absent: it proves the real adapter fails
  closed (`market_status_no_credentials`) and marks the live fresh-response
  proof as *pending credentials* — still `RC=0` when the fail-closed checks pass.
- If present: one read-only clock/calendar check. The **live-source proof
  passes only if Alpaca returned a fresh, well-formed `market_open` or
  `market_closed`**. If the live source is `unavailable`/`stale`/`malformed`/
  `internal_error`, the guard still fails closed correctly (that safety property
  is separately asserted and remains true), but the **live-source proof did not
  pass**, so the runner returns `RC=1` and prints the reason honestly (no
  secrets). This separates two distinct claims: *guard fail-closed works* vs
  *the live Alpaca source proof passed*.

```
Credentials absent          = fail-closed proof passes; live-source proof pending credentials (RC=0).
Credentials present + fresh open/closed = live-source proof passes (RC=0).
Credentials present + unavailable/stale/malformed = live-source proof failed/pending rerun (RC=1); fail-closed guard still correct.
```

Output ends with `D1_VPS_VERIFY_OK` and `M21_1EXTRA_D1_VPS_VERIFY_RC=0` (or the
failure RC). The runner is invoked by a wrapper that logs the full transcript to
`/tmp/m21_1extra_d1_market_clock_<timestamp>.log`, hard-aborts unless HEAD is the
approved D1 SHA, and does not close an interactive shell.
