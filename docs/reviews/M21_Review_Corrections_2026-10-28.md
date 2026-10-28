# M21 — corrections after the independent review of the accelerated delivery (28 Oct 2026)

**Inputs:**
- Issue #3 review record [#6475528106](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6475528106).
- PR reviews: #23 / 5686441635, #17 / 5686442374, #24 / 5686443135.
- The operator's 28 Oct review brief.

**Scope rule followed:**
- Correct the existing implementation and verify it; no new M21 features.
- Every fix below has a failing test or concrete code evidence behind it.
- No merges.
- No VPS, IBKR, CI, Telegram, Vercel, migration or dashboard activity.
- `main` is unchanged at `80b4343`.

---

## 1. Findings and their resolution

| # | Severity | PR | Finding | Resolution |
|---|---|---|---|---|
| F1 | HIGH (merge blocker) | #23 | `apply()` changed the template-controls store before the promotion transaction committed. Auto-revert rolled back before recording REVERTED and cooling-off. A crash between the two left inconsistent state. | **Resolved.** Durable cross-store operations, reconciliation, and a BLOCKED state (§4). |
| F2 | HIGH | #23 | One-active-change rule was read-then-insert (race). | **Resolved.** The check now runs inside the `BEGIN IMMEDIATE` insert transaction, and a partial unique index enforces it in the database. |
| F3 | MEDIUM | #23 | `record_shadow` INCONCLUSIVE recorded an event without checking the state. | **Resolved.** The state is checked inside the transaction; a wrong state gives an audited refusal and no event. |
| F4 | HIGH (merge blocker) | #17 | `reconcile()` fabricated an approved `risk_evaluated` (M14 PASS plus decision id) from a journal order. | **Resolved.** VERIFIED / RECONSTRUCTED_INFERRED / UNVERIFIED evidence (§5). |
| F5 | MATERIAL | #24 | Canary plan identity omitted the WFConfig, search space, universe, sessions, generator, costs, design and seeds. | **Resolved.** Canonical experiment identity bound into the plan and verified by the report card (§6). |
| A1 | HIGH (found in the §6 audit; pre-existing M17/M18 code) | #20 | M17 multi-timeframe backtests and M18 dataset builds read raw M16 bars, which are stamped at bar **start**, as **closes**. Higher-timeframe bars that had not yet closed leaked into earlier anchors. | **Resolved fail-closed at the real-data entrances** (§7). |
| A2 | MEDIUM | #24 | Walk-forward `origin="real"` documented, but did not enforce, close-stamped input. | **Resolved.** `require_close_stamped`. |
| A3 | MEDIUM | #25 | Market-context trend, sector and breadth features accepted start-stamped bars. | **Resolved.** Refused, and the feature is UNAVAILABLE. |
| A4 | MEDIUM | #24 | The walk-forward lumped NO_FILL with economics-INCONCLUSIVE trades (entered, exit price unknown) and removed both from equity. A PASS could rest on trades with unknown outcomes. | **Resolved.** Counted apart; unknown exits block PASS (§8). |
| A5 | LOW (observation) | #24 | Old canary `pipeline_id` used the decision-code digest, while walk-forward trials carry the search-code digest. The report card could never accept it (fail-closed, but useless). | Superseded by F5. |
| O1 | test flake (pre-existing) | — | `test_m17_backtesting.G9…byte_identical_report` failed once in 4 runs. | Not changed: no product defect shown. Recorded. |

No production safety assertion was weakened. Fixture changes were needed in two places:
- the M17 runner smoke tests;
- the Group F CLI threading test.

Both previously fed **24-hour start/pre-market** synthetic bars through the real-data entrance. They now use regular-session close-stamped bars, and the original fixture is asserted to be **refused**.

## 2. Corrections: old → new heads

| PR | WP | Old head | New head | What changed |
|---|---|---|---|---|
| [#17](https://github.com/mikemitakidis/algotrading/pull/17) | WP7 | `16ad7fb` | `4c1cac7` | F4 |
| [#21](https://github.com/mikemitakidis/algotrading/pull/21) | WP8 | `24a64a9` | `04970d4` | merge of #17 only (one conflict resolved: the WS2 tool wiring keeps both changes) |
| [#22](https://github.com/mikemitakidis/algotrading/pull/22) | WP9 | `bb1b571` | `ded303d` | merge only |
| [#23](https://github.com/mikemitakidis/algotrading/pull/23) | WP14 | `b671d92` | `28c1943` | F1–F3 (+ merge of #22) |
| [#20](https://github.com/mikemitakidis/algotrading/pull/20) | WP11 | `ac31ef8` | `5f86c30` | A1 + fixtures |
| [#24](https://github.com/mikemitakidis/algotrading/pull/24) | WP13 | `609aa85` | `aa00b12` | F5, A2, A4 (+ merges of #20) |
| [#26](https://github.com/mikemitakidis/algotrading/pull/26) | WP16–18 | `6fe2d1a` | `9cffb0a` | merges of #24 only |
| [#25](https://github.com/mikemitakidis/algotrading/pull/25) | WP15 | `b421903` | `d992a0f` | A3 |

Unchanged:
- #15 `5394882`, #16 `bf92678`, #18 `45784e7`, #19 `d7b8e66`;
- #12 `47e9057`, #1 `0f3f9e6`, #14 `68ca7b4`, #13 `6a8a690`.

All stacked branches were updated with **normal merges**. There were no rebases or force-pushes.

## 3. PR links

#12, #1, #14, #13 · L lane #15 → #16 → #17 → #21 → #22 → #23, plus #18 · R lane #19 → #20 → #24 → #26, plus #25 · integration-only branch `m21/integration` (not a PR, never to be merged).

## 4. Cross-store governance design (#23) and crash tests

The promotion DB and the controls DB are separate SQLite files; no transaction spans both.

- **Durable operation first.** `promotion_ops` is written in the same transaction that re-checks state, cooling-off and the 7-day budget. The row holds:
  - the op id (APPLY: proposal digest + attempt; REVERT: applied version + attempt);
  - `from_version` (the version active at intent time);
  - `intended_sha256`;
  - the exact `reason` stamped on the controls version, which is how recovery finds the version this op created.

  A REVERT intent records the cooling-off **in the same transaction**, before any rollback.
- **One open operation.** A partial unique index allows only one PENDING or BLOCKED op. While one is open, every other state change is refused (audited). A lease (10 min) stops a second process second-guessing a live driver.
- **`reconcile(controls)`** runs on start; `apply` and `monitor` call it first. It settles from what the controls store shows:

  | Op | Controls show | Outcome |
  |---|---|---|
  | APPLY | intended version active, sha matches | `APPLIED (recovered)` |
  | APPLY | pre-apply version active, intended never activated | `ABORTED`; orphan version withdrawn; proposal stays APPROVED. **Nothing is ever re-activated.** |
  | REVERT | pre-apply version active | `REVERTED` |
  | REVERT | applied version still active | the rollback is performed |
  | any | anything else: a third version, an unreadable or tampered store, the intended version activated and replaced outside the book, a rollback landing anywhere but the pre-apply version | `BLOCKED` |
  | MONITORING proposal | its version is no longer active | DIVERGENCE `BLOCKED` |

- **`resolve_block`.** Named human only. Accepted only when the controls store already shows the chosen outcome:
  - ROLL_FORWARD or ROLL_BACK;
  - a decay revert can never be rolled forward.

  Alerts (sent or failed) are audited and retryable.
- **Rollback safety.** A revert re-activates exactly the recorded pre-apply version (verified after the call) and never touches orders (`cancel_orders` is always False). A static test proves the controls store's only reader outside the promotion and templates packages is the WS2 **template-shadow** call. So apply and revert change shadow parameters only, never routing, sizing, brackets or exposure.

| Required scenario | Test |
|---|---|
| 1 crash before activation (3 points) | `test_01` |
| 2 crash after activation, before promotion commit | `test_02` |
| 3 promotion DB write failure | `test_03` |
| 4 controls DB write failure (propose / activate) | `test_04` |
| 5 crash during auto-revert (3 points) | `test_05` |
| 6 repeated apply after recovery, idempotent reconcile | `test_01`, `test_02`, `test_06` |
| 7 two simultaneous proposals (6 threads) | `test_07` |
| 8 two simultaneous approves and applies | `test_08` |
| 9 restart during cooling-off | `test_09` |
| 10 corrupted or unavailable config | `test_10` |
| 11 failed alert | `test_11` |
| 12 recovery with contradictory stores | `test_12`, `12b`–`12e`, plus "config changed between intent and activation" |

Also covered: `record_shadow` state, token binding and id reuse, database-level guards, and exposure isolation. **41 tests** (was 20); **21/21 targeted mutants killed**.

## 5. Risk-decision evidence policy (#17)

Every `risk_evaluated` payload carries `evidence`:

| Evidence | Written by | Verdict | Authorising? |
|---|---|---|---|
| `M14_LIVE` | `risk()`, with the decision id the M14 gate returned in-process before the intent | pass / reject | **yes** |
| `VERIFIED` | `reconcile()`, after reading the decision back **read-only** (`mode=ro`, `query_only`) from M14 `risk_decisions` and matching every field (see below) | pass | **yes** |
| `RECONSTRUCTED_INFERRED` | `reconcile()` with no M14 store configured or readable | `unverified` | no |
| `UNVERIFIED` | `reconcile()` when the decision is missing or contradicts the order; `risk()` given a pass without a decision id | `unverified` | no |

Fields matched for VERIFIED:
- decision id, `trade_open`, `ibkr_paper`, `allow`;
- symbol, side, leverage 1;
- an amount covering qty × reference price;
- taken before planning and at most 10 min earlier.

Rules:
- `intent()` is the mandatory pre-submit audit. It **refuses unless the latest risk evidence is authorising**, so a live pass without a decision id now blocks the entry. Mandatory-audit failure still blocks submission, as before.
- Only authorising evidence links `candidate_ledger.risk_decision_id`.
- Contradictions are counted and never repaired:
  - `contradiction:risk_rejected_but_ordered`;
  - `contradiction:risk_decision_id`;
  - `contradiction:terminal`.

  Terminals are never changed: the writer refuses.
- `journal_consistency` reports evidence per order and lists every non-authorising one, so no report can read inferred evidence as verified. Payloads written before the labels existed classify as INFERRED.
- The WS2 tool passes `m14_db=signals.db`.

Tests: 26 (was 16), covering 12 forged or contradictory approvals, an unreadable store, idempotent replay, terminal immutability, and the read-only reader. **25/25 mutants killed.**

## 6. Canary experiment identity (#24) and the preserved v2 battery

`canary.experiment_identity(template, cfg, protocol)` records:

| Section | Contents |
|---|---|
| `pipeline` | decision and trial code digests; the full WFConfig (economics and engine included); the template's search space |
| `data_protocol` | every `MarketSpec` parameter except the seed; planted effect and horizon; universe and sessions |
| `design` | nulls, effects, positives, required effects, false-pass target, alpha, min power, base seed, power rule |
| `seed_schedule` | the `child_seed` scheme and the plan name |
| `holdout_policy` | holdout fraction; sealed holdout never used |
| `costs_and_liquidity` | the economics model |

- It is part of `BatteryPlan` (and its digest) and recorded with its own digest. `pipeline_id` is bound to the pipeline section.
- **Report card.** It re-hashes the recorded plan. For `wf:` families (and any battery carrying an identity) it requires:
  - identity digest equal to the evaluation's `canary_experiment`;
  - family and candidate-trial code equal;
  - `pipeline_id` bound;
  - plan equal to its design.

  Otherwise NOT verified, so the card cannot PASS. Plans without an identity hash exactly as before; other research batteries are unaffected.
- **v2, preserved verbatim:** `docs/research/evidence/wp13_canary_v2_historical_609aa85.json` **on PR #24** (sha256 `7a6760a1…`).
  - Plan `16aaccc1…`, run at `609aa85`.
  - **0/59** null passes (upper bound 4.95 %); **20/20** detections at 0.5σ (lower bound 0.83); 30 symbols × 750 sessions.
  - It is valid evidence for that exact run. It is **not** acceptable calibration for the corrected pipeline: no identity in its plan, its ledger was not exported, and the code fingerprint changed.
  - The decision code under test (walk-forward, genome, economics) did not change in a way that affects synthetic decisions. Synthetic bars have no zero-volume exits (checked on 4 seeds; the generator has no zero-volume path), so the expectation is that v2's counts carry over. v3 tests that expectation instead of assuming it.
- **v3, preregistered, NOT run** (`PROTOCOL_V3`; `docs/research/WP13_canary_preregistration_v3.md` on PR #24):
  - identical design, new name (new seeds), no retuning;
  - the decision rule is fixed in advance;
  - it runs once, on the approved WP13 head, because the identity includes the code fingerprint. A run now would go stale with any further review fix.
  - The CLI now also writes the plan record, the experiment digest and the exported hash-chained ledger.

Tests: 28 in the optimiser suite (was 16). **21/21 mutants killed**, after 7 first-pass survivors each got a test.

## 7. Timestamp entrypoint audit (#20)

M16 stores provider bars at their **start**: yfinance daily rows at local midnight, intraday rows at the bar start. Every consumer below assumes **close** stamps unless stated.

The rule used for refusal: no regular-session US bar can close at or before 09:30 New York time, so a stamp there is a bar start or pre-market. It is exact on yfinance rows and zero on converted rows (tested).

| Entrypoint | Expected convention | How it is verified | Incomplete bars excluded | Conversion mandatory | Unsafe direct call | Tests |
|---|---|---|---|---|---|---|
| M16 `bot.historical.store.get_bars` (source) | provider START | n/a (it is the source) | **DATA_UNVERIFIED**: whether a refresh during the session stored a forming bar is checked by OP1 (last stamp vs refresh time) | n/a | n/a | — |
| M17 `load_backtest_bars`, single timeframe | ordering only (decide at bar i, fill at bar i+1 open) | — | as stored | no (no cross-time join) | allowed | M17 G1/G2 |
| M17 `load_multi_tf_bars` → runner → `MultiTimeframeContext` | CLOSE | start-stamp evidence across ≥2 timeframes | via conversion | **yes** | **refused** (`StartStampedBarsError`) | M17 G4 refusal + smoke; WP11 R11 |
| M17/M18 `scanner_replica`, `mtf_confluence`, Model-B anchors | CLOSE | through the entrances above and below | via conversion | yes | refused at the entrance | as above |
| M18 CLI `_default_bars_provider` → assembler | CLOSE | same check | via conversion | **yes** | **refused** | WP11 R11; Group F |
| M18 with an injected provider (tests/offline) | CLOSE | caller's duty (documented) | caller | caller | not checked | M18 suite |
| WP6 live scanner completed-bar seam | provider START → completed selection | injected calendar policy | **yes** (opt-in `completed` basis) | n/a | no policy → no signals | WP6 N1–N4 |
| WP11 `detect_convention` / `to_close_stamped` / readiness CLI | detects START/CLOSE; mixed/unknown refused | calendar evidence | rows outside the session dropped; 4H closes conservative | it *is* the conversion | mixed/unknown → refused | R2/R3: holidays, early close, DST, extended hours, 4H inexact |
| Research genome engine | CLOSE + `bar_open_ts` | `bar_open_ts` < `ts_utc` | via conversion | via labeler / walk-forward | real without `bar_open_ts` refused | genome, evidence suites |
| Walk-forward `run` / `run_template(origin="real")` | CLOSE + `bar_open_ts` | `require_close_stamped` | via conversion | **yes** | **refused** | O6 |
| Economics `liquidity_asof` | CLOSE (`ts_utc <= asof`) | through the walk-forward guard | via conversion | via walk-forward | direct callers: documented | O4, economics |
| Shadow ML / mllab labeler | CLOSE + `bar_open_ts` for real providers | `resolve_bar_opens` | via conversion | yes for real | real without `bar_open_ts` refused | mllab, shadow ML |
| WP15 trend / sector / breadth | CLOSE | same 09:30 rule | `ts_utc <= t` | yes | **refused → UNAVAILABLE** | X3 incl. a bar stamped exactly at the open |
| WP15 point-in-time observations (VIX, macro) | observation date, publication time, retrieval time and vintage are distinct | store invariants | unpublished values invisible | n/a | publication after retrieval refused | X1/X2 |
| Reports (walk-forward, readiness, canary) | label their origin | `origin`, synthetic banner | — | — | — | suites |

- 1D, 4H, 1H and 15m are covered across a DST change in R11; holidays and early closes in R3.
- **Consequence:** M17 multi-timeframe runs and M18 dataset builds on raw M16 data now **fail closed**. A converted-input path for them is a real-data prerequisite (§15).
- A bot-side converter would need the session calendar, which architecture guard G10 confines to `bot.scoring_truth`. The research path (WP11 output) is the supported route.

Mutants: 5/5 (entrances), 3/3 (WP15).

## 8. Economics verification (#19)

| Item | Status | Evidence |
|---|---|---|
| Split-adjusted vs raw shares | **verified** | real shares = adjusted ÷ factor-after-date; post-split holdings × ratio. Notional is invariant (checked algebraically; WP12 split tests). Raw basis without a complete corporate-action record → INCONCLUSIVE. |
| Fees before and after splits | **verified** | entry fees on pre-split real shares; exit fees on post-split real shares |
| Dividends, long and short | **verified** | ex-date in (entry, exit]; held real shares on the ex-date; + long / − short; skipped on all-adjusted prices (flagged approximate) |
| Borrow | verified; one **assumption** | unknown / unavailable / hard-without-rate → NO_FILL. Easy without a rate uses 0.25 %/yr, flagged. Charged on entry notional × nights / 360 (no daily mark) — an ASSUMPTION. |
| Commission minimum and maximum | **verified** | IBKR Pro Fixed: $0.005/share, minimum $1.00, maximum 1 % of value (source fetched 28 Oct; **re-verify at OP3**) |
| SEC/FINRA direction and dates | **verified** | sales only (long exit, short entry). Dated schedules; a date outside every window uses the most expensive known rate and is flagged unverified. **Re-verify the rates at OP3.** |
| Bid/ask | **assumption, uncalibrated** | dollar-ADV buckets, half-tick floor; a measured quoted spread replaces the bucket when present (M16 has no quotes) |
| Slippage and impact | **assumption, uncalibrated** | square-root law and volatility-scaled slippage. Calibrate against paper fills (`fee_calibration`, C-4). |
| ADV participation | **verified** | entries capped at min(1 % ADV, 10 % of bar $volume) → CAPPED; below one share → NO_FILL |
| Exits beyond liquidity | **verified + now disclosed** | priced at full size and flagged `exit_capacity_breach`; the walk-forward now counts these and lists them as a reason |
| Missing quotes / missing borrow | **verified** | bucket assumption / NO_FILL |
| Halts | verified; **DATA_UNVERIFIED** | halted or zero-volume entry → NO_FILL; exit → INCONCLUSIVE. M16 has no halt feed, so only zero-volume bars are detected. |
| Unfilled and partial fills | **verified** | partial entry = CAPPED; an exit cannot be partial (flagged) |
| Numerical stability | **verified** | finite and positive liquidity required; epsilon on share floors; no division by zero ADV |
| No-trade vs failed fill vs inconclusive | **fixed (A4)** | counted apart per fold and in the summary; unknown exits block PASS; FAIL still takes precedence |
| Direction coverage | noted | the walk-forward re-prices long trades only; all templates are long-only |

No profit claim rests on an unsupported fill. Every coefficient is labelled an assumption until calibrated against paper fills.

## 9. Scanner, scoring and paper-loop safety (#12/#15/#16, #1/#21/#22)

**C5b proof.** The blob chain is:

| File | main | after #12 | after #16 |
|---|---|---|---|
| `bot/scanner.py` | `4317e57` | `08dc751` | `581f2a7` |
| `bot/providers/alpaca_provider.py` | `944f391` | — | `0d756dd` |

The L-lane heads and the integration tree carry exactly the #16 blobs; the R lane carries the pre-WP6 blobs. The full diff against `main` is:

- **scanner:**
  - a stdlib-only `bot.bar_completion` import;
  - a metadata helper;
  - the opt-in `bar_basis` gate. The default `in_progress` path is unchanged; a missing policy means no signals;
  - regular-session / completed-bar selection applied only with a policy;
  - the `bar_asof` and `bar_selection` metadata;
  - `now_utc` taken from the policy when present;
- **Alpaca:** the `BAR_PRICE_ADJUSTMENT="raw"` constant, passed on both bar requests (Alpaca's documented default).

Nothing else changed. C5b is not weakened; merging still needs your approval.

**Scoring.**
- Scores never grant execution.
- An M14 approval counts only when read back from the persisted store (A01 `ReadOnlyM14DecisionStore`).
- Missing data fails closed.
- Covered by the CORE guard suite (A01, bridge, group D, WP5/WP6).

**Paper loop and templates.**
- Paper/live isolation: the port-4002 guard and account checks (D2 suites).
- Trusted clock and holiday gate; D0 permissions; crash recovery and duplicate claims; brackets with stable refs; foreign-order isolation; NEEDS_REVIEW.
- Template identity and versioning; shadow-only routing (`LIVE_TEMPLATES = {scanner}`); controls audit, rollback and corrupt-store fallback.
- A static scan of `bot/templates`, `bot/promotion`, `bot/candidate_ledger`, `bot/market_context`, `bot/bar_completion.py`, `bot/scoring_truth` and the template-shadow tool finds **no broker, order, network or messaging import**. There is no bypass into the scanner's live-submit path.
- Simulation stays separate from broker paper (`data_source`).

Pre-existing note: `bot/brokers/ibkr_broker.py` hard-codes a default **paper** account identifier for when `IBKR_ACCOUNT` is unset. This is not a secret and was not changed; moving it to the environment is a candidate for E07.

## 10. Context and ML status (#25, #26)

**WP15:**
- point-in-time store (observation, publication and retrieval times, vintages, append-only);
- lagged publication never later than retrieval;
- events visible only once announced;
- earnings UNAVAILABLE until G4;
- reduce-only throttle;
- new: start-stamped index bars refused.

**WP16–18:**
- purged OOF, with past folds resolved before each test fold;
- baselines (prior / take-all / global);
- calibration and drift reported;
- regime detection lag modelled, with unknown regime → global threshold;
- multipliers {0, 0.5, 1}, so **never a size increase**;
- `can_change_live` is always False, so there is no auto-promotion.

**Synthetic findings preserved, not tuned:** meta-labelling WORSE than take-all; regime thresholds and regime weights NO_EVIDENCE. Real-data runs are **DATA_UNVERIFIED** until OP1.

## 11. Integration tree

- `m21/integration` = `bcbcf01` (tree `03c8cbb`).
- Built by normal merges of #23 `28c1943`, #26 `9cffb0a` (includes #24 `aa00b12`, #20 `5f86c30`, #19), #25 `d992a0f` and #18 `45784e7` into the previous tree `a4791f6`.
- No conflicts.
- Integration-only: non-deploying, never to be merged.
- Cross-lane E2E: 24/24.

## 12. Test evidence

**Command:**
```
OMP_NUM_THREADS=1 python _test_isolation.py --all
```
Run from a detached checkout outside `/tmp`, with C5a isolation and broker/env variables unset.

**Environment:** Python 3.12.3, NumPy 1.26.4, pandas 2.2.3, SQLite 3.45.1, scikit-learn 1.8.0, SciPy 1.17.1, Flask 3.1.0, pyarrow 24.0.0.

**Results:**
- Integration `bcbcf01`: **129 modules: 128 OK, 0 failed, 1 with no tests (test_m12, by design)**.
- An earlier full run on `9b1a202` caught one regression: the Group F CLI test fed 24h bars through the new M18 guard. It was fixed in #20 `5f86c30` by giving that test session-close-stamped bars; the run above is green.
- `test_m12` reports NO TESTS by design, and must stay that way. It is an operator script: its live checks run only as `python test_m12.py --live` executed as `__main__`, and an import is C5a-isolated. Automated review must never run it with `--live`.
- Per-head runs (same environment):
  - #17 `4c1cac7`: 30 modules;
  - #23 `28c1943`: 35 modules;
  - #24 at `33ef652`: 7 modules;
  - #26 at `4c5b426`: 39 modules;

  all 0 failures, before the later #20/#24 audit fixes. Those were re-checked by module (M17 201, M18 670, readiness 39, optimiser 28, WP15 16, Group F 15) and by the full run above.
- **Mutation:** 75/75 targeted mutants killed this round (WP14 21, WP7 25, WP13 21, WP11 entrances 5, WP15 3). The 10 first-pass survivors were real test gaps; each now has a test.
- **Canary:** v2 historical evidence only; v3 preregistered, not run.
- **Experiment identity and ledger:** verified by the O10/O11 tests; the CLI-exported ledger is checked by `verify_jsonl`.

**Evidence classification:**

| Class | What it covers |
|---|---|
| Reproducible offline | Everything above |
| Mocked / synthetic | IBKR adapter / broker ports, scanner provider, M14 store fixtures, every research market, canary, ML pilots |
| Genuine historical | **none yet** (OP1) |
| Real IBKR paper | **none** (OP2/OP3) |
| Not yet verified | M16 forming-bar handling, halts, real costs, the paper gateway, the dashboard UI |

## 13. Remaining critical / high defects

- **None open from this review** in the code reviewed.
- Every fix awaits independent exact-head re-review.
- The canary needs the v3 run on the approved head.

## 14. Remaining M21 requirements

Unchanged scope:
- review and merge of 16 PRs;
- the dashboard UI for templates and controls (protected `dashboard/app.py`);
- the walk-forward report-card wiring for real runs, consuming `canary_experiment`;
- WS2 context shadow wiring;
- a converted-input path for M17 multi-timeframe and M18 real builds (or running them only through WP11 snapshots);
- WP19 / E08 reconciliation;
- the real-data reruns (WP11/13/16–18);
- OP1–OP5;
- the C-4 and C-5 reports.

## 15. Real-data and operational blockers

- **OP1 not run:**
  - no inventory;
  - M16 convention, coverage and forming bars all DATA_UNVERIFIED;
  - no exchange calendar export;
  - no corporate-action record, so raw-basis trades stay INCONCLUSIVE.
- M17 multi-timeframe and M18 builds on raw M16 data are blocked by design until converted.
- The IBKR gateway was last seen failed (OP2).
- No halt feed.
- Survivorship data decision (G4).
- Fee rates must be re-verified at OP3.

## 16. OP1–OP5 readiness

| OP | Status |
|---|---|
| **OP1** | Ready to run on approval. Runbook updated with OP1.6: read-only old-paper-system queries, syntax-checked, **not run**. They separate local simulation, broker-paper submissions, claimed vs proven fills, live-scope activity, positions, outcomes, fees (statement only), timestamps and source. |
| **OP2/OP3** | Runbooks now carry explicit **abort criteria** and **per-step rollback**. They need the merges first. |
| **OP4** | Unchanged. |
| **OP5** | Now starts with `PromotionBook.reconcile`; any BLOCKED stops. |

## 17. Merge sequence

1. #12 → #1 → #14 → #13.
2. L: #15 → #16 (C5b approval) → #17 → #21 → #22 → #23, plus #18.
3. R: #19 → #20 → #24 → #26, plus #25.

Each stacked base is retargeted by merging `main` in. Then run v3 on the merged WP13 code.

## 18. Remaining effort

- Review fixes for this round are done.
- Remaining engineering ≈ 13–16 dev-days plus review rounds:
  - converted-input path ≈ 2;
  - report-card wiring ≈ 1;
  - dashboard UI ≈ 4–5;
  - context/WS2 wiring ≈ 2;
  - WP19 ≈ 2;
  - real reruns ≈ 2–3.
- The calendar estimates in the Finish Line §7a stand: engineering ≈ 25 Nov; operational ≈ 1–5 Dec; full acceptance ≈ 22 Dec – 8 Jan. Each depends on OP1 by early November and the review cadence.

## 19. Approvals needed (grouped)

| Gate | What it covers |
|---|---|
| **G-review** | Independent exact-head review of the corrected PRs: #17, #23, #20, #24, #25 (and the merge-only #21, #22, #26). |
| **G1** | Scope freeze (unchanged). |
| **G2** | OP1 read-only session, now including OP1.6. |
| **G3** | Merges, then OP2/OP3 with the new abort and rollback rules. |
| **C5b** | Protected transitions for `bot/scanner.py` and `bot/providers/alpaca_provider.py` (#16). |
| **G4** | Data sources and survivorship. |

The v3 canary run (offline) needs no operational approval; it runs on the approved head.
