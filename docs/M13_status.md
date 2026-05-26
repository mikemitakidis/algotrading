# M13 — eToro Discovery Status

**Date:** 2026-05-25
**Status:** Discovery complete. Documentation-only design phase next (M13.1).

## Outcome

The eToro Public API has been verified as the correct integration surface
for placing real orders on the Real Main eToro account. The previously-
investigated Agent Portfolio flow (`/agent-portfolios`, scoped user
tokens) is NOT required for normal own-account algorithmic trading; that
flow is a separate product for AI Agent Portfolio sub-accounts.

The correct path uses the `Trading - Real` endpoint family with the
Main account `x-api-key` + `x-user-key` headers directly.

## What was verified (read-only)

A new Real Main API key was created in the eToro API portal with Read +
Write access. Using that key, the following endpoints were tested:

| Endpoint                                  | Method | Outcome   |
|-------------------------------------------|--------|-----------|
| `/me`                                     | GET    | Confirmed |
| `/trading/info/portfolio`                 | GET    | Confirmed |
| `/trading/info/real/pnl`                  | GET    | Confirmed |
| `/market-data/search`                     | GET    | Confirmed |
| `/trading/info/trade/history`             | GET    | Confirmed |

One isolated routing issue was noted on `/market-data/rates` (HTTP 404
`RouteNotFound`). This is a path/parameter shape issue, not an auth or
scope issue — to be resolved during M13.1 design through documentation
reading.

## What was NOT done

- No POST/DELETE endpoint was called.
- No order or trade was placed (open or close).
- No `etoro_broker.py` was created.
- No `etoro_read.py` was created.
- No repo code was modified for eToro.
- No eToro credentials were committed to the repo (no `.env.example`
  entries, no tracked files containing keys or tokens).
- No UUIDs, account IDs, balances, position counts, or response bodies
  are recorded in this file.

## Conclusion

eToro Public API supports the integration path the project needs.
Read access on the Real Main account is confirmed. Write capability
remains designed but not yet exercised.

## Next

M13.1 — documentation-only design phase. Produces design documents
only, no production code, no broker adapter, no dashboard changes.
Implementation work (read adapter, paper broker, live writes) is
deferred to M13.2+ and gated on explicit ChatGPT review and operator
approval of the M13.1 design.

The IBKR execution path remains unchanged and unaffected.
