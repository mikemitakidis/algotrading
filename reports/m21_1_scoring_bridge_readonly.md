# M21.1 — Score & Rank (research-grade, read-only)

- report_type: **M19 score + rank over scanner signals (via the M21.1 scoring bridge)**
- data_source: **simulated_fixture**
- scoring_profile: **RESEARCH**
- engine: **bot.signal_scoring (M19 public API; gates.py model_readiness downgraded to REVIEW under RESEARCH only)**
- signals_scored: **4**
- execution_eligible_any: **false** (must be false)

> **Honesty statement (read this).** These are RESEARCH-GRADE rankings, NOT calibrated live probabilities and NOT execution approval. ML readiness is NOT passed — no model has been trained on real outcome data yet (that is M21.1extra). Under the RESEARCH profile, 'model not ready' and 'calibration unavailable' are MANUAL_REVIEW, so candidates can be ranked by component quality. Under the STRICT (live) profile these same candidates remain hard-BLOCKED. execution_eligible is False on every candidate. No runtime / broker / live / paper / Telegram path is touched.

## Ranked candidates (by composite score)

| rank | symbol | side | score | decision | confidence | gate | exec_eligible |
|---|---|---|---|---|---|---|---|
| 1 | `CCC` | LONG | 33.72 | REJECT | LOW | review/block | false |
| 2 | `AAA` | LONG | 33.41 | REJECT | LOW | review/block | false |
| 3 | `BBB` | LONG | 18.85 | REJECT | LOW | review/block | false |
| 4 | `DDD` | LONG | 0.20 | REJECT | LOW | review/block | false |

## Component breakdown (0–100 each)

| symbol | ml | scanner | technical_confluence | trend | momentum | volume_liquidity | volatility | market_regime | risk_adjusted | data_quality | calibration_uncertainty |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `CCC` | 25.0 | 90.0 | 65.8 | 50.0 | 85.0 | 95.0 | 90.0 | 50.0 | 90.0 | 100.0 | 60.0 |
| `AAA` | 25.0 | 90.0 | 61.8 | 50.0 | 85.0 | 95.0 | 90.0 | 50.0 | 90.0 | 100.0 | 60.0 |
| `BBB` | 25.0 | 50.0 | 61.8 | 50.0 | 85.0 | 70.0 | 90.0 | 50.0 | 90.0 | 100.0 | 60.0 |
| `DDD` | 25.0 | 0.0 | 41.8 | 50.0 | 25.0 | 65.0 | 90.0 | 50.0 | 90.0 | 100.0 | 60.0 |

## Why `CCC` ranks above `DDD`

- composite score delta: **33.51**
- top component advantages:
  - `scanner`: **+90.00**
  - `momentum`: **+60.00**
  - `volume_liquidity`: **+30.00**
  - `technical_confluence`: **+24.00**

## Safety confirmation

- research-grade only; STRICT/live remains hard-blocked until a real trained model exists
- ML readiness NOT passed; no calibrated probability invented; prediction_calibrated stays null
- execution_eligible = False on every candidate
- no runtime wiring; no main.py change; no IBKR paper order; no eToro; no broker / live / paper; no Telegram
- M19 public API unchanged (44 names); only gates.py behaviour for RESEARCH model_readiness changed
