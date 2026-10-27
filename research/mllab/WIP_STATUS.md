# Lane C (advanced model laboratory) — WORK IN PROGRESS checkpoint

**Do not merge.** Saved at the operator's request before a usage limit (27 Oct 2026). Stacked on lane B (`research/m21-b-integrity`).

## Done (code present, smoke-run manually, NO dedicated test module yet)
- `labels.py` — parametric barrier labels (gap-aware, ambiguous same-bar ties flagged, pending excluded).
- `datasets.py` — M18 feature groups (scale-free subset) + causal market-context features; M18 missingness policy; `drop_rule` / `oversold_rule` / `every_k` samplers; `audit_feature_asof` (perturb-the-future check: 0 violations in a manual run over 398,610 cells).
- `models.py` — prior, logistic, RF, ExtraTrees, HGB, XGBoost, LightGBM/CatBoost (optional), MLP; isotonic/Platt calibration on a purged chronological holdout; mean and stacked (purged OOF) ensembles; regime-gated model; bagged model; regime-blind `_noctx` variants.
- `uncertainty.py` — Mondrian split conformal sets, adaptive conformal inference, ensemble dispersion, risk-coverage.
- `evaluate.py` — purged walk-forward harness; walk-forward thresholds; conformal decisions; AUC / log loss / Brier / ECE; daily-aggregated paired tests with Holm; verdicts IMPROVES / PROBABILITIES_ONLY / DECISIONS_ONLY / NO_EVIDENCE / WORSE; ledgered trials; integrity report card for the best challenger.
- `benchmarks.py` — known-truth scenarios: null, linear, interaction (non-monotone in volume), regime.

## Manual smoke-run observations (synthetic; NOT a committed evidence report)
- **null:** no challenger improved on logistic or the prior.
- **linear:** trees were not better than logistic (WORSE on log loss).
- **interaction:** HGB and XGBoost beat logistic on probabilities (Holm p 0.018 / 0.005). Decision gains were not significant.
- **regime:** adding context improved logistic probabilities (Holm p 0.018). Decision gains were not significant.

## Remaining to finish lane C
1. `test_m21_research_mllab.py`. It should cover:
   - M18 triple-barrier parity on unambiguous rows;
   - the as-of audit;
   - leakage verification of the harness folds;
   - calibration and conformal coverage;
   - the verdict logic;
   - the null scenario producing no IMPROVES verdict.
2. Evidence CLI and `docs/research/evidence/lane_c_mllab.md`, including:
   - a lab null battery (false-improvement rate with a Clopper–Pearson interval);
   - ACI coverage;
   - ensemble-dispersion analysis.
3. Model card writer, targeted mutation pass, then convert to a normal draft PR.
