# M20 Roadmap & Status

**Purpose:** single source of truth for M20 progress. Not chat-dependent.
**Last reviewed:** 2026-06-25

## Baseline
- M19 frozen on `main`: `e823fe6779deaccc7b8ff7859c17b4dab564b868`
- Nothing in M20 merged to main yet (branch: `m20-uc1-quality-collector`).
- Merge to main requires three-party sign-off (Claude implements, ChatGPT
  reviews, Mike verifies on VPS).

## Completed & frozen
| Milestone | Description | Status |
|-----------|-------------|--------|
| M20.A | Paper contracts foundation | frozen |
| M20.B | Paper routing foundation | frozen |
| M20.C | Clean-room paper risk sizing | frozen |
| M20.D | Simulated fills / order handling | frozen |
| M20.E | Paper positions / PnL foundation | frozen |
| M20.F | Closing / realised PnL | frozen |
| M20.G | Account / cash / portfolio ledger | frozen |
| M20.H | Paper storage foundation | frozen |
| M20.UA | Universe registry infrastructure | frozen |
| M20.UB | US expansion (S&P500 ∪ Nasdaq100 + curated ETFs) | frozen |
| M20.UC1 | Quality collector + final v3 snapshot | frozen |
| M20.UC2 | Quality gate engine + universe write-back | frozen |

### Key commits
- UC1 canonical quality snapshot: `63b16ba0ea8418a4e9069dd536618adc9dd67766`
- UC2 engine/config/tests: `52ee00d093976b32a54769aa0a2cfb1fbc5b4611`
- UC2 universe write-back: `501487ffb715e62bb4172c1bca55a173a3e492b1`
- M20 roadmap doc: `3ec5d89766563a0347939a3572e653c31d2d65c9`
- M20.UE registry runtime selector: `d077260d189a8fe6927b7c994f45872800df243a`
- Test-refresh / full regression green: `6109650904b320b49806b21ce2ce7f6e3dca05c3`
- M20.I runtime paper loop: `8421a2fae4cadfa003f0985e8a3fba93b5e4c838`

### Current universe state (after UC2 write-back)
- Total symbols: 573
- verified: 536 · failed: 18 · unverified: 19 · scan_ready: 536
- identity_changed: 0 (only quality/scan_ready fields written)
- runtime / scanner / paper / live / dashboard / risk / broker: untouched
- Reproducible from committed snapshot (v3) + `quality_thresholds.json`.

### UC2 locked rules (carry forward)
- Both Alpaca AND Yahoo required for `verified` (single-source → unverified).
- Cross-check is PRICE + DATE only. Alpaca IEX (single-venue) vs Yahoo
  (consolidated) volume is NOT comparable; volume divergence is informational
  and never causes a failure.
- Liquidity gates use one consolidated source (Yahoo).
- Default scan_ready=false; configurable ceiling `max_scan_ready_per_run` (600).
- Yahoo must be collected from a residential IP, never the VPS datacenter IP.

## Remaining M20

### M20.UE — runtime migration to registry  (DONE — d077260)
Migrated runtime symbol selection from hardcoded `FOCUS_SYMBOLS` toward the
registry / `scan_ready=true` universe, flag-gated (USE_REGISTRY_UNIVERSE,
default off). FOCUS_SYMBOLS retained; default behaviour unchanged.

### M20.I — runtime paper loop  (DONE — 8421a2f)
Scanner signal dict -> M19 scoring -> paper routing -> paper engine, in
`bot/runtime/paper_loop.py`, flag-gated (PAPER_LOOP_ENABLED, default off).
Simulation only; no live orders; no broker calls; uses `paper_routing_eligible`.

### M20.J — dashboard / admin visibility
Surface universe quality + paper status: total symbols, scan-ready count,
failed/unverified counts, latest quality snapshot, paper trades, PnL, ledger.
- Admin tasks should eventually return structured results:
  `tests_passed`, `tests_failed`, `exit_code`, `summary`, `log_path`
  (not terminal-only).

### M20.UD — UK/EU/Japan/HK/China/ADRs/global candidates — REQUIRED (next after M20 close / main merge)
This is the next required roadmap item once M20 is closed and merged to main.
It is NOT dropped and NOT optional.
- Adds global candidates: UK, EU, Japan, Hong Kong, China, ADRs, and other
  non-US lines.
- These are added as **inactive registry candidates first**:
  - `active = false`
  - `scan_ready = false`
  - NOT used by runtime / scanner / paper loop yet.
- Registry-only additions; no global scanning, no trading on them.
- Later, quality-gating + global activation will make the bot able to check
  1000+ stocks safely (only after the inactive candidates land and prove out).
- Global activation requirements — FX, exchange sessions, trading calendars /
  holidays, and currency-aware paper PnL — remain a **later** milestone
  (M20.UF or beyond), NOT part of M20.UD.

Sequence: M20 close -> main merge -> **M20.UD (inactive global candidates)** ->
later global activation (FX/session/calendar/currency-aware PnL).

### M20.UF (or later) — global activation  (DEFERRED)
Requires FX, timezone/session handling, exchange calendars, holiday logic,
currency-aware paper PnL. Not in the immediate M20 finish path unless approved.

## Recommended short path to finish M20
1. UC2 verified/closed at `501487f`. (done)
2. Add this roadmap doc. (this file)
3. M20.UE — plan only, then approval, then implement.
4. M20.I — paper loop.
5. M20.J — minimal read-only status reporter (bot/runtime/m20_status.py).
6. Final M20 regression/protected verification, then merge to main.
7. M20.UD (inactive global candidates) is the REQUIRED next item after merge.
8. No merge to main without final three-party approval.

## Guardrails (always)
- Never modify outside the approved milestone's file set.
- Protected/frozen: main.py, bot/scanner.py, bot/risk.py, bot/risk_authority/*,
  bot/strategy.py, dashboard/app.py, bot/brokers/*, bot/live*, bot/flywheel.py,
  bot/signal_scoring/*, bot/providers/alpaca_provider.py, bot/paper/* (outside
  its milestone), and the universe schema/registry/suffixes (outside UA).
- All VPS Python via `/opt/algo-trader/venv/bin/python3`.
- One milestone per commit; push before reporting; report includes a VPS
  verification command.
