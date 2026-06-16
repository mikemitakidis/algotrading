"""bot.ml.readiness — M18.B.11 read-only model-readiness reporter.

ADVISORY / DIAGNOSTIC ONLY. This is NOT a promotion gate: promotion
remains owned by the B4 production thinness gates, the B8 artifact-
consistency check, and the registry rules. A `ready: true` here means
"diagnostic readiness looks reasonable", NOT "approved for live" and
NOT "promoted".

It CONSUMES an existing EvaluationReport (dict form, as persisted to
evaluation_report.json) plus optional registry/entry metadata. It does
NOT recompute metrics and does NOT import evaluator/trainer/registry
internals to recompute anything — it reads what is already stored.

B3 limitation history: stored calibration can be assessed. As of F1
(ISSUE-007) the stored isotonic artifact IS applied at predict time when
available; each PredictionResult carries predict_time_calibration_applied
and calibration_source so the output never implies calibration that was
not actually applied.

Primary metric for M18 is PR-AUC (higher is better); ROC-AUC also
higher-is-better; Brier is lower-is-better.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Advisory thresholds (documented; NOT promotion gates).
OVERFIT_WARN_DELTA = 0.15      # train-minus-test drop on a [0,1] metric
CALIB_ECE_WARN = 0.10          # expected calibration error ceiling
CALIB_MCE_WARN = 0.25          # maximum calibration error ceiling

# Metrics where train-minus-test is the overfit direction (higher better)
HIGHER_IS_BETTER = ("pr_auc", "roc_auc")


def _get_split_metric(
    ext: Dict[str, Any], split: str, key: str,
) -> Optional[float]:
    block = ext.get(split)
    if not isinstance(block, dict):
        return None
    v = block.get(key)
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # NaN guard
    if f != f:
        return None
    return f


def _overfit_gap(report: Dict[str, Any]) -> Dict[str, Any]:
    """train->val and train->test deltas on the primary metrics, where
    available. Positive delta = train better than holdout = potential
    overfit (for higher-is-better metrics)."""
    ext = report.get("ml_metrics_extended", {}) or {}
    gaps: Dict[str, Any] = {}
    suspected = False
    for metric in HIGHER_IS_BETTER:
        tr = _get_split_metric(ext, "train", metric)
        va = _get_split_metric(ext, "val", metric)
        te = _get_split_metric(ext, "test", metric)
        entry: Dict[str, Any] = {
            "train": tr, "val": va, "test": te,
            "train_minus_val": (None if tr is None or va is None
                                else round(tr - va, 6)),
            "train_minus_test": (None if tr is None or te is None
                                 else round(tr - te, 6)),
        }
        tmt = entry["train_minus_test"]
        if tmt is not None and tmt > OVERFIT_WARN_DELTA:
            suspected = True
            entry["overfit_warn"] = True
        else:
            entry["overfit_warn"] = False
        gaps[metric] = entry
    return {"per_metric": gaps, "overfit_suspected": suspected,
            "warn_delta": OVERFIT_WARN_DELTA}


def _calibration_verdict(report: Dict[str, Any]) -> Dict[str, Any]:
    cal = report.get("calibration", {}) or {}
    test = cal.get("test") if isinstance(cal, dict) else None
    if not isinstance(test, dict):
        return {"available": False,
                "reason": "no test calibration block in report"}
    ece = test.get("expected_calibration_error")
    mce = test.get("maximum_calibration_error")
    well = True
    problems = []
    try:
        if ece is not None and float(ece) == float(ece) \
                and float(ece) > CALIB_ECE_WARN:
            well = False
            problems.append(f"ECE {float(ece):.4f} > {CALIB_ECE_WARN}")
    except (TypeError, ValueError):
        pass
    try:
        if mce is not None and float(mce) == float(mce) \
                and float(mce) > CALIB_MCE_WARN:
            well = False
            problems.append(f"MCE {float(mce):.4f} > {CALIB_MCE_WARN}")
    except (TypeError, ValueError):
        pass
    return {
        "available": True,
        "expected_calibration_error": ece,
        "maximum_calibration_error": mce,
        "brier_score": _get_split_metric(
            report.get("ml_metrics_extended", {}), "test",
            "brier_score"),
        "well_calibrated_stored": well,
        "problems": problems,
        # B3 honesty — assesses STORED calibration quality. As of F1
        # (ISSUE-007) the stored artifact IS applied at predict time when
        # available; this verdict only scores its quality, not application.
        "note": "assesses stored calibration quality; applied at predict when available",
    }


def _baseline_verdict(report: Dict[str, Any]) -> Dict[str, Any]:
    """Read a baseline comparison IF the report carries one. The
    per-model EvaluationReport does not itself store a
    BaselineComparisonReport, so this is typically 'unavailable' —
    reported honestly rather than recomputed (recompute would need
    retraining baselines, which is out of B11 scope)."""
    bb = report.get("baseline_beats")
    if not isinstance(bb, dict) or not bb:
        return {"available": False,
                "reason": "no baseline_beats in this report "
                          "(per-model report does not embed the "
                          "baseline comparison; run compare_baselines "
                          "separately)"}
    beats_primary = all(bool(v) for v in bb.values())
    return {"available": True, "baseline_beats": bb,
            "beats_all": beats_primary}


def _regime_verdict(report: Dict[str, Any]) -> Dict[str, Any]:
    bd = report.get("breakdowns", {}) or {}
    test = bd.get("test") if isinstance(bd, dict) else None
    if not isinstance(test, dict):
        return {"available": False,
                "reason": "no test breakdowns in report"}
    weak: List[str] = []
    thin: List[str] = []
    available_groups = []
    for group in ("per_symbol", "per_year",
                  "volatility_regime", "market_regime"):
        block = test.get(group)
        if not isinstance(block, dict):
            continue
        if not block.get("available", False):
            continue
        available_groups.append(group)
        skipped = block.get("skipped_segments", []) or []
        for sk in skipped:
            if isinstance(sk, dict) and \
                    sk.get("reason") == "below_min_samples":
                thin.append(f"{group}:{sk.get('segment')}")
    return {
        "available": len(available_groups) > 0,
        "available_groups": available_groups,
        "thin_segments": thin,
        "weak_segments": weak,
    }


def _thinness_verdict(
    report: Dict[str, Any],
    entry_meta: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    eligible = report.get("promotion_eligible")
    blocked = report.get("promotion_blocked_reasons", []) or []
    production_blocked = [
        r for r in blocked
        if isinstance(r, str) and r.startswith("production:")
    ]
    return {
        "promotion_eligible": eligible,
        "promotion_blocked_reasons": list(blocked),
        "production_blocked_reasons": production_blocked,
        "n_train": report.get("n_train"),
        "n_val": report.get("n_val"),
        "n_test": report.get("n_test"),
    }


def assess_readiness(
    report: Dict[str, Any],
    *,
    entry_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Aggregate an advisory readiness summary from a stored
    EvaluationReport dict. Pure function: no I/O, no mutation."""
    overfit = _overfit_gap(report)
    calibration = _calibration_verdict(report)
    baseline = _baseline_verdict(report)
    regime = _regime_verdict(report)
    thinness = _thinness_verdict(report, entry_meta)

    reasons: List[str] = []     # reasons ready would be false
    warnings: List[str] = []    # advisory, do not flip ready

    if overfit["overfit_suspected"]:
        reasons.append(
            f"overfit_suspected: train-minus-test > "
            f"{OVERFIT_WARN_DELTA} on a primary metric")
    if calibration.get("available") and \
            not calibration.get("well_calibrated_stored", True):
        reasons.append(
            "stored_calibration_weak: " +
            "; ".join(calibration.get("problems", [])))
    if baseline.get("available") and not baseline.get("beats_all", True):
        reasons.append("does_not_beat_all_baselines")
    if not baseline.get("available"):
        warnings.append(
            "baseline_comparison_unavailable_in_report")
    if regime.get("available") and regime.get("thin_segments"):
        warnings.append(
            f"thin_regime_segments: {regime['thin_segments']}")
    if not regime.get("available"):
        warnings.append("regime_breakdowns_unavailable")
    if thinness.get("production_blocked_reasons"):
        reasons.append(
            "production_thinness_blocked: " +
            ", ".join(thinness["production_blocked_reasons"]))
    # F2 / ISSUE-017: adjusted-price point-in-time leakage gate. If the
    # dataset was built on adjusted prices without the explicit allow flag,
    # the assembler records adjusted_price_pit_risk in
    # promotion_blocked_reasons; readiness must NOT call such a dataset ready.
    _blocked = report.get("promotion_blocked_reasons", []) or []
    if "adjusted_price_pit_risk" in _blocked:
        reasons.append(
            "adjusted_price_pit_risk: dataset uses adjusted prices "
            "(synthetic O/H/L, point-in-time leakage risk) without "
            "allow_adjusted_prices_for_ml=True")

    ready = len(reasons) == 0

    return {
        "ready": ready,
        "reasons": reasons,
        "warnings": warnings,
        "overfit_gap": overfit,
        "calibration": calibration,
        "baseline": baseline,
        "regime_coverage": regime,
        "thinness": thinness,
        "limitations": [
            "readiness is advisory/diagnostic only",
            "cross-fold feature-importance stability deferred "
            "(B11.x / M21-style walk-forward work)",
            "speed/timing is reported elsewhere and is not a "
            "pass/fail criterion",
        ],
        # explicit, non-misleading disclaimers
        "readiness_is_advisory": True,
        "promotion_gate": False,
        # This readiness REPORT does not itself apply calibration (it only
        # reads stored artifacts) — so this flag stays False for the report.
        "predict_time_calibration_applied": False,
        # F1 / ISSUE-007: the PREDICT PATH (bot.ml.registry.predictions) now
        # applies the stored isotonic artifact when available; each
        # PredictionResult carries the per-batch truth. This companion flag
        # records that capability without changing the report-level field.
        "predict_path_applies_calibration_when_available": True,
    }
