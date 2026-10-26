#!/usr/bin/env python3
"""Reproducible numbers behind docs/research/M21_autonomous_research_engine_proposal.md.

Offline, deterministic (fixed seed), numpy/scipy only; reads and writes no
project data. Run:  python docs/research/m21_research_power_analysis.py

A. Sample size needed to detect a per-trade edge (R-multiples) from trades.
B. Champion-vs-challenger sample size per arm.
C. Selection bias: the Sharpe ratio a pure-noise search "finds" after N
   trials (analytic expected maximum, after Bailey & Lopez de Prado), checked
   by Monte Carlo.
D. Calendar time those sample sizes imply at the paper loop's throughput.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.stats import norm

Z_A = norm.ppf(0.95)          # one-sided alpha = 5 %
Z_B = norm.ppf(0.80)          # power = 80 %
EULER_GAMMA = 0.5772156649


def bracket_moments(p_win: float, reward_r: float, cost_r: float = 0.0):
    """Two-outcome bracket: +reward_r with prob p, -1R otherwise, minus cost."""
    mu = p_win * reward_r - (1 - p_win) - cost_r
    sd = (reward_r + 1.0) * math.sqrt(p_win * (1 - p_win))
    return mu, sd


def n_detect(mu: float, sd: float) -> float:
    return ((Z_A + Z_B) * sd / mu) ** 2 if mu > 0 else float("inf")


def expected_max_sharpe(n_trials: int, years: float) -> float:
    """E[max annualised SR] of n independent zero-edge strategies, each with
    an SR estimate whose std is ~ 1/sqrt(years) (daily returns, normal)."""
    if n_trials < 2:
        return 0.0
    sd = 1.0 / math.sqrt(years)
    return sd * ((1 - EULER_GAMMA) * norm.ppf(1 - 1.0 / n_trials)
                 + EULER_GAMMA * norm.ppf(1 - 1.0 / (n_trials * math.e)))


def mc_max_sharpe(n_trials: int, years: float, sims: int = 200, seed: int = 7) -> float:
    rng = np.random.default_rng(seed)
    days = int(round(252 * years))
    out = []
    for _ in range(sims):
        r = rng.standard_normal((n_trials, days))
        sr = r.mean(axis=1) / r.std(axis=1, ddof=1) * math.sqrt(252)
        out.append(sr.max())
    return float(np.mean(out))


def main() -> None:
    print("## A. Trades needed to detect a per-trade edge (one-sided 5 %, power 80 %)\n")
    print("Bracket: target = 1.5R, stop = 1R (break-even win rate 40.0 %).\n")
    print("| true win rate | cost per trade | edge per trade | trades needed |")
    print("|---|---|---|---|")
    for p in (0.42, 0.45, 0.50):
        for cost in (0.0, 0.05):
            mu, sd = bracket_moments(p, 1.5, cost)
            n = n_detect(mu, sd)
            mu = 0.0 if abs(mu) < 1e-12 else mu
            print("| %.0f %% | %.2fR | %+.3fR | %s |" % (
                100 * p, cost, mu, "{:,.0f}".format(n) if math.isfinite(n) else "never"))

    print("\n## B. Champion vs challenger: trades per arm to detect a difference\n")
    print("| difference in edge | trades per arm (independent) |")
    print("|---|---|")
    _, sd = bracket_moments(0.45, 1.5)
    for d in (0.05, 0.10, 0.20):
        n = 2 * ((Z_A + Z_B) * sd / d) ** 2
        print("| %.2fR | %s |" % (d, "{:,.0f}".format(n)))

    print("\n## C. Sharpe ratio found by searching pure noise (no edge exists)\n")
    print("| trials searched | 3 years of daily data: expected best annual Sharpe | Monte Carlo check |")
    print("|---|---|---|")
    for n in (10, 100, 1000, 10000):
        mc = mc_max_sharpe(n, 3.0, sims=60 if n >= 1000 else 200) if n <= 10000 else float("nan")
        print("| %s | %.2f | %.2f |" % ("{:,}".format(n), expected_max_sharpe(n, 3.0), mc))

    print("\n## D. Calendar time at paper-loop throughput\n")
    mu, sd = bracket_moments(0.45, 1.5)
    n1 = n_detect(mu, sd)
    per_arm = 2 * ((Z_A + Z_B) * sd / 0.05) ** 2
    total = 2 * per_arm                           # both arms share one paper-trade stream
    print("| closed trades per trading day | detect a 45 %% win-rate edge (%s trades) "
          "| A/B a 0.05R improvement (%s per arm, %s total) |"
          % ("{:,.0f}".format(n1), "{:,.0f}".format(per_arm), "{:,.0f}".format(total)))
    print("|---|---|---|")
    for tpd in (0.5, 1.0, 3.0):
        print("| %.1f | %.1f years | %.1f years |" % (tpd, n1 / tpd / 252, total / tpd / 252))


if __name__ == "__main__":
    main()
