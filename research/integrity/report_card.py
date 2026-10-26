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
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Tuple

import numpy as np

from research.common.hashing import array_digest, digest
from research.integrity import stats as S
from research.integrity.controls import BatteryResult
from research.integrity.pbo import pbo as _pbo
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
             cfg: GauntletConfig = GauntletConfig()) -> ReportCard:
    x = np.asarray(gross, dtype=float).ravel()
    reasons_fail, reasons_inc = [], []
    nums: Dict[str, float] = {"n_obs": float(len(x)), "n_trials": float(n_trials),
                              "cost_per_obs": float(cost_per_obs)}
    if len(x) < 10 or not np.isfinite(x).all():
        return ReportCard(candidate_id, family_id, "INCONCLUSIVE",
                          ("fewer than 10 finite observations",), nums,
                          {"gross_digest": array_digest(x)}, asdict(cfg))
    lags = cfg.hac_lags
    neff = float(effective_n) if effective_n is not None else _effective_n(x, lags)
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
