"""research.integrity.report_card — the validation gauntlet and its report card.

One function, ``evaluate``, turns a candidate's per-observation outcomes
(e.g. net R per trade, or per-period returns) plus its family's trial count
and the pipeline's canary status into a ``ReportCard`` with a fail-closed
verdict:

* FAIL         any economic or statistical test fails (FAIL takes precedence:
               failing is always safe);
* INCONCLUSIVE no test failed but evidence is missing (too few effective
               observations, no calibrated canary battery, ...);
* PASS         every required test passed. PASS means "survived this offline
               gauntlet on this data"; it is NOT a validated or tradable edge.

Every number in the card is computed here from the inputs. ``unsupported_numbers``
lets reviewers (and the lane E Reporter) check that narrative text quotes only
numbers that are in the card.

Input provenance. ``n_trials``, ``effective_n``, the outcome series and the
family matrix are claims until proved. Two separate checks, both required for
PASS:

* ``ledger_metadata_reconciled`` (first review, P2): a ledger and its trial
  ids are supplied, every id belongs to the family, ``n_trials`` does not
  understate the ledger's family count, the matrix is no wider than
  ``n_trials``, and the chain verifies (against ``expected_tip`` when one is
  supplied from outside the research process).
* ``outcomes_verified`` (second review, P1): the evaluated ``gross`` series
  must be EXACTLY the series the candidate trial committed in its ledger
  record (``artifacts['gross']``), and every family-matrix column j must be
  exactly the series trial ``trial_ids[j]`` committed
  (``artifacts['family_series']``). Substituted or edited outcomes with
  genuine ledger ids therefore fail.

``provenance_verified`` is 1 only when both hold. Outcome provenance proves
the card evaluated what the ledger recorded; it does not prove the recorded
series came from the claimed computation (that requires recomputation from
the snapshot and code identity — see ``research.integrity.provenance``). A
caller-supplied effective N can only LOWER the internal estimate.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple

import numpy as np

from research.common.hashing import array_digest, digest
from research.integrity import stats as S
from research.integrity.controls import BatteryResult
from research.integrity.ledger import Ledger
from research.integrity.pbo import pbo as _pbo
from research.integrity.provenance import outcome_digest
from research.integrity.reality_check import hansen_spa, white_reality_check

DISCLAIMER = ("PASS means the candidate survived this offline gauntlet on this data. It is not a "
              "validated, realised or tradable edge, and synthetic results never describe markets.")


@dataclass(frozen=True)
class GauntletConfig:
    dsr_min: float = 0.95
    pbo_max: float = 0.25
    snooping_alpha: float = 0.05
    min_effective_n: int = 30
    cost_multipliers: Tuple[float, ...] = (1.0, 2.0)
    ci_level: float = 0.95
    n_boot: int = 1000
    hac_lags: Optional[int] = None
    pbo_blocks: int = 10
    require_canary: bool = True


@dataclass(frozen=True)
class ReportCard:
    candidate_id: str
    family_id: str
    verdict: str
    reasons: Tuple[str, ...]
    numbers: Dict[str, float]
    inputs: Dict[str, Any]
    config: Dict[str, Any]
    disclaimer: str = DISCLAIMER

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def digest(self) -> str:
        return digest(self.to_dict())

    def to_markdown(self) -> str:
        lines = ["### Report card — `%s` (family `%s`)" % (self.candidate_id, self.family_id), "",
                 "**Verdict: %s**" % self.verdict, ""]
        lines += ["- %s" % r for r in self.reasons] + [""]
        lines += ["| metric | value |", "|---|---|"]
        for k in sorted(self.numbers):
            v = self.numbers[k]
            lines.append("| %s | %s |" % (k, "n/a" if v is None or (isinstance(v, float) and np.isnan(v))
                                         else ("%.4g" % v)))
        lines += ["", "_%s_" % self.disclaimer]
        return "\n".join(lines)


def _effective_n(x: np.ndarray, lags: Optional[int]) -> float:
    se = S.newey_west_se(x, lags)
    sd = x.std(ddof=1)
    if not (se > 0 and sd > 0):
        return float(len(x))
    return float(min(len(x), (sd / se) ** 2))


def evaluate(*, candidate_id: str, family_id: str, gross, cost_per_obs: float, n_trials: int,
             rng: np.random.Generator, trial_sr_variance: Optional[float] = None,
             effective_n: Optional[float] = None, family_matrix=None,
             canary: Optional[BatteryResult] = None,
             cfg: GauntletConfig = GauntletConfig(), ledger: Optional[Ledger] = None,
             trial_ids: Sequence[str] = (), candidate_trial_id: Optional[str] = None,
             expected_tip: Optional[str] = None) -> ReportCard:
    x = np.asarray(gross, dtype=float).ravel()
    reasons_fail, reasons_inc = [], []
    if isinstance(n_trials, bool) or not isinstance(n_trials, (int, np.integer)) or n_trials < 1:
        raise ValueError("n_trials must be an integer >= 1, got %r" % (n_trials,))
    if effective_n is not None and not (np.isfinite(effective_n) and effective_n > 0):
        raise ValueError("effective_n must be finite and > 0 when given, got %r" % (effective_n,))
    meta_ok, meta_why = _provenance(ledger, family_id, n_trials, trial_ids, family_matrix, expected_tip)
    out_ok, out_why = (_outcomes(ledger, trial_ids, candidate_trial_id, x, family_matrix) if meta_ok
                       else (False, "metadata not reconciled"))
    prov_ok = meta_ok and out_ok
    nums: Dict[str, float] = {"n_obs": float(len(x)), "n_trials": float(n_trials),
                              "cost_per_obs": float(cost_per_obs)}
    if len(x) < 10 or not np.isfinite(x).all():
        return ReportCard(candidate_id, family_id, "INCONCLUSIVE",
                          ("fewer than 10 finite observations",), nums,
                          {"gross_digest": array_digest(x)}, asdict(cfg))
    lags = cfg.hac_lags
    neff_internal = _effective_n(x, lags)
    neff = min(neff_internal, float(effective_n)) if effective_n is not None else neff_internal
    nums["effective_n"] = neff
    nums["mean_gross"] = float(x.mean())
    block = max(1.0, len(x) / max(neff, 1.0))       # mean bootstrap block ~ design effect
    strictest = None
    for m in cfg.cost_multipliers:
        net = x - m * cost_per_obs
        est, lo, hi = S.bootstrap_ci(net, np.mean, rng, n_boot=cfg.n_boot, mean_block=block, level=cfg.ci_level)
        _, t, p = S.mean_test(net, lags)
        tag = "%gx" % m
        nums["mean_net_%s" % tag] = est
        nums["ci_lo_net_%s" % tag] = lo
        nums["ci_hi_net_%s" % tag] = hi
        nums["t_hac_net_%s" % tag] = t
        nums["p_hac_net_%s" % tag] = p
        strictest = (tag, lo)
    net1 = x - cost_per_obs
    d = S.deflated_sharpe_ratio(net1, n_trials=max(1, n_trials), sr_variance_across_trials=trial_sr_variance)
    nums.update({"sr_net_per_obs": d.sr, "psr_vs_0": d.psr0, "sr0_expected_max": d.sr0, "dsr": d.dsr,
                 "skew": d.skew, "kurtosis": d.kurt})
    nums["win_rate_diagnostic"] = float(np.mean(net1 > 0))
    wins, losses = net1[net1 > 0], net1[net1 < 0]
    nums["payoff_ratio_diagnostic"] = float(wins.mean() / -losses.mean()) if len(wins) and len(losses) else float("nan")
    nums["max_drawdown"] = S.max_drawdown(net1)
    nums["expected_shortfall_95"] = S.expected_shortfall(net1, 0.95)
    if family_matrix is not None:
        fm = np.asarray(family_matrix, dtype=float)
        try:
            nums["pbo"] = _pbo(fm, n_blocks=cfg.pbo_blocks).pbo
        except ValueError as exc:
            reasons_inc.append("PBO not computable: %s" % exc)
        spa = hansen_spa(fm, rng, n_boot=cfg.n_boot, mean_block=block)
        rc = white_reality_check(fm, rng, n_boot=cfg.n_boot, mean_block=block)
        nums["spa_p"] = spa.p_value
        nums["reality_check_p"] = rc.p_value
    if canary is not None:
        nums["canary_false_pass_upper"] = canary.false_pass_upper
        nums["canary_power_at_max_effect"] = canary.power[-1].rate if canary.power else float("nan")

    # FAIL conditions (fail-closed: any one suffices).
    if strictest is not None and not strictest[1] > 0:
        reasons_fail.append("net mean CI lower bound <= 0 at %s costs" % strictest[0])
    if not d.dsr >= cfg.dsr_min:
        reasons_fail.append("deflated Sharpe %.3f < %.2f with %d trials" % (d.dsr, cfg.dsr_min, n_trials))
    if "pbo" in nums and not nums["pbo"] <= cfg.pbo_max:
        reasons_fail.append("PBO %.2f > %.2f" % (nums["pbo"], cfg.pbo_max))
    if "spa_p" in nums and not nums["spa_p"] <= cfg.snooping_alpha:
        reasons_fail.append("SPA p-value %.3f > %.2f" % (nums["spa_p"], cfg.snooping_alpha))
    # INCONCLUSIVE conditions.
    if neff < cfg.min_effective_n:
        reasons_inc.append("effective N %.1f < %d" % (neff, cfg.min_effective_n))
    if cfg.require_canary and (canary is None or canary.verdict != "CALIBRATED"):
        reasons_inc.append("pipeline canary battery missing or not CALIBRATED")
    if family_matrix is None:
        reasons_inc.append("no family matrix: PBO / SPA not computed")
    nums["ledger_metadata_reconciled"] = 1.0 if meta_ok else 0.0
    nums["outcomes_verified"] = 1.0 if out_ok else 0.0
    nums["provenance_verified"] = 1.0 if prov_ok else 0.0
    nums["ledger_tip_anchored"] = 1.0 if (meta_ok and expected_tip is not None) else 0.0
    if not meta_ok:
        reasons_inc.append("inputs not reconciled with the immutable ledger (%s): local diagnostic only" % meta_why)
    elif not out_ok:
        reasons_inc.append("outcomes not tied to ledgered trial artifacts (%s): local diagnostic only" % out_why)
    if reasons_fail:
        verdict, reasons = "FAIL", reasons_fail + reasons_inc
    elif reasons_inc:
        verdict, reasons = "INCONCLUSIVE", reasons_inc
    else:
        verdict, reasons = "PASS", ["all gauntlet tests passed"]
    inputs = {"gross_digest": array_digest(x),
              "family_matrix_digest": array_digest(np.asarray(family_matrix, dtype=float)) if family_matrix is not None else None,
              "canary_plan_digest": canary.plan_digest if canary is not None else None}
    return ReportCard(candidate_id, family_id, verdict, tuple(reasons), nums, inputs, asdict(cfg))


def _provenance(ledger: Optional[Ledger], family_id: str, n_trials: int, trial_ids: Sequence[str],
                family_matrix, expected_tip: Optional[str] = None) -> Tuple[bool, str]:
    if ledger is None:
        return False, "no ledger supplied"
    if not trial_ids:
        return False, "no ledgered trial ids supplied"
    entries = {e.ref_id: e for e in ledger.entries("trial")}
    missing = [t for t in trial_ids if t not in entries]
    if missing:
        return False, "trial ids not in the ledger: %s" % missing[:3]
    foreign = [t for t in trial_ids if entries[t].payload["family_id"] != family_id]
    if foreign:
        return False, "trial ids from another family: %s" % foreign[:3]
    counted = ledger.family_trial_count(family_id)
    if n_trials < counted:
        return False, "n_trials %d understates the ledger's %d family trials" % (n_trials, counted)
    if family_matrix is not None and np.asarray(family_matrix).shape[1] > n_trials:
        return False, "family matrix has more columns than n_trials"
    if not ledger.verify(expected_tip).ok:
        return False, "ledger chain does not verify" + (" against the anchored tip" if expected_tip else "")
    return True, "reconciled"


def _outcomes(ledger: Ledger, trial_ids: Sequence[str], candidate_trial_id: Optional[str], x: np.ndarray,
              family_matrix) -> Tuple[bool, str]:
    entries = {e.ref_id: e.payload for e in ledger.entries("trial")}
    if candidate_trial_id is None:
        return False, "no candidate trial id: the evaluated series cannot be traced to a trial"
    if candidate_trial_id not in trial_ids:
        return False, "candidate trial id is not among the supplied trial ids"
    want = entries[candidate_trial_id].get("artifacts", {}).get("gross")
    if not want:
        return False, "candidate trial recorded no 'gross' outcome artifact"
    if outcome_digest(x) != want:
        return False, "evaluated gross series differs from the series the candidate trial committed"
    if family_matrix is not None:
        fm = np.asarray(family_matrix, dtype=float)
        if fm.ndim != 2 or fm.shape[1] != len(trial_ids):
            return False, "family matrix columns do not correspond one-to-one to the trial ids"
        for j, tid in enumerate(trial_ids):
            want = entries[tid].get("artifacts", {}).get("family_series")
            if not want or outcome_digest(fm[:, j]) != want:
                return False, "family matrix column %d differs from trial %s's committed series" % (j, tid)
    return True, "outcomes match ledgered artifacts"


_NUM = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")


def unsupported_numbers(text: str, card: ReportCard, small_int_max: int = 10) -> Tuple[str, ...]:
    """Numbers quoted in `text` that do not appear in the card (at the precision
    quoted, also as percentages) nor in its config. Small integers are allowed."""
    allowed = set()
    vals = [v for v in card.numbers.values() if isinstance(v, (int, float)) and np.isfinite(v)]
    vals += [v for v in card.config.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    for v in card.config.get("cost_multipliers", ()):
        vals.append(v)
    for v in vals:
        for scale in (1.0, 100.0):
            for nd in range(0, 5):
                allowed.add(round(v * scale, nd))
    out = []
    for tok in _NUM.findall(text):
        f = float(tok)
        if f.is_integer() and abs(f) <= small_int_max:
            continue
        nd = len(tok.split(".")[1]) if "." in tok else 0
        if round(f, nd) not in allowed and round(-f, nd) not in allowed:
            out.append(tok)
    return tuple(out)


__all__ = ["DISCLAIMER", "GauntletConfig", "ReportCard", "evaluate", "unsupported_numbers"]
