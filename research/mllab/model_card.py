"""research.mllab.model_card — model cards for lab results.

A model card records what a model is, what it was trained and evaluated on,
how it compares with the simple baselines, and what it must NOT be used for.
Cards are generated from a ``LabResult`` only. Nothing is typed in by hand,
so a card cannot claim more than the evaluation showed.

The recommendation is bounded by design. A research card can say "keep the
baseline" or "candidate for pre-registered forward testing", and never
"deploy": promotion is outside the lab's authority (Issue #2, the approval
ladder).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from importlib.metadata import PackageNotFoundError, version as _dist_version
from typing import Dict, List, Optional, Tuple

from research.common.hashing import digest
from research.mllab.evaluate import LabResult

INTENDED_USE = ("Offline research on synthetic or approved historical data only. Not approved for live or paper "
                "trading, order generation or production scoring.")
BASE_LIMITATIONS = (
    "Out-of-fold metrics come from one walk-forward path; they are estimates with the uncertainty shown, "
    "not guarantees.",
    "Conformal coverage assumes exchangeability, which markets violate; coverage is measured, not assumed.",
    "Synthetic results describe the machinery, never the market. Net R is a counterfactual outcome, not P&L.",
)
RECOMMENDATIONS = ("keep the baseline", "candidate for pre-registered forward testing")
LIBS = ("numpy", "pandas", "scikit-learn", "scipy", "xgboost", "lightgbm", "catboost")


def library_versions() -> Dict[str, str]:
    """Installed distribution versions (read from package metadata; nothing is imported)."""
    out = {}
    for m in LIBS:
        try:
            out[m] = _dist_version(m)
        except PackageNotFoundError:
            out[m] = "not installed"
    return out


@dataclass(frozen=True)
class ModelCard:
    model: str
    scenario: str
    role: str                                   # "baseline", "prior" or "challenger"
    dataset_digest: str
    n_rows: int
    n_oof_rows: int
    n_features: int
    feature_digest: str
    label: Dict
    sampler: str
    protocol: Dict
    metrics: Dict[str, float]
    versus: List[Dict]
    report_card: Optional[Dict]
    trial_id: str
    n_trials_charged: int
    recommendation: str
    library_versions: Dict[str, str]
    limitations: Tuple[str, ...] = BASE_LIMITATIONS
    intended_use: str = INTENDED_USE
    extra: Dict = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return digest(asdict(self))


def build_card(res: LabResult, model: str, *, baseline: str = "logistic", protocol: Optional[Dict] = None,
               versions: Optional[Dict[str, str]] = None) -> ModelCard:
    if model not in res.runs:
        raise KeyError("model %r is not in this result" % model)
    role = "prior" if model == "prior" else "baseline" if model == baseline else "challenger"
    versus = [{k: r[k] for k in ("vs", "verdict", "holm_p_logloss", "holm_p_decision", "daily_r_diff",
                                 "daily_r_diff_lo", "daily_r_diff_hi")}
              for r in list(res.comparisons) + list(res.vs_prior) + list(res.meta.get("pairs", []))
              if r["model"] == model]
    rc = None
    if res.card is not None and res.card.candidate_id.endswith(":" + model):
        rc = {"verdict": res.card.verdict, "reasons": list(res.card.reasons)}
    beats_baseline = any(v["vs"] == baseline and v["verdict"] == "IMPROVES" for v in versus)
    rec = RECOMMENDATIONS[1] if role == "challenger" and beats_baseline else RECOMMENDATIONS[0]
    lim = BASE_LIMITATIONS
    if role == "challenger" and not beats_baseline:
        lim = lim + ("Did not show IMPROVES against the %s baseline: do not prefer it." % baseline,)
    if rc is not None and rc["verdict"] != "PASS":
        lim = lim + ("Integrity report card verdict %s." % rc["verdict"],)
    feats = list(res.meta.get("feature_names", []))
    dmeta = res.meta.get("dataset_meta", {})
    return ModelCard(
        model=model, scenario=res.scenario, role=role, dataset_digest=res.dataset_digest, n_rows=res.n_rows,
        n_oof_rows=int(res.meta.get("oof_rows", 0)), n_features=len(feats), feature_digest=digest(feats),
        label=dict(dmeta.get("barrier", {})), sampler=str(dmeta.get("sampler", "")),
        protocol=dict(protocol or {}),
        metrics={k: float(v) for k, v in res.runs[model].metrics.items()},
        versus=versus, report_card=rc, trial_id=res.trial_ids.get(model, ""),
        n_trials_charged=int(res.meta.get("n_trials_charged", len(res.runs))), recommendation=rec,
        library_versions=dict(versions if versions is not None else library_versions()), limitations=lim)


def render(card: ModelCard) -> str:
    m = card.metrics
    L = ["#### Model card: `%s` (%s, scenario `%s`)" % (card.model, card.role, card.scenario), "",
         "- **Intended use:** %s" % card.intended_use,
         "- **Recommendation:** %s" % card.recommendation,
         "- **Data:** dataset `%s`, %d labelled rows (%d out-of-fold), %d features (digest `%s`)" % (
             card.dataset_digest[:12], card.n_rows, card.n_oof_rows, card.n_features, card.feature_digest[:12]),
         "- **Label:** barrier %s, sampler `%s`" % (card.label, card.sampler),
         "- **Protocol:** %s" % (card.protocol or "see report"),
         "- **Trial:** `%s`; this family has been charged for %d trials" % (card.trial_id or "not ledgered",
                                                                           card.n_trials_charged),
         "- **Out-of-fold metrics:** AUC %.3f · log loss %.4f · Brier %.4f · ECE %.3f · trades %d · mean net R/trade "
         "%.3f · conformal coverage %.2f" % (m.get("auc", float("nan")), m.get("log_loss", float("nan")),
                                             m.get("brier", float("nan")), m.get("ece", float("nan")),
                                             int(m.get("n_trades", 0)), m.get("mean_net_r_per_trade", float("nan")),
                                             m.get("conformal_coverage", float("nan")))]
    for v in card.versus:
        L.append("- **vs `%s`:** %s (Holm p: probabilities %.3f, decisions %.3f; daily net-R diff %+.3f "
                 "[%+.3f, %+.3f])" % (v["vs"], v["verdict"], v["holm_p_logloss"], v["holm_p_decision"],
                                      v["daily_r_diff"], v["daily_r_diff_lo"], v["daily_r_diff_hi"]))
    if card.report_card:
        L.append("- **Integrity report card:** %s (%s)" % (card.report_card["verdict"],
                                                          "; ".join(card.report_card["reasons"]) or "no reasons"))
    L.append("- **Limitations:** " + " ".join(card.limitations))
    L.append("- **Libraries:** " + ", ".join("%s %s" % kv for kv in sorted(card.library_versions.items())))
    L.append("- **Card digest:** `%s`" % card.digest[:16])
    return "\n".join(L)


__all__ = ["INTENDED_USE", "RECOMMENDATIONS", "ModelCard", "build_card", "render", "library_versions"]
