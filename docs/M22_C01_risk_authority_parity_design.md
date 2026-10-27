# C01 — scanner-path Risk Authority parity: design (M22 hard blocker)

**Status: DESIGN ONLY.**
- No code, configuration or broker behaviour is changed by this document.
- Basis: `main` @ `80b4343`, plus the WS2 risk gate in draft PR #1 @ `46c87ab` and the A01 contract in draft PR #12 @ `3f1fb1e`.
- Anything that can only be confirmed on the VPS is labelled **VPS VERIFICATION REQUIRED**.

C01 stays a **hard blocker for M22 live-trading readiness** until this design is implemented, tested and independently reviewed.

---

## 1. The live scanner → broker path today (`main.py`)

Steps 1–7 run in order for each signal; step 8 happens inside `broker.submit()`.

| # | step | where | decides |
|---|---|---|---|
| 1 | `scan_cycle()` → signals | `main.py:275`, `bot/scanner.py` | candidates (M19 is **not** on this path) |
| 2 | `insert_signal`, `log_candidate` | `main.py:298-315` | persistence only |
| 3 | `OrderIntent(...)` from the signal | `main.py:317-327` | — |
| 4 | **`RiskManager.evaluate(intent)`** | `main.py:328` → `bot/risk.py:81` | legacy gates L1–L6; also **sizes** the intent |
| 5 | `portfolio_ctx.gather` + **`PortfolioRiskPolicy.evaluate`** | `main.py:331-370` → `bot/risk.py:515` | portfolio gates P1–P5 |
| 6 | gateway watchdog (IBKR only) | `main.py:381` | infrastructure readiness, not risk |
| 7 | **`broker.submit(intent)`** | `main.py:408` | broker-level gates B1–B5 (below) |
| 8 | `log_intent` / `update_intent_status` | `main.py:385, 409, 429` | audit (`execution_intents.risk_checks` JSON) |

**M14 is absent.** The M14 Risk Authority (`bot/risk_authority`: `decide`, `decide_and_audit`, `run_risk_preflight`, governor) is **not imported** by `main.py`, `bot/scanner.py` or `bot/risk.py`.

### Every code path that can reach a broker order (non-test code)

| path | entry | risk gating today |
|---|---|---|
| **Scanner main loop** | `main.py:408` `broker.submit` | legacy RiskManager + PortfolioRiskPolicy + broker-level checks. **No M14.** |
| eToro live operator CLI | `tools/etoro_live_write.py:282,466` | **M14 `run_risk_preflight`**, then `submit_live` with nonce |
| WS2 paper learning loop (PR #1, draft) | `bot/paper_loop_d2/scheduler.py` → `submit_bracket` | D0 + D1 + kill switch + **M14 preflight** with boundary market-state verification (A09) |
| B2b one-shot paper submit (operator) | `tools/paper_loop/m21_1extra_b2b_single_submit.py:195` | confirmation flag, kill switch, clean slate. **No M14.** |
| IBKR flatten / close | `bot/brokers/ibkr_broker.py:747` (`placeOrder` close) | flatten-specific checks. **No M14** (risk-reducing) |
| M20.I paper loop in `main.py` | `run_paper_loop` (flag-gated) | simulation only, no broker |

---

## 2. Gate inventory

**Legacy `RiskManager.evaluate`** (`bot/risk.py:81`)

| id | gate | behaviour on missing / invalid state |
|---|---|---|
| L1 | `BROKER=ibkr_live` and `RISK_MAX_POSITION_PCT > 2` → block | config read |
| L2 | live only: `IBKRBroker().reconcile()`. A broker position or open order in the symbol → block. Broker positions + local intents ≥ max_open → block | reconcile exception → **block** (fail-closed) |
| L3 | market hours = **UTC weekday only** | no hours, no holidays, no early closes |
| L4 | sizing: `portfolio_size × max_pct ÷ abs(entry − stop)`; zero → block | portfolio size is **config** (`RISK_PORTFOLIO_SIZE`), not equity |
| L5 | open intents (`execution_intents`) ≥ `RISK_MAX_OPEN_POSITIONS` → block | `_load_open_intents()` returns **`[]` on any error → counts 0 (fail-OPEN)** |
| L6 | same symbol + direction already open → block (unless `RISK_ALLOW_DUPLICATES`) | same `[]` on error → **fail-OPEN** |

**Legacy `PortfolioRiskPolicy.evaluate`** (`bot/risk.py:515`). It re-reads env on every call.

| id | gate | behaviour on missing / invalid state |
|---|---|---|
| P1 | daily loss, as % of **config** portfolio; latch | PnL unknown → block **live only** (`RISK_REQUIRE_DAILY_PNL_FOR_LIVE`, default true); paper warns. **`RISK_ALLOW_DAILY_LOSS_OVERRIDE=true` skips the gate entirely** |
| P2 | bracket-aware open-trade count ≥ max → block | uses ctx lists; empty lists count 0 |
| P3 | symbol exposure % of config portfolio > `RISK_MAX_SYMBOL_EXPOSURE_PCT` → block | values "estimated" when market values missing |
| P4 | sector exposure % > max → block | sector unknown → block live (default true); paper warns. SectorMap load error → empty map |
| P5 | loss-streak cooldown | outcomes unavailable → block live **only if `RISK_REQUIRE_OUTCOMES_FOR_LIVE`** (default **false**). Read exception → **not blocked (fail-OPEN)** |
| — | `ctx.kill_switch_active` | **populated by `portfolio_ctx.gather` but never evaluated by the policy** |

**Broker-level** (inside `submit`)

| id | gate | brokers |
|---|---|---|
| B1 | file kill switch `bot.kill_switch.is_kill_switch_active()` (missing/unreadable → active) | **IBKR only** |
| B2 | D0 runtime policy `get_signal_only_reason(broker)` (policy DB, TTL cache; unknown → skip) | IBKR, PaperBroker, eToro paper |
| B3 | `_check_live_safety_config()` | `ibkr_live` |
| B4 | paper port must be 4002 | IBKR paper |
| B5 | gateway TCP probe | IBKR |

**M14 engine** (`bot/risk_authority/engine.py:684`, 24 gates in a fixed order, first failure wins; `decide_and_audit` writes `risk_snapshots` + `risk_decisions`):
`policy`, `global_kill`, `broker_kill`, `global_auto`, `broker_auto`, `broker_allowed`, `etoro_live_flag`, `etoro_live_env`, `authority` (trade_open needs ≥ `ONE_SHOT_MANUAL`), `amount_valid`, `amount_min`, `single_trade_cap`, `broker_capital`, `global_capital`, `combined_exposure` (all 4 scopes), `broker_open_positions`, `global_open_positions`, `broker_daily_loss` (+ latch), `global_daily_loss`, `drawdown`, `concentration` (per-symbol USD), `market_open`, `quote_freshness`, `spread`, `data_staleness` (exposure and PnL known; ≥1 fresh read).
- Unknown exposure or PnL → block.
- The governor proposes downgrade-only authority changes.
- **Caveats, verified in code:**
  - `market_open` is a caller-supplied flag defaulting to `True` (`engine.py:367`, `preflight.py:83`).
  - `quote_freshness` and `spread` are enforced only when the caller supplies them.
  - There is no sector gate (design-only in M14.E).
  - There is no loss-streak gate and no duplicate-direction gate.

---

## 3. Gate-by-gate parity matrix

Coverage key: ✔ enforced · ◐ partial / conditional · ✘ absent.

| # | protection | legacy scanner path | M14 | gap on the scanner path | severity |
|---|---|---|---|---|---|
| 1 | global kill switch | ◐ B1 file (IBKR only) + B2 policy kill via D0 | ✔ `global_kill` | Two sources (file and policy DB) checked at different layers; PaperBroker/eToro paper ignore the file | MEDIUM |
| 2 | per-broker kill | ◐ B2 (D0) | ✔ `broker_kill` | the D0 ↔ M14 flag mapping is unverified | MEDIUM |
| 3 | auto-trading enabled (global/broker) | ◐ B2 (D0 signal-only mode) | ✔ `global_auto`, `broker_auto` | D0 semantics (time-boxed operator authorisation) ≠ M14 auto flags; needs an explicit mapping | HIGH |
| 4 | broker allowed (policy allowlist) | ✘ (env `BROKER` chooses; factory refuses `etoro_real`) | ✔ `broker_allowed` | no policy allowlist check | MEDIUM |
| 5 | authority ladder / governor downgrade | ✘ | ✔ `authority` + governor | **the scanner path has no authority concept** | **CRITICAL** |
| 6 | amount valid / minimum | ◐ L4 zero-size block | ✔ | no minimum amount | LOW |
| 7 | single-trade notional cap (USD) | ◐ via P3 (% of **config** portfolio) | ✔ `single_trade_cap` | legacy cap is relative to config, not equity; sizing can produce large notionals with tight stops | HIGH |
| 8 | broker / global capital cap | ✘ | ✔ | none | HIGH |
| 9 | combined exposure across all 4 scopes | ✘ | ✔ (unknown scope ⇒ block) | cross-broker exposure ignored | **CRITICAL** (live) |
| 10 | broker / global open positions | ◐ L5 / P2 / L2 (live) | ✔ (unknown ⇒ block) | L5/L6 **fail-open** on DB error | **CRITICAL** |
| 11 | daily loss (broker + global, latch, UTC day) | ◐ P1 (broker-local %; env override; paper fail-open) | ✔ | **env override bypass**; no global (cross-broker) loss cap | HIGH |
| 12 | drawdown throttle | ✘ | ✔ | none | HIGH |
| 13 | per-symbol concentration | ◐ P3 (% of config, estimated) | ✔ (USD) | duplicated limit, inconsistent units | MEDIUM |
| 14 | sector exposure | ✔ P4 (live) | ✘ (design-only) | **M14 lacks it**: migration must not drop it | MEDIUM (regression risk) |
| 15 | loss-streak cooldown | ◐ P5 (fail-open on error; outcomes-unknown does not block live by default) | ✘ | M14 lacks it; legacy is weak | MEDIUM |
| 16 | duplicate symbol/direction; broker position/open-order conflict | ✔ L6 + L2 (live) | ✘ (concentration only) | M14 lacks it: keep or port | MEDIUM |
| 17 | **market open (session, holidays, early close)** | ◐ L3 UTC weekday only | ◐ caller flag, **default True** | neither verifies market state; D1 market clock exists but is not wired here | **CRITICAL** |
| 18 | quote freshness / spread | ✘ (entry = last bar close) | ◐ only if supplied | no live quote check before entry | HIGH |
| 19 | risk-state data freshness (exposure / PnL ingestion) | ◐ live reconcile per call | ✔ `data_staleness` | paper has none; M14 depends on ingestion running (**VPS VERIFICATION REQUIRED**) | HIGH |
| 20 | **signal data truth** (stale / incomplete bars, corporate actions) | ✘ | ✘ | A01 (PR #12) measures it; not wired before risk | HIGH |
| 21 | live safety config | ✔ L1 + B3 | ◐ eToro-specific flags | keep B3 at the broker | — |
| 22 | paper port / gateway health | ✔ B4 / B5 / watchdog | n/a | infrastructure: keep outside M14 | — |
| 23 | decision ↔ order traceability | ◐ `risk_checks` JSON on the intent | ✔ `decision_id` + audit rows | no `risk_decision_id` on `execution_intents` (A02) | HIGH |
| 24 | single source of limits | ✘ env vars re-read per call | ✔ policy DB (`broker_allocation`) | the same limit is defined twice (max open, daily loss, symbol exposure) | MEDIUM |
| 25 | sizing | ✔ L4 (inside the risk check; mutates the intent) | input (`amount_usd`) | sizing must stay **before** M14 and feed `amount_usd` | — |

---

## 4. Missing protections by severity

**CRITICAL — must be closed before any M22 live step**
- **C-1 (#5, #9, #10, #12, #19):** no M14 decision on the scanner path. That means no authority ladder, cross-broker capital and exposure caps, drawdown throttle or risk-state freshness.
- **C-2 (#17):** market state is never verified. Legacy checks only the UTC weekday; M14 trusts a caller flag that defaults to `True`. (WS2 fixed this at its own boundary in A09; M14 itself still defaults to `True`.)
- **C-3 (#10, #15):** legacy fails open on state-read errors:
  - `_load_open_intents()` returns `[]` on any exception, which silently disables max-open and duplicate checks;
  - loss-streak read errors are treated as "not blocked".

**HIGH:**
- the daily-loss env override bypass, and no global loss cap;
- exposure as a percentage of the *configured* portfolio, not equity;
- no signal-truth gate (A01);
- no quote or spread check;
- no decision ↔ intent link;
- D0 ↔ M14 auto-flag mapping unverified.

**MEDIUM:**
- two kill-switch sources;
- duplicated limits;
- legacy-only protections that M14 lacks (sector, loss streak, duplicate/conflict);
- live does not block on outcomes-unknown by default;
- the B2b one-shot and flatten lanes are not M14-gated (operator lanes; authority level to be made explicit).

**LOW:** no minimum amount.

---

## 5. Duplicated or inconsistent rules

| limit | legacy source | M14 source | inconsistency |
|---|---|---|---|
| max open positions | `RISK_MAX_OPEN_POSITIONS` (L5 and P2 read it separately) | policy `broker_open_positions_cap`, `global_open_positions_cap` | two counts (intents-only vs bracket-aware), plus a third in M14 |
| daily loss | `RISK_MAX_DAILY_LOSS_PCT` / `_USD`, env override | policy broker/global daily-loss caps (USD) + latch | % of config vs USD; the override exists only in legacy |
| symbol exposure | `RISK_MAX_SYMBOL_EXPOSURE_PCT` (% of config) | `per_symbol_exposure_cap_usd` | units and base differ |
| kill switch | file `bot/kill_switch` (B1) | policy `global_kill_switch` / per-broker (B2, M14) | different stores; IBKR checks both, paper brokers only the policy |
| portfolio value | `RISK_PORTFOLIO_SIZE` | snapshot exposure / PnL from ingestion | config vs observed |
| market open | L3 weekday | `market_open` flag (default True) | neither observes the market |

---

## 6. Required fail-closed behaviour

Every row is a **block** with a stable reason code. Nothing here may fall back to "allow".

| missing / invalid state | required outcome | today |
|---|---|---|
| policy DB unreadable or invalid | block `policy_invalid` | M14 ✔; D0 skip ✔ |
| risk snapshot missing or stale for any scope | block `exposure_unknown` / `combined_exposure_unknown` | M14 ✔; legacy ✘ |
| PnL unknown (any mode that can place orders) | block `daily_pnl_unknown` | M14 ✔; legacy paper ✘ |
| `execution_intents` unreadable | block (new reason, e.g. `open_intents_unavailable`) | legacy **fail-open** |
| outcomes unreadable (loss streak) | block in live; explicit reason | legacy **fail-open** |
| sector unknown (live) | block | legacy ✔ (default) |
| kill-switch file unreadable | block (treated as active) | ✔ |
| market clock unavailable / stale / closed / holiday / inconsistent | block before preflight (A09 pattern) | ✘ |
| quote missing or stale (when quote gating is enabled) | block `quote_stale` | ✘ |
| governor / authority state missing | treat as `SIGNAL_ONLY` → block `authority_too_low` | n/a |
| A01 truth: stale / incomplete / unverified data | block before risk (policy decision: which states block) | ✘ |
| any exception in the risk path | block; no submit | legacy partly (live reconcile only) |

---

## 7. Integration points

1. **IP-1, `main.py` between step 5 and step 6.**
   - **The call:** `run_risk_preflight(conn, broker_scope=SCOPE[broker.name], request=TradeRequest(symbol, amount_usd=position_size × entry, side, sl, tp), current_authority=<governor state>, actor="scanner", audit_source="auto", market_open=<verified>, quote_age_sec=..., spread_bps=...)`.
   - `main.py` is sha-pinned (`test_m17_backtesting._PROTECTED_APPROVED_SHA256`), so this needs an explicit reviewed pin transition.
2. **IP-2, scope mapping.** `get_broker_name()` gives `paper | ibkr_paper | ibkr_live | etoro_paper`, while M14 scopes are `ibkr_paper | ibkr_live | etoro_paper | etoro_real`. The local JSONL `paper` broker has **no M14 scope**. **Decision needed:** treat it as simulation (excluded) or block it.
3. **IP-3, market state.** Reuse the A09 `verify_market_open` (fresh, typed D1 decision) to compute `market_open`. Separately, M14's default should change from `True` to "required, no default". That is an M14 change with its own tests.
4. **IP-4, authority source.** WS2 presents a constant `AUTO_ALLOWED`. The scanner path should read the **persisted governor authority**, so a downgrade (kill, loss latch) is honoured on the next decision.
5. **IP-5, signal truth (A01, PR #12).** The `TruthReport` gates the candidate before risk. `stale_data` and `schema_mismatch` block; the INCOMPLETE-bar policy is an operator decision.
6. **IP-6, traceability (A02).** An additive `risk_decision_id` on `execution_intents`, plus the candidate ledger linking candidate → score → decision → intent → outcome.
7. **IP-7, broker boundary (defence in depth).** `broker.submit` verifies a fresh, matching **allow** decision (symbol, side, amount ≤ approved, age ≤ N s) before placing. This makes a submit without a decision impossible even if a caller skips IP-1.
8. **IP-8, operator lanes.** For B2b one-shot and flatten, document the required authority (`ONE_SHOT_MANUAL` for an entry; `trade_close` ≥ `PAPER_ONLY`), and either route them through M14 or record an explicit, reviewed exemption.
9. **IP-9, ingestion.** M14 `data_staleness` needs M14.C/D ingestion of PnL and exposure for every scope. **VPS VERIFICATION REQUIRED:** whether ingestion runs, its freshness, and the current `RISK_*` env and `BROKER` values.

---

## 8. Test plan

**T-A, submit-path inventory (AST).**
- Every non-test call to `.submit(`, `submit_bracket(`, `submit_live(` or `placeOrder(` must be on an allowlist that names its gate chain.
- A new submit site fails the test until it is reviewed.

**T-B, scanner gate order and reachability.**
- Static: in `main.py`, `broker.submit` is reachable only after `RiskManager` pass → `PortfolioRiskPolicy` pass → `run_risk_preflight` allow → watchdog healthy.
- Dynamic: run the loop body with fakes. Each gate blocking alone prevents the submit, and the intent is logged `risk_rejected` with that gate's reason.

**T-C, parity replay (offline).**
- A fixture matrix with one scenario per legacy reason (L1–L6, P1–P5) and per M14 reason (24 codes).
- Run both on the same state.
- Assert the **composed verdict is `legacy AND M14`**: either one blocking means no submit.
- Report agreement and disagreement counts per reason.

**T-D, fail-closed matrix.**
- One test per row of §6.
- This includes regression tests for the two legacy fail-open bugs: `_load_open_intents` error, and loss-streak read error.

**T-E, bypass attempts.**
- Call `broker.submit` directly without a decision.
- Present forged decision objects (dict, look-alike, `True`).
- Reuse a stale decision.
- Use a decision for another symbol or side.
- Increase the amount after the decision.
- Set `RISK_ALLOW_DAILY_LOSS_OVERRIDE=true` (must not bypass M14).
- Pass a literal `market_open=True` (AST ban, as in A09).
- Present an authority constant above the governor state.
- Change env between decision and submit.

**T-F, close and flatten paths.**
- `trade_close` remains possible when entries are blocked: kill switch, loss latch, market closed.
- The kill-switch semantics for closes are explicit and tested.

**T-G, audit linkage.**
- Every submitted intent has a `risk_decision_id` that exists in `risk_decisions` with `result='allow'`.
- The decision matches the intent's symbol and side, its amount is no smaller than the intent's, and it was taken shortly before the submit.

**T-H, TOCTOU and sequencing.**
- Two signals in one cycle: the second decision sees the first intent's exposure.
- The kill switch is re-checked at the broker after an allow.

**T-I, M14 market default.** Once IP-3 lands, calling preflight without an explicit, verified `market_open` fails.

---

## 9. Migration approach (behaviour preserved until reviewed)

| phase | what | behaviour change | gate to proceed |
|---|---|---|---|
| 0 | this design | none | independent review |
| 1 | **offline parity harness** (T-C): replay fixture states and offline exports through both stacks | none (offline) | parity report reviewed; every disagreement explained |
| 2 | **shadow on the scanner path**: compute M14 (IP-1) and record `risk_decision_id` (IP-6). The verdict stays legacy. Flag default **off** | none to verdicts. Enabling it writes audit rows to the production DB, so it **needs operator approval** | shadow agreement reviewed on paper |
| 3 | **enforce `legacy AND M14`** + IP-3 market verification + IP-7 broker-boundary decision check. Flag default off; paper first | stricter only: M14 can add blocks, never remove one | paper evidence + review + approval |
| 4 | **fix legacy fail-open bugs** (C-3) with T-D tests | stricter only | review |
| 5 | **consolidate limits** into the policy DB (§5). Port or retain legacy-only gates (sector, loss streak, duplicate/conflict, live reconcile conflicts) as M14 gates or pre-gates. Retire a legacy gate only with parity evidence | per gate, reviewed individually | review + approval |
| 6 | M22 live readiness re-assessment | — | C01 closed by review |

**Rules for every phase:**
- **Never OR-compose:** an M14 allow must never override a legacy block.
- No gate is removed without parity evidence.
- No live configuration change without approval.
- Every phase ships with T-A and T-E green.

---

## 10. Open decisions for the operator

1. **Local JSONL `paper` broker:** treat it as M14-exempt simulation, or block it under M14 (IP-2)?
2. **Authority source for the scanner auto lane:** the persisted governor state (recommended) or a constant as in WS2 (IP-4)?
3. **A01 truth policy before risk:** which observation states block entries (at least STALE, INVALID, MISSING; INCOMPLETE?) (IP-5)?
4. **Operator lanes (B2b, flatten):** route through M14, or a reviewed exemption (IP-8)?
5. **`RISK_ALLOW_DAILY_LOSS_OVERRIDE`:** remove it, or confine it to paper only?

**Dependencies:**
- **A01** (PR #12) for IP-5.
- **A02** (candidate ledger) for IP-6.
- **A09** (PR #1) for the verified-market-state pattern used in IP-3.
- **VPS verification** of M14 ingestion and `RISK_*` env values (IP-9).
