"""M19.A config — SignalScoringConfig with validated, approved defaults.

Default profile is STRICT. The RESEARCH profile is defined (selectable) but
not the default. Config is a frozen dataclass of nested dicts (the approved
default blocks). Validation rejects malformed configs; config_hash is
deterministic via provenance.canonical_json.

No scoring logic here — defaults + validation + (de)serialisation only.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict

from bot.signal_scoring import provenance
from bot.signal_scoring.schema import ScoringProfile

CONFIG_SCHEMA_VERSION = "m19_scoring_config_v1"
DEFAULT_PROFILE = ScoringProfile.STRICT

# Tolerance for float-sum validation (weights / anchor split).
_SUM_TOL = 1e-6


# ── approved default blocks (factories so each config gets its own copy) ──
def _default_weights() -> Dict[str, float]:
    return {
        "ml":                     0.30,
        "scanner":                0.16,
        "technical_confluence":   0.12,
        "trend":                  0.08,
        "momentum":               0.07,
        "volume_liquidity":       0.07,
        "volatility":             0.06,
        "market_regime":          0.06,
        "risk_adjusted":          0.05,
        "data_quality":           0.02,
        "calibration_uncertainty": 0.01,
    }


def _default_thresholds() -> Dict[str, float]:
    return {
        "reject_below":        45,
        "watch_min":           45,
        "eligible_min":        65,
        "high_conviction_min": 82,
        "manual_review_low":   58,
        "manual_review_high":  65,
    }


def _default_hard_gates() -> Dict[str, Any]:
    return {
        "require_no_broker_execution":     True,
        "require_no_live_state_mutation":  True,
        "require_valid_timestamp":         True,
        "require_non_stale_data":          True,
        "require_min_available_timeframes": 4,
        "require_no_adjusted_price_pit_risk": True,
        "require_model_readiness_passed":  True,
        "require_schema_match":            True,
        "require_risk_preview_available":  True,
        "require_min_liquidity":           True,
        "block_short_side_by_default":     True,
    }


def _default_multipliers() -> Dict[str, float]:
    return {
        "regime_aligned":            1.00,
        "regime_unknown":            0.95,
        "regime_countertrend":       0.85,
        "volatility_normal":         1.00,
        "volatility_elevated":       0.92,
        "liquidity_thin_but_allowed": 0.90,
        "fourh_utc_fixed_reliance":  0.95,
        "multiplier_floor":          0.70,
    }


def _default_penalties() -> Dict[str, float]:
    return {
        "uncalibrated_ml_probability":  15,
        "each_feature_extrapolation":   3,
        "extrapolation_cap":            20,
        "production_thinness_warning":  8,
        "production_thinness_blocked":  100,
        "missing_noncritical_timeframe": 5,
        "weak_scanner_confluence":      10,
        "poor_reward_risk":             15,
        "max_total_penalty_points":     30,
    }


def _default_ml() -> Dict[str, Any]:
    return {
        "prefer_calibrated_probability":               True,
        "allow_raw_probability_fallback_research_only": True,
        "min_calibrated_probability_for_eligible":     0.55,
        "high_conviction_probability":                 0.68,
        "manual_review_probability_above":             0.95,
        "max_feature_extrapolations_for_eligible":     3,
        "block_adjusted_without_allow_flag":           True,
    }


def _default_scanner() -> Dict[str, Any]:
    return {
        "min_valid_timeframes":     3,
        "min_available_timeframes": 4,
        "allow_partial_data":       False,
    }


def _default_technical() -> Dict[str, float]:
    return {
        "trend_weight":              0.30,
        "momentum_weight":           0.25,
        "volume_weight":             0.20,
        "volatility_weight":         0.15,
        "support_resistance_weight": 0.10,
    }


def _default_risk() -> Dict[str, Any]:
    return {
        "max_risk_per_trade_pct":          1.0,
        "min_reward_risk_ratio":           1.5,
        "ideal_reward_risk_ratio":         2.0,
        "max_stop_distance_atr":           2.5,
        "min_stop_distance_atr":           0.5,
        "max_position_preview_pct_equity": 10.0,
        "block_if_risk_authority_blocks":  True,
    }


def _default_liquidity() -> Dict[str, Any]:
    return {
        "min_avg_dollar_volume_20d":   10_000_000,
        "ideal_avg_dollar_volume_20d": 50_000_000,
        "min_price":                   2.00,
        "max_spread_pct":              0.20,
        "block_below_min_liquidity":   True,
    }


def _default_volatility() -> Dict[str, Any]:
    return {
        "atr_pct_min":             0.005,
        "atr_pct_ideal_min":       0.01,
        "atr_pct_ideal_max":       0.04,
        "atr_pct_max":             0.06,
        "block_above_atr_pct_max": True,
    }


def _default_regime() -> Dict[str, Any]:
    return {
        "source":                    "supplied_input",
        "benchmark_symbol":          "SPY",
        "regime_sma_window":         200,
        "long_above_sma_multiplier": 1.00,
        "long_below_sma_multiplier": 0.85,
        "unknown_regime_multiplier": 0.95,
    }


def _default_data_quality() -> Dict[str, Any]:
    return {
        "block_adjusted_price_pit_risk": True,
        "fourh_utc_fixed_warning":       True,
        "stale_data_max_age_minutes":    30,
        "max_missing_features":          0,
    }


def _default_output() -> Dict[str, Any]:
    return {
        "default_mode":       "in_memory",
        "allow_jsonl":        False,
        "allow_sqlite_write": False,
        "commit_outputs":     False,
    }


@dataclass(frozen=True)
class SignalScoringConfig:
    config_schema_version: str = CONFIG_SCHEMA_VERSION
    profile: ScoringProfile = DEFAULT_PROFILE

    ml_anchor_weight: float = 0.55
    support_weight: float = 0.45

    weights:       Dict[str, float] = field(default_factory=_default_weights)
    thresholds:    Dict[str, float] = field(default_factory=_default_thresholds)
    hard_gates:    Dict[str, Any]   = field(default_factory=_default_hard_gates)
    multipliers:   Dict[str, float] = field(default_factory=_default_multipliers)
    penalties:     Dict[str, float] = field(default_factory=_default_penalties)
    ml:            Dict[str, Any]   = field(default_factory=_default_ml)
    scanner:       Dict[str, Any]   = field(default_factory=_default_scanner)
    technical:     Dict[str, float] = field(default_factory=_default_technical)
    risk:          Dict[str, Any]   = field(default_factory=_default_risk)
    liquidity:     Dict[str, Any]   = field(default_factory=_default_liquidity)
    volatility:    Dict[str, Any]   = field(default_factory=_default_volatility)
    regime:        Dict[str, Any]   = field(default_factory=_default_regime)
    data_quality:  Dict[str, Any]   = field(default_factory=_default_data_quality)
    output:        Dict[str, Any]   = field(default_factory=_default_output)

    def __post_init__(self):
        # normalise profile
        if not isinstance(self.profile, ScoringProfile):
            try:
                object.__setattr__(self, "profile", ScoringProfile(self.profile))
            except ValueError:
                raise ValueError(f"invalid profile: {self.profile!r}")
        self.validate()

    # ── validation ──
    def validate(self) -> None:
        # anchor split must sum to 1
        if abs((self.ml_anchor_weight + self.support_weight) - 1.0) > _SUM_TOL:
            raise ValueError(
                "ml_anchor_weight + support_weight must equal 1.0 "
                f"(got {self.ml_anchor_weight} + {self.support_weight})")
        for nm, v in (("ml_anchor_weight", self.ml_anchor_weight),
                      ("support_weight", self.support_weight)):
            if not (0.0 <= v <= 1.0):
                raise ValueError(f"{nm} out of range [0,1]: {v}")

        # component weights in range and summing to 1
        for k, v in self.weights.items():
            if not (0.0 <= v <= 1.0):
                raise ValueError(f"weight {k} out of range [0,1]: {v}")
        wsum = sum(self.weights.values())
        if abs(wsum - 1.0) > _SUM_TOL:
            raise ValueError(f"component weights must sum to 1.0 (got {wsum})")

        # threshold ordering
        t = self.thresholds
        if not (t["reject_below"] == t["watch_min"]):
            raise ValueError("reject_below must equal watch_min (contiguous)")
        if not (t["watch_min"] <= t["manual_review_low"]
                <= t["manual_review_high"] <= t["eligible_min"]
                <= t["high_conviction_min"]):
            raise ValueError(
                "thresholds out of order: require watch_min <= "
                "manual_review_low <= manual_review_high <= eligible_min "
                "<= high_conviction_min")
        for k, v in t.items():
            if not (0 <= v <= 100):
                raise ValueError(f"threshold {k} out of range [0,100]: {v}")

        # penalties non-negative
        for k, v in self.penalties.items():
            if v < 0:
                raise ValueError(f"penalty {k} must be non-negative: {v}")

        # multiplier floor in [0,1]; multiplier values in (0,2]
        floor = self.multipliers.get("multiplier_floor")
        if floor is None or not (0.0 <= floor <= 1.0):
            raise ValueError(f"multiplier_floor out of range [0,1]: {floor}")
        for k, v in self.multipliers.items():
            if k == "multiplier_floor":
                continue
            if not (0.0 < v <= 2.0):
                raise ValueError(f"multiplier {k} out of range (0,2]: {v}")

        # output safety defaults
        if self.output.get("allow_sqlite_write", False) is True:
            raise ValueError(
                "allow_sqlite_write must be False by default in M19")

        # ml probability fields in [0,1]
        for k in ("min_calibrated_probability_for_eligible",
                  "high_conviction_probability",
                  "manual_review_probability_above"):
            v = self.ml.get(k)
            if v is None or not (0.0 <= v <= 1.0):
                raise ValueError(f"ml.{k} out of range [0,1]: {v}")

    # ── (de)serialisation ──
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["profile"] = self.profile.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SignalScoringConfig":
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in d.items() if k in known}
        return cls(**kwargs)

    def config_hash(self) -> str:
        return provenance.config_hash(self.to_dict())


def default_config(profile: ScoringProfile = DEFAULT_PROFILE
                   ) -> SignalScoringConfig:
    """Return a validated default config for the given profile (default
    STRICT). Profile changes behaviour in later phases (gates), not the
    structural defaults here."""
    if not isinstance(profile, ScoringProfile):
        profile = ScoringProfile(profile)
    return SignalScoringConfig(profile=profile)
