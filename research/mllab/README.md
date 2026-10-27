# `research.mllab`: lane C model laboratory (B14–B17, B19, D07)

One question, answered with evidence: **does a more sophisticated model genuinely improve out-of-sample, cost-adjusted results over a simpler one?** "No" is a valid answer, and the lab is tested on reaching it.

| module | contents |
|---|---|
| `labels.py` | Parametric barrier labels: gap-aware, same-bar ties flagged as ambiguous, pending rows excluded. Parity with M18 is tested. |
| `datasets.py` | M18 features (scale-free), causal market context, the M18 missingness policy, samplers, and the feature as-of audit (with a pluggable feature function for its negative control) |
| `models.py` | Model zoo, bit-reproducible per seed. <ul><li>Simple baselines: prior and logistic.</li><li>Single learners: RF, ExtraTrees, HGB, XGBoost, LightGBM, CatBoost and MLP.</li><li>Calibration: purged isotonic or Platt.</li><li>Ensembles: mean and stacked (purged OOF).</li><li>Others: a regime model, a bagged model and regime-blind `_noctx` twins.</li></ul> |
| `uncertainty.py` | Mondrian split conformal, adaptive conformal (ACI), static-vs-ACI online coverage, ensemble dispersion, risk-coverage |
| `evaluate.py` | Purged walk-forward harness with past-only thresholds and conformal sets, and daily-aggregated Newey–West paired tests with Holm. Verdicts are **skill-gated**: a gain over the baseline counts only if it also beats the base-rate prior. Trials are ledgered and the report card is ledger-reconciled. |
| `benchmarks.py` | Known-truth scenarios: null, linear, interaction, regime; `scaled()` sets effect strength |
| `audits.py` | M18 parity, the leaky-feature negative control, brute-force fold leakage |
| `model_card.py` | Generated model cards. The recommendation is bounded: never "deploy". |
| `evidence.py` | The reproducible report: `python -m research.mllab.evidence --out docs/research/evidence/lane_c_mllab` |

Tests: `python _test_isolation.py test_m21_research_mllab`. The evidence of record is `docs/research/evidence/lane_c_mllab.md`, which is synthetic and makes no market claim.
