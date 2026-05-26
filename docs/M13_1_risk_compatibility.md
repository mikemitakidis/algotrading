# M13.1 — Risk Layer Compatibility Decision

**Status:** Documentation only.

This document records the design decision for how the M14 portfolio
risk layer (`bot/risk.py`, `PortfolioRiskPolicy`) handles two brokers
running in parallel. **No `bot/risk.py` code changes in M13.1.** The
decision documented here will guide M13.6 implementation when (and
only when) it is approved.

## Context

The current `bot/risk.py` was designed for one broker at a time:
- The bot reads `BROKER=` from config.
- `RiskManager` and `PortfolioRiskPolicy` evaluate intents against
  one portfolio's state.
- Position sizing, exposure limits, and the daily loss budget all
  assume one account.

Adding eToro means the bot can, in principle, place trades on the
IBKR Real account AND on the eToro Real account in the same session.
The risk question: are these one combined portfolio or two
independent ones?

## Three options

### Option A — Independent broker pools (RECOMMENDED)

Each broker has its own independent risk budget. IBKR's daily loss
limit, max open positions, and per-position exposure cap apply to
IBKR trades only. Same for eToro.

**Pros:**
- Minimal change to `bot/risk.py`. Risk state already partitioned by
  broker in `daily_state` (rows keyed by broker_mode). The policy
  evaluator just needs to filter inputs by `route='ETORO'` vs
  `route='IBKR'`.
- No real-time currency normalisation required. IBKR limits stay in
  GBP, eToro limits stay in USD.
- No coupling between brokers. A bad day on IBKR does not freeze
  eToro and vice versa.
- Easy to reason about. Easy to test.

**Cons:**
- Risk discipline is per-pool, not whole-portfolio. An operator who
  funds both accounts could in principle exceed their intended total
  daily risk by trading both at their per-broker limit.
- Requires operator discipline at account funding level (set IBKR
  daily limit + eToro daily limit such that the sum is acceptable).

**Change footprint in `bot/risk.py`:**
- `PortfolioRiskPolicy.evaluate()` accepts a `broker_mode` parameter
  and filters open positions / daily PnL by that broker.
- `daily_state` rows are looked up by `(date, broker_mode)` instead
  of just `date`. (Schema already supports this — `daily_state` has
  a `broker_mode` column from M14.)
- New `.env.example` entries:
  `RISK_MAX_OPEN_POSITIONS_ETORO`, `RISK_DAILY_LOSS_LIMIT_ETORO`,
  etc. — separate from the existing IBKR ones.
- About 30–50 lines added to `bot/risk.py`. No data flow changes.

### Option B — Combined cross-broker portfolio

The two brokers are treated as one portfolio. Position sizing
considers exposure on both. Daily loss limit applies to combined PnL.

**Pros:**
- Operator's intended "I'm risking £X per day" is enforced regardless
  of which broker the orders go to.
- More accurate portfolio-level metrics (Sharpe, drawdown, etc.) once
  M13 is mature.

**Cons:**
- Requires real-time GBP↔USD conversion at every risk check.
- Currency conversion source must be authoritative — using eToro's
  `conversionRateAsk`/`conversionRateBid` is one option, but those
  are per-instrument and not the cross-currency rate.
- Failure mode: if currency rate fetch fails or stales, the risk
  layer can't function — adds a new critical-path dependency.
- Significant `bot/risk.py` rewrite: state, evaluation, and tests
  all need cross-broker logic. Several hundred LOC delta.
- Reconciliation gets harder: a fill on one broker has to be reflected
  in the other broker's available budget within seconds.

**Change footprint in `bot/risk.py`:**
- Substantial. Estimated several hundred LOC. New currency module.
  New tests. Schema additions to `daily_state` and
  `portfolio_risk_snapshots` to track combined-vs-per-broker.

### Option C — Exclusive broker selection (mutual exclusion)

The bot can be configured for ONE broker per run. `BROKER=ibkr_live`
OR `BROKER=etoro_real`. Never both at once. Switching brokers
requires a process restart.

**Pros:**
- Zero risk-layer changes. Today's code works.
- Simplest possible model.

**Cons:**
- Defeats half the reason for adding eToro. The whole point of a
  second broker is parallel execution.
- Operator has to choose between IBKR and eToro per session — manual
  reconfiguration to switch.
- Hedging across brokers impossible.

## Recommendation: Option A

Independent broker pools is the right choice for M13.6 because:

1. **It matches the actual constraint.** Each broker's funded
   capital IS independent. There's no fungibility between IBKR and
   eToro account balances. Treating them as one portfolio is a
   reporting-layer concern, not a risk-enforcement concern.
2. **It's the smallest change to `bot/risk.py`.** Less code to write,
   less code to break, less code to test.
3. **It scales.** If a third broker is ever added, Option A extends
   trivially. Option B doesn't.
4. **It's reversible.** Option A → Option B is a future refactor that
   can be planned and executed cleanly. Option B → anything else is
   a much bigger undo.
5. **It does not introduce a new critical-path dependency.** No
   currency rate fetch in the risk-check critical path means one
   less failure mode at the worst possible time.

The currency-aware whole-portfolio view (what Option B promises) can
be added LATER as a read-only reporting layer over the same data,
without changing the risk-enforcement path.

## What changes / what doesn't change (against current `bot/risk.py`)

### What changes (in M13.6, NOT in M13.1)

- `PortfolioRiskPolicy.evaluate()` takes a `broker_mode` parameter
  and filters its inputs (open positions, today's PnL) by that
  broker.
- Lookups against `daily_state` use the existing `broker_mode`
  column. (No schema migration needed — the column exists since
  M14.)
- Lookups against open positions filter by `broker` column. (No
  schema migration needed — the column exists since M10.)
- `.env.example` gains a duplicated set of risk limit envs scoped
  per broker. Existing IBKR-scoped ones become explicitly named.
- Backwards compatibility shim: if only the unscoped envs are set,
  they default for both brokers.

### What does NOT change

- `bot/risk.py`: untouched in M13.1, M13.2, M13.3, M13.4, M13.5.
- Schema: no new columns. No new tables.
- M10 `OrderIntent`: unchanged.
- M12 lifecycle: unchanged.
- M15 modules (watchdog, recovery executor, heartbeat): unchanged.
- IBKR-only behaviour: bit-for-bit identical when eToro is not
  configured.

## Gating rule

Option A is the recommendation. It is **not implemented** in any
phase up to and including M13.5. M13.5 lands with eToro running
under the same single-broker assumption as IBKR — the bot is run
with EITHER `BROKER=ibkr_live` OR `BROKER=etoro_real`, not both, in
M13.5.

Only M13.6 (cross-broker risk pool) flips on the dual-broker
capability, and only after:

1. M13.5 is closed.
2. ChatGPT reviews and approves the M13.6 design.
3. Operator approves the M13.6 plan including the new env keys.
4. Tests for the dual-broker risk path are written first, pass, and
   prove that single-broker behaviour is unchanged.

Until then, "eToro and IBKR at the same time" is **not supported**.

## Out of scope for M13.1

- Whole-portfolio reporting (Option A + reporting layer is a future
  enhancement, not part of M13.6).
- Cross-broker hedging strategies.
- Currency-converted dashboard.
- Account-funding recommendations.
