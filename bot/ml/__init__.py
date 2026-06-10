"""bot.ml — M18 machine-learning subsystem.

Status: read-only / shadow-only throughout M18.

This package provides the offline ML pipeline:
  schemas       — FeatureSpec, LabelSpec, DatasetConfig, TrainConfig,
                  plus the ALLOWED_* allowlists (locked at M18.A.1).
  errors        — M18Error hierarchy + registry-specific errors.
  dataset       — M16 bar loader, flywheel reader, anchor selection,
                  walk-forward splits, adversarial validation, the
                  dataset assembler, the coverage-degraded computation.
  features      — feature group implementations (M18.A.2 + M18.A.3).
  labels        — triple-barrier + 10 locked secondary labels (M18.A.4).
  models        — baselines (B0/B1/B2) and gated LightGBM trainer (M18.A.6).
  evaluation    — EvaluationReport v2 (M18.A.7).
  registry      — file-based model registry under data/ml/ (M18.A.8).
  cli           — argparse surface (M18.A.9, partial wiring).

INVARIANTS (hard-coded across M18, asserted by G10):
  - ML code never writes signals.db.
  - data/ml/ is gitignored; no model artifact is committed.
  - bot.historical is imported ONLY by bot.ml.dataset.m16_loader.
  - ALWAYS_FALSE_APPROVED_FOR_LIVE = False on every registry entry.
  - ML is read-only / shadow-only — no live promotion in M18.
"""
