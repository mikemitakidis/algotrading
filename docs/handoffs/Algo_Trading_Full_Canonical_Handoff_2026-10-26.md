# Algo Trading — FULL Canonical Project Handoff & Completion Register
**Snapshot:** 26 October 2026 (Europe/London) · **Version:** 3.0, full-scope replacement of the short research-only handoff · **Repo:** mikemitakidis/algotrading · **Dashboard:** algotrading.marketwarrior.club

> **Permanent rule:** This handoff covers every remaining capability from the June 2026 canonical roadmap through M23 and the two-broker operating objective, plus all still-open technical debt and the new advanced AI/self-development research architecture. **No delivery deadlines or calendar estimates.** Work proceeds by dependencies, testing evidence and operator approvals. Preserve original milestone names and numbers. A GitHub merge, a VPS deployment, a runtime activation and an independently validated trading edge are FOUR DIFFERENT STATES.

## 1. Mission / non-negotiable product goals

Build a sophisticated continuously learning trading research-and-execution platform that improves in BOTH dimensions, in parallel:
- **Evidence intelligence:** more point-in-time, trustworthy observations and counterfactual outcomes for accepted, rejected and blocked signals; realistic costs/fills; accurate effective sample sizes; immutable trial history; adversarial validation.
- **Advanced AI intelligence:** feature discovery, strong classical baselines, boosting/ensembles, meta-labeling, calibrated probability/uncertainty, regime conditioning, concept-drift monitoring, champion/challenger, safe autonomous multi-agent research, learning from failed hypotheses and published results. No model complexity earns an automatic promotion.
- **IBKR PAPER = permanent research lab**, even if a later eToro live lane exists. Scanner → score → risk → paper entry/exit → durable outcome → research/training → shadow validation must run reliably before paper results are trusted.
- **eToro LIVE = eventual restricted, operator-governed track-record lane.** Begin only with verified funded-broker capability, full risk parity, paper evidence, manual approvals and tiny capital; scale only on out-of-sample net performance, drawdown, execution reliability and legal/platform feasibility. A public/copyable record is an objective, **not a confirmed available API capability or income guarantee**.
- Continue producing clear LONG/SHORT technical signals distinctly from actual broker orders; do not conflate a SHORT-entry candidate with selling an owned position.

## 2. Sources and conflict-resolution rules

1. **Original business/milestone scope:** user-supplied “30.06.2026 Algo_Trading_Bot_Canonical_Roadmap.docx” (v2, document itself dated 29 June). It retains M21.U, M21.1, M21.1extra, M21.2–M21.7, M22.1–M22.6, M23.1–M23.5, four starter templates, promotion tiers and two broker lanes.
2. **Live code truth:** verified GitHub branches and exact SHAs below. Do not treat the historically dated ROADMAP.md or MILESTONE_STATUS.md header as up-to-date; both still describe D3 as “not started”, though its code merged in October. Update those docs as a separate, reviewable documentation task.
3. **Outstanding older debt:** docs/NEXT_WORK_REGISTER.md, M14 audit carry-forward, M17 real-intraday replay debt and older October C5a/roadmap handoff. Keep deliberate deferrals visible; don't silently mark them done.
4. **New R&D design:** Issue #2 and research/m21-autonomous-research-engine, including the independent critique on Issue #2. Integrate its 24 research capabilities into M21/M23 sub-work, not new top-level milestone numbers.
5. **This file + Issue #3** becomes the operational full handoff. Every subsequent handoff must refresh each status, last tested SHA, blockers, approvals and complete unfinished checklist; never replace this with only the next workstream.

## 3. Verified checkpoint — not a claim of runtime completion

| Scope | Verified state as of snapshot | Remaining gate |
|---|---|---|
| M1–M18, M19/M20 and M21.U | Foundations in main; historical implementation recorded as closed, with known deferred follow-ups | Reuse and extend; do not redo |
| C5a/C5b protected-baseline/test isolation | **Merged** into main, before latest October integration | Keep narrow exact-pinned guards |
| M21.1 signal scoring and scanner bridge | M19 research-grade scoring + enrichment bridge exist; no proven calibrated live win probability | Prove actual scanner-to-score parity, freshness, traceable ranked decisions |
| M21.1extra D0/D1/supervised D2 | Pre-existing safety groundworks delivered | Unattended lifecycle not activated |
| WS1 / D3 outcome metrics | **Code merged** to main at 015d2ce | Dashboard service NOT restarted; panel not yet runtime verified; genuine net outcomes absent |
| WS3 Telegram BUY/SHORT signal alerts | **Code merged** at 015d2ce | No live send, schedule or duplicate-notifier resolution approved |
| WS2 corrected unattended paper loop | **Draft PR #1**, branch m21-ws2-corrected-review @ 5d6654ddca989d2db25f59f4b46426e564ac27f5; code findings addressed, developer reports offline suite green | Operator merge permission, independent CI/review gates, separately approved genuine IBKR paper-adapter tests, gateway and activation |
| Research architecture | Proposal on research/m21-autonomous-research-engine @ fbf6e68966086fa595588e925cbb59e45e84668b; independent critique in Issue #2 | Revise proposal and implement isolated offline components, no research code merged yet |
| GitHub main | **015d2cea927b9b4db3ee65604e0b4ebb58211e61** | No changes to main without separate explicit approval |
| Last operator-reported VPS | Trader inactive; ibgateway failed; dashboard + Caddy running; no dashboard restart after WS1; no Telegram, no new IBKR orders; sync daemon off | Current runtime must be independently rechecked at activation, not guessed |
| M21.2–M21.7, M22, M23 | Unfinished or only supporting infrastructure exists | Completion tasks follow |

## 4. Full remaining-work inventory — carry every unchecked item until proved CLOSED

The task IDs below are **tracking IDs, not replacement milestone numbers**. A task is CLOSED only if GitHub SHA, exact test evidence and applicable runtime or broker evidence exist. Tasks can be developed in parallel in isolated branches; integration/promotion remains gated.

### A. Close the core M21.1 / M21.1extra loop

- [ ] **A01 · M21.1 scoring reality check.** Verify real scanner signals reach the M19 enrichment/scorer and return valid ranking, component breakdown, direction, explanation, timestamp and freshness; no fabricated calibrated probabilities, no execution grant from research scores. Automated real-fixture parity tests and source-aware error codes.
- [ ] **A02 · M21.1 candidate decision audit.** Persist every candidate stage, M19 gates/reasons, paper risk decision and outcomes with stable join IDs; distinguish rejected, watch, eligible, blocked and skipped; instrument effective data coverage.
- [x] **A03 · M21.1extra C5a/C5b controls.** October protected-file/test-isolation work merged. Preserve exact content-transition pins, no generic guard weakening.
- [x] **A04 · M21.1extra D0, D1, supervised D2 foundation.** Already shipped historically; do not confuse supervised proof with unattended activity.
- [x] **A05 · M21.1extra D3 WS1 code delivery.** Read-only authenticated performance metrics, honest N/A, fees and drawdown merged; runtime proof still in A06.
- [ ] **A06 · D3 dashboard operational acceptance.** Separately approved restart/deploy, authenticated GET-only dashboard/mobile inspection, genuine-vs-simulated and pending-fee N/A displays, no write/broker route, service/health unchanged.
- [x] **A07 · WS3 signal-alert code.** Signals-only Telegram outbox/cooldown/expiry/retry implementation merged; activation remains A08.
- [ ] **A08 · WS3 operational signal alerting.** Decide separate credentials and bot .env notifier overlap to prevent duplicates; dry-run/controlled send and scheduled source only after operator permission. Confirm BUY versus SHORT, stale signals, dedup, restart safety, security and observability.
- [ ] **A09 · WS2 controlled code integration.** Exact-head independent review of draft PR #1 (5d6654d), safeguarded fast-forward/merge only upon operator approval; no broker actions from code merge.
- [ ] **A10 · WS2 IBKR adapter acceptance.** On separately approved PAPER gateway only: verify account/port isolation, bracket orderRef across legs, permIds, completed/open order visibility across client IDs, real market data type/timestamps, broker errors, true paper fills/fees, and manual foreign-order coexistence. No real-money order.
- [ ] **A11 · WS2 crash/exit reconciliation acceptance.** Prove pending-submit and pending-flatten uncertainty survive disconnect/restart, no duplicate entry/close, late take-profit/stop and flatten fill, partial fills, fee delays, expired broker history, manual intervention on NEEDS_REVIEW, broker-owned-only cancellations and safe kill-switch.
- [ ] **A12 · WS2 scheduler/market clock and operational gating.** Isolated test → authorised paper shadow/read-only probe → explicit permission for unattended PAPER schedule. Multiple sessions, holidays, half-days, DST, market halts, rate limits, clock skew; D0 expiry and account equity/source freshness. Bot remains off absent approval.
- [ ] **A13 · True paper outcome capture.** Exact entry/exit quantity and VWAP, commissions final versus unknown, P&L net of costs, R-multiple, stop/target/time-stop taxonomy, drawdown, account/strategy attribution, provenance, trade counts and real vs simulator flags. Reconcile unmatched events to NEEDS_REVIEW, no invented results.
- [ ] **A14 · Production monitoring for paper research.** Reliable services, restart recovery, connectivity health, stale data detection, backups/restores, structured logs, alert escalation, capacity monitoring and durable journaling, tested without live-capital exposure.
- [ ] **A15 · Four original starter templates.** Implement/verify, independently, (1) trend-following multi-TF confluence; (2) mean-reversion RSI/Bollinger; (3) breakout/momentum volume/ATR; (4) pullback-in-trend. Validate exits and adjustable parameters, and make template identity explicit in outcomes. No unreviewed switch to paper/live.
- [ ] **A16 · Original dashboard strategy-controls scope.** Selected template, RSI/MACD/EMA/Bollinger/ATR/volume/confluence/stop/target/trailing parameters, validation, versioning, audit, rollback, least privilege. No unrestricted free-form user code or silent live parameter changes.
- [ ] **A17 · M17/M16 real-bar parity debt.** Confirm adequate real 15m/1h/4h/1d coverage and proper corporate actions, reproducible scanner_replica parity on actual historical candidate rows (not only synthetic). Preserve missing-data fail-closed semantics. Consolidate/retire legacy backtest engines only after proved equivalence.

### B. Evidence + integrity + more advanced AI, developed in parallel (M21.2 / M21.3 / M21.4–M21.7)

- [ ] **B01 · Historical data inventory.** Inspect *available* M16/M18 datasets, timestamps, frequency, gaps, market sessions, corporate actions and as-of universe membership; no private VPS export or paid vendor access without approval.
- [ ] **B02 · Data lake readiness.** Evaluate licensing-compatible survivorship-free/delisted constituent data and point-in-time fundamentals; Linux-compatible ingestion, quality/freshness audits and immutable snapshot hashes; propose vendors, do not purchase.
- [ ] **B03 · Counterfactual evidence v0.** Label accepted, rejected, gated, near-miss, historical anchors and shadow candidates for hypothetical path outcomes. Flag unobservable stop-vs-target order, executable-fill unknowns, incomplete prices, costs, overlapping windows and clusters. Never label EOD ambiguity as certain intraday fill or treat counterfactual as realised P&L.
- [ ] **B04 · Gate-economics/regret ledger.** Report observational avoided/foregone hypothetical R by each filter, holdout, threshold and Risk Authority reason. Do not infer causal benefit absent appropriate comparison.
- [ ] **B05 · Immutable experiment/trial registry.** All tried/failed/discarded hypotheses, family IDs, code/data/config/model hashes, splits, seeds, economic assumptions, CI report cards, contamination/research access ledger and verified references.
- [ ] **B06 · Sealed holdout and preregistration.** Separately permissioned holdout and access log; Git signed identity and independent/server-side timestamp attestation, frozen specifications and one-use final tests. Never rely on a mutable Git commit date or CODEOWNERS alone as security.
- [ ] **B07 · Anti-overfit testing suite.** Purging/embargo and genuinely forward splits, CPCV/CSCV PBO, deflated Sharpe with effective trial multiplicity sensitivity, White Reality Check/SPA as appropriate, reality checks for label overlap, walk-forward regime/market splits and transaction-cost stress.
- [ ] **B08 · Independent statistical canaries.** Large pre-registered known-null batteries, planted synthetic positives, negative controls (shuffled labels/placebos), proper uncertainty intervals on false pass/detection rates. Five nulls alone cannot estimate a ≤5% false-positive rate reliably.
- [ ] **B09 · Strategy Genome v0.** Declarative, constrained JSON/schema of existing templates, typed entry/exit/risk/timeframe, semantic IDs, lineage/variant accounting, fail-closed validator, reproducible M17 parity. Offline research only; no direct broker path.
- [ ] **B10 · Multi-symbol and portfolio-aware event backtesting.** Time-aligned data, borrow/short restrictions, capital/slot/exposure and correlations, spreads/slippage/gaps, survivor-free membership, next-open/intrabar ambiguity policies, deterministic fixtures, throughput benchmarking and parity with M17 where valid.
- [ ] **B11 · M21.2 parameter optimisation.** RSI, MACD, EMA/MA, Bollinger, ATR, volume, confluence, stops, targets and trailing variants. Search parameter *surfaces and plateaus*, not best isolated backtest; count every trial and compare no-trade/simple baselines.
- [ ] **B12 · M21.3 out-of-sample walk-forward optimiser.** Train/validation/testing fully separated; embargo overlapping labels; sealed final sample; cost-adjusted net expectancy with confidence intervals, drawdown, expected shortfall/CVaR, turnover, capacity and stability across market regimes. Win rate/avg-win-to-loss remain recorded but are not sole approval metrics.
- [ ] **B13 · M21.3 Promotion & Approval Model.** Preserve GREEN safer-only/AMBER material/RED increased risk/new champion distinctions, shadow-first, change budget, explicit reviewer/operator authorization for behaviour changes and fail-safe deactivation/rollback. Conservative changes must be proven genuinely safer (e.g. a tighter stop can increase realised losses through churn); never auto-promote risk.
- [ ] **B14 · Advanced ML laboratory NOW, not deferred.** Baseline logistic/linear/random-forest/boosted trees versus XGBoost/LightGBM/CatBoost and appropriate temporal/representation challengers; deterministic offline comparison, feature-as-of audit, proper train/calibration/test, model cards and reproducible experiments. No claimed real alpha from synthetic data.
- [ ] **B15 · Ensembles / model diversity.** Independently trained feature families, out-of-fold stacking, calibrated probabilities, robustness against correlated errors, ablations, calibration quality and honest abstention thresholds. Demonstrate incremental OOS gains over simpler models before promotion.
- [ ] **B16 · M21.4 meta-labeling.** Take/skip/downsize (no authority to widen risk limits) from point-in-time candidate/outcome labels, realistic costs and selection-bias correction; probability calibration, uncertainty interval, decision-curve economics and independent OOS testing.
- [ ] **B17 · M21.5 regime-aware thresholds.** Bull/bear/range, volatility/breadth/dispersion/liquidity states; causal as-of features and realistic detection lag. Start as shadow conditioning and risk throttles, not brittle instant regime switches.
- [ ] **B18 · M21.6 market context.** VIX/volatility term structure, breadth, sector rotation, index trend, macro and news/event calendars, earnings/central-bank proximity, delayed-revision control, licensing, source availability and event-time correctness.
- [ ] **B19 · M21.7 learned regime weights.** Model conditional contribution of indicators/context by regime, bounded/drift-aware adaptation, out-of-fold training and comparison with global weights; no automatic production scoring/risk override.
- [ ] **B20 · Self-improvement evaluation.** Measure model calibration, net return per R, abstention quality, false discovery, drift detection, research hypotheses discarded, agent cost per validated discovery and regression against the previous approved champion. Track outcome-lag and learning-data staleness.
- [ ] **B21 · Autonomous research knowledge library.** Source-verified citations, point-in-time paper knowledge cutoff, falsifiable hypotheses/mechanisms, strategy and trial lineage, rejection graveyard, semantic dedupe, re-test triggers and owner/audit accountability.
- [ ] **B22 · Research agents mock → gated proof.** Design Director/Scout/Hypothesist/Engineer/Skeptic/Librarian/Reporter and deterministic Auditor, allowed tools and writing areas; synthetic end-to-end mock first. Any paid/scheduled agent action, API key, GitHub workflow with broader permissions or autonomous PR creation requires separate approval. Agents never access broker credentials or independent evaluation vaults.
- [ ] **B23 · Continuous validation and decay monitoring.** Sequential change alarms, uncertainty/cost drift, overfit safeguards, predicted-versus-observed calibration and automated proposals to abstain/demote; quantify false-alarm/detection trade-off before auto-safe changes.
- [ ] **B24 · Cross-market / causal / alternative-data challenge.** Falsify US-discovered effects on UK/EU/HK when genuine equivalent data exist; evaluate point-in-time SEC/earnings/insider/short/option data, causal assumptions and event-time leakage. Do not claim transfer where coverage is absent.

### C. M22.1–M22.6 controlled real-money readiness: NO LIVE AUTHORIZATION

The June document groups M22.1–M22.6 without fixing a complete subnumber-to-deliverable mapping. **Retain M22.1–M22.6 exactly; reconcile precise sub-numbering with repo before implementation.** The following are required acceptance tasks under the M22 family, not an invented renumbering.

- [ ] **C01 · M14 scanner-path Risk Authority parity — HARD BLOCKER.** Every scanner-driven submit path must invoke or meet/exceed the full M14 gate set: broker/global capital, broker daily loss, combined exposure, drawdown throttle, per-symbol/sector concentration, quote/spread and data freshness, kill switches and unified policy-invalid semantics; AST and runtime tests.
- [ ] **C02 · Paper/live separation.** Broker accounts, ports, keys, data, signals, execution permissions, model promotion and kill switches separated; eToro live cannot be enabled by a research environment flag or paper-only workflow.
- [ ] **C03 · Production hardening.** Supervisors/auto-recovery, IBKR reconnection, stale price and positions rejection, external monitoring, safe deploy/rollback, test-in-isolation discipline, secrets/file permission audit, rotation/backup/restore drills, no unexpected auto-sync or restart on push.
- [ ] **C04 · Recovery under partial broker truth.** UNKNOWN orders/fills, double-submit prevention, account-wide manual activity separation, trade ownership, cancellations, OCO/bracket orphan detection, fee reconciliation and break-glass operator flow, fail closed.
- [ ] **C05 · Funded eToro broker/API feasibility proof.** Verify current eToro authorised access, order permissions, sandbox-vs-live differences, account residency, regulatory/platform terms and public copy-track-record support. No promise of automated eToro execution until independently proven.
- [ ] **C06 · Per-order human approval.** Order preview, sized maximum loss, spread/quote/position age, risk reasons, independent TOTP/step-up, atomic one-time authorisation, durable audit, revoked/expired approvals and operator cancellation.
- [ ] **C07 · Tiny live order permission gate.** Evidence of stable out-of-sample positive **net** expectancy (not just win rate), execution reliability, stress capacity, complete M14 parity, verified broker flatness/exposure and explicit operator permission for each first small funded order.
- [ ] **C08 · Post-trade funded reconciliation.** Actual broker confirmations, fees, taxes/FX where applicable, manual fills, net P&L, broker-corrected history, audit replay, displayed track record distinct from hypothetical/paper performance.
- [ ] **C09 · Controlled promotion / capital progression.** Explicit approval before every increase in size, autonomy, broker allocation or acceptable risk. Predefined daily loss, overall drawdown, exposures, currency and concentration caps; capital never scales just because of age.
- [ ] **C10 · Public track record and copying objective.** Check platform/regulatory eligibility, verifiable statements, strategy disclosure, copier-specific risks, operational transparency, drawdown limitations and honest profitability claims. No guaranteed income.
- [ ] **C11 · Persistent broker paper laboratory.** IBKR paper continues researching and challenging even when eToro live is approved; separate ledgers, scoring and risk policies, never assume research-edge transfers to execution fills.

### D. M23 advanced professional operating system — keep every original advanced task

- [ ] **D01 · M23.1 champion/challenger.** Multi-family/spec breeding, meaningful diversity not cloned factor variants, independent OOS/forward challenge, calibrated and cost-aware report cards, shadow champion pairing, explicit promotion and rollback.
- [ ] **D02 · M23.2 drift detection.** Feature/concept/label/cost regime shifts, sequential alerts, retraining proposals, anti-churn guards, degraded-data abstention and safe rollback/demotion.
- [ ] **D03 · M23.3 portfolio correlation/exposure.** Holdings/strategies/brokers correlations, sector/country/currency/beta/volatility/crowding, tail stress, capital and liquidity budgets, kill-switch and daily loss across all open orders and actual exposures.
- [ ] **D04 · M23.4 advanced macro/news intelligence.** Reliable event-time news, revision-aware macro/earnings/central-bank calendars, source scoring, natural language event extraction with model cutoff/leakage controls, provider-failure behavior and audited impact on outcomes.
- [ ] **D05 · M23.5 multi-strategy management.** Several reviewed strategies coexist under common risk authority, strategy-specific attribution/exposure/fees, champion selection, deactivation when deteriorating, dashboard side-by-side metrics and no unapproved live strategy switching.
- [ ] **D06 · Autonomous R&D organisation (within M23, parallel foundations in B).** Research agents propose new specs, experiments, feature ideas, tests and PRs on isolated branches; knowledge accumulation, bounded research-yield allocation, independent deterministic evaluator, canary-based safeguards, approval-only merge/promotion, and cost controls.
- [ ] **D07 · Advanced uncertainty and decision economics.** Confidence calibration, conformal/ensemble uncertainty where empirical coverage holds, expected net-R lower bounds, explicit no-trade option, strategy allocation based on evidence not raw scores, portfolio-aware opportunity costs.
- [ ] **D08 · Research versus operating twin.** Use a read-only/shadow digital twin to compare forecast fills, broker paper fills and real brokerage fills, explain discrepancies, report transaction cost analysis and capacity limits. No unsafe direct self-modifying order code.
- [ ] **D09 · Fully auditable mature operation.** Recovery drills, live/paper separation, signed model/spec manifests, reproducible source/data hashes, credible health alerts, daily independent broker reconciliation, operator runbooks and third-party reproducibility of performance where allowed.
- [ ] **D10 · Project-end acceptance.** All original M21/M22/M23 functions either accepted with evidence or transparently marked intentionally not deployed; capability dashboard, latest experiment/model registry, live/paper order trail, rigorous OOS risk metrics and complete incident/rollback proof. “Project finished” describes validated capabilities; permanent learning/operations continue indefinitely.

### E. Audit, data, security and historical carry-forward — do not lose this work

- [ ] **E01 · Older audit-P1-broker-permId-fallback.** Confirm intended legacy behavior versus bounded wait; no silent fallback; ensure WS2's new adapter does not wrongly mark the older path repaired.
- [ ] **E02 · audit-P1-portfolio-ctx-engine-bypass.** Check local-intent semantics and realistic populated contexts; distinct from the M14 scanner-path full-gate M22 blocker C01.
- [ ] **E03 · audit-P2-batch: 9 items.** Backtest engine duplication; historical scanner/backtest overlap; open-intents symbol/direction discrimination; eToro SignalOnlyBroker reason parity; M13.5 eToro event audit completeness; M14 decision/reason spelling; M14 gate default tightness; M15 manual-reset/export audit constraints; M14 sector-map config precedence. Audit against code and close only with evidence.
- [ ] **E04 · audit-P3-batch: 6 deferred items.** Preserve as an open batch; individually inventory exact six records from original audit before planning their fixes (not specified in the June canonical document).
- [ ] **E05 · Real M17 intraday E2E and provider quality.** Incomplete 15m/1h/4h coverage and mismatch risk, cross-provider latency, rate limiting, listing history, corporate actions and missing-data semantics; linked to A17/B01.
- [ ] **E06 · M15 deferred UX/security items.** Login latency investigation; persistent rate limiter only if warranted; multi-user/read-only roles **deferred indefinitely unless a concrete need arises**. Do not revive cosmetic dashboard work as a current priority.
- [ ] **E07 · Secret and backup permissions.** Prior historical report flagged an .env backup as overly permissive; check metadata only with operator permission and remediate via separately approved controlled action; do not read or expose credentials.
- [ ] **E08 · Canonical documentation reconciliation.** Update GitHub ROADMAP.md, MILESTONE_STATUS.md, NEXT_WORK_REGISTER and status snapshot to match **actual code/activation state**, preserving original milestone numbers, exact commit references, and deliberately deferred audit items. No accidental “D3 not started” after code merge.
- [ ] **E09 · Test and deployment governance.** Isolated no-secrets offline CI, protected paths, reviewer checks on exact head, deterministic tests and mutations, PR-only updates; no unreviewed auto-sync, service restarts or production DB writes.

## 5. Work order — parallelism without deadline promises

- **Immediate engineering lane 1:** finish review/paper integration preparation A01–A17 where OFFLINE possible; WS2 draft PR #1 kept isolated until operator approves merge, real paper gateway and runtime separately.
- **Immediate research lane 2:** implement B03–B08 evidence/integrity **and simultaneously** B14–B19 sophisticated AI harness/ensembles/regime work on synthetic and already authorised public or repository data. Do not wait for real fills before coding/test harnesses.
- **Immediate research lane 3:** implement B09/B10 strategy genome and parity, E agent mock/design, vendor/license memo B01/B02, and E08 documentation alignment on separate branches. Where data lacks coverage, report N/A and proceed with unblocked technical work.
- **Later once validated:** B11–B13 optimiser/governance; M21.4–M21.7 and M23 capabilities; C01–C11 operational/funded readiness subject to explicit evidence gates. Tasks need not be coded in a strictly serial sequence when files and risk surfaces are independent.
- Claude Chat/Code may use separate branches and draft PRs, complete as many safe offline tasks as possible, and independently prove tests; ChatGPT/Codex reviews exact heads; only the operator authorises merge and operational effects.
- An architecture/model is not “closed” just because it runs on synthetics, and a backtest win is not proof of funded positive expected value.

## 6. Rules for all future complete handoffs

1. Begin with objective + architecture + **both brokers** + evidence AND AI priorities; keep original M1–M23 milestone numbering unchanged.
2. Include every task A01–E09 (even deferred) with CLOSED / CODE_MERGED / TESTED_OFFLINE / PR_PENDING / RUNTIME_UNVERIFIED / BLOCKED / NOT_STARTED / INTENTIONALLY_DEFERRED, exact SHA and proof. Add tasks if new material defects emerge; never silently remove tasks.
3. Identify the exact current GitHub main, open PR heads, services, operator kill switch, data provenance and gateway state **only if reverified**; otherwise label last-known and date. Distinguish paper simulated trades, actual IBKR paper trades and eToro funded trades.
4. State who is working on which non-overlapping branch, outstanding review findings, what may proceed without approval and the next explicit operator gate.
5. Summarise the last applied change and its tests without restarting old broad audits; maintain links to original June roadmap, Issue #2, this handoff, audit backlog, and relevant pull requests.
6. No time estimates, deadlines or invented percentages. Evidence-driven gates decide progression. **No paid purchases, credentials, private data export, broker calls, live/paper orders, dashboard/Telegram enablement, VPS deployment or merges without the matching separate permission.**
7. **Hand off the FULL project**, not just the next sprint. Keep end-state functionality and unproved operational steps visible until accepted, even if current chat or engineering agent changes.

## 7. Links and current action

- **This full handoff:** GitHub Issue #3, and this file in its isolated documentation branch.
- **Original canonical plan:** user-supplied 29 June 2026 canonical roadmap (uploaded 30.06.2026).
- **Research discussion and proposal:** https://github.com/mikemitakidis/algotrading/issues/2
- **WS2 review:** https://github.com/mikemitakidis/algotrading/pull/1
- **Working research report:** https://github.com/mikemitakidis/algotrading/blob/research/m21-autonomous-research-engine/docs/research/M21_autonomous_research_engine_proposal.md
- **GitHub source:** ROADMAP.md; MILESTONE_STATUS.md; docs/NEXT_WORK_REGISTER.md.

**Claude's next authorised action:** Start A/B parallel offline research/code and comprehensive docs reconciliation; deliver focused test-backed draft PRs in separate research/* branches. Do **not** merge or alter production. Continue to next unblocked task without waiting for arbitrary calendar windows.

**Next operator decisions, only when needed:** WS2 code merge permission; IBKR PAPER gateway/order test permission; WS1 dashboard restart; WS3 Telegram send/activation; actual private data export; vendor spend; paid agent or scheduled activity; any eToro funded order. None is approved by this handoff.
