"""research.integrity.controls — calibrating the research pipeline itself.

A pipeline (any callable that takes a dataset and decides "edge found / not
found") is only trustworthy if, at the trial counts it actually uses:

* on many KNOWN-NULL datasets it rarely passes — reported as a false-pass
  rate with an exact one-sided upper bound (Clopper–Pearson). Zero passes in
  n independent nulls bounds the rate at 1 - alpha^(1/n): 45 % for n = 5,
  4.95 % for n = 59 (Issue #2 critique #2);
* on datasets with a PLANTED effect of known size it detects the effect,
  reported as a power curve with intervals.

The battery plan is written to the ledger BEFORE any result exists, and the
result after. A plan digest can be used ONCE: re-running the same plan is
refused (PR #5 review, P2), so a battery cannot be silently repeated until it
looks good. Power is judged on the exact Clopper–Pearson LOWER bound of the
detection rate at EVERY required effect size, not on a point estimate.

Negative-control helpers (label block-permutation, placebo events, circular
shifts) build nulls from real data, where synthetic nulls are not enough.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

import numpy as np

from research.common.hashing import digest
from research.common.rng import child_seed
from research.integrity.ledger import Ledger, LedgerError
from research.integrity.stats import clopper_pearson

VERDICTS = ("CALIBRATED", "NOT_CALIBRATED", "UNDERPOWERED")


@dataclass(frozen=True)
class PipelineDecision:
    passed: bool
    statistic: float = float("nan")
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BatteryPlan:
    name: str
    pipeline_id: str
    n_nulls: int
    false_pass_target: float = 0.05
    alpha: float = 0.05
    effect_sizes: Tuple[float, ...] = ()
    n_per_effect: int = 0
    min_power: float = 0.8
    base_seed: int = 1000
    required_effects: Tuple[float, ...] = ()     # default: the largest declared effect

    def __post_init__(self):
        if self.n_nulls < 1:
            raise ValueError("n_nulls must be >= 1")
        if self.effect_sizes and self.n_per_effect < 1:
            raise ValueError("n_per_effect must be >= 1 when effect sizes are given")
        if any(e not in self.effect_sizes for e in self.required_effects):
            raise ValueError("required_effects must be among effect_sizes")

    @property
    def required(self) -> Tuple[float, ...]:
        if self.required_effects:
            return tuple(self.required_effects)
        return (max(self.effect_sizes),) if self.effect_sizes else ()

    @property
    def digest(self) -> str:
        return digest(asdict(self))

    def min_nulls_for_target(self) -> int:
        """Smallest n with zero passes bounding the false-pass rate at the target."""
        n = 1
        while 1.0 - self.alpha ** (1.0 / n) > self.false_pass_target:
            n += 1
        return n


@dataclass(frozen=True)
class PowerPoint:
    effect: float
    n: int
    detections: int
    rate: float
    lo: float
    hi: float


@dataclass(frozen=True)
class BatteryResult:
    plan_digest: str
    n_nulls: int
    null_passes: int
    false_pass_rate: float
    false_pass_upper: float
    null_calibrated: bool
    power: Tuple[PowerPoint, ...]
    power_ok: bool
    verdict: str
    notes: str = ""
    power_rule: str = "cp_lower_bound_at_every_required_effect"


def run_battery(plan: BatteryPlan, pipeline: Callable[[Any], PipelineDecision],
                make_null: Callable[[int], Any],
                make_positive: Optional[Callable[[float, int], Any]] = None,
                ledger: Optional[Ledger] = None) -> BatteryResult:
    if ledger is not None:
        if ledger.entries("control_battery_plan", plan.digest) or ledger.entries("control_battery_result", plan.digest):
            raise LedgerError("battery plan %s was already used; register a new plan (new name or seed)"
                              % plan.digest[:12])
        ledger.append("control_battery_plan", {**asdict(plan), "plan_digest": plan.digest}, ref_id=plan.digest)
    passes = 0
    for i in range(plan.n_nulls):
        d = pipeline(make_null(child_seed(plan.base_seed, plan.name, "null", str(i))))
        passes += bool(d.passed)
    _, ub = clopper_pearson(passes, plan.n_nulls, plan.alpha, one_sided_upper=True)
    null_ok = ub <= plan.false_pass_target
    pts = []
    if plan.effect_sizes:
        if make_positive is None:
            raise ValueError("effect sizes given but no make_positive")
        for eff in plan.effect_sizes:
            det = 0
            for j in range(plan.n_per_effect):
                d = pipeline(make_positive(eff, child_seed(plan.base_seed, plan.name, "pos", repr(eff), str(j))))
                det += bool(d.passed)
            lo, hi = clopper_pearson(det, plan.n_per_effect, plan.alpha)
            pts.append(PowerPoint(eff, plan.n_per_effect, det, det / plan.n_per_effect, lo, hi))
    by_eff = {p.effect: p for p in pts}
    power_ok = all(by_eff[e].lo >= plan.min_power for e in plan.required)
    verdict = "NOT_CALIBRATED" if not null_ok else ("CALIBRATED" if power_ok else "UNDERPOWERED")
    notes = ""
    if passes == 0 and not null_ok:
        notes = ("zero null passes but n_nulls=%d cannot bound the rate at %.3f; need >= %d"
                 % (plan.n_nulls, plan.false_pass_target, plan.min_nulls_for_target()))
    res = BatteryResult(plan.digest, plan.n_nulls, passes, passes / plan.n_nulls, ub, null_ok,
                        tuple(pts), power_ok, verdict, notes)
    if ledger is not None:
        ledger.append("control_battery_result", asdict(res), ref_id=plan.digest)
    return res


# ── negative-control helpers ───────────────────────────────────────────

def block_permute(y, block_len: int, rng: np.random.Generator) -> np.ndarray:
    """Permute contiguous blocks of `y`: keeps within-block autocorrelation,
    destroys alignment with features. A model that still 'works' on
    block-permuted labels is leaking or overfitting."""
    a = np.asarray(y)
    if block_len < 1:
        raise ValueError("block_len must be >= 1")
    blocks = [a[i:i + block_len] for i in range(0, len(a), block_len)]
    order = rng.permutation(len(blocks))
    return np.concatenate([blocks[i] for i in order]) if blocks else a.copy()


def placebo_events(n_bars: int, n_events: int, rng: np.random.Generator, warmup: int = 0) -> np.ndarray:
    """Sorted random event positions with the same count as a real signal."""
    if n_events > n_bars - warmup:
        raise ValueError("more events than eligible bars")
    return np.sort(rng.choice(np.arange(warmup, n_bars), size=n_events, replace=False))


def circular_shift(x, shift: int) -> np.ndarray:
    return np.roll(np.asarray(x), int(shift))


__all__ = ["VERDICTS", "PipelineDecision", "BatteryPlan", "PowerPoint", "BatteryResult",
           "run_battery", "block_permute", "placebo_events", "circular_shift"]
