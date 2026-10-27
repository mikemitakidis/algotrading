"""research.integrity.evidence — reproducible calibration evidence for lane B.

Runs the integrity stack against reference pipelines on synthetic markets:

* a NAIVE pipeline (search 12 drop-reversal variants, pick the best, pass if
  its iid t-stat > 1.96) and an HONEST pipeline (same search, but HAC
  t-stats, Holm adjustment across the 12 variants and a deflated Sharpe
  gate) are each run through a null battery and a planted-positive battery;
* PBO, White's Reality Check and Hansen's SPA on null versus planted data;
* ledger verification and tamper detection on the run's own ledger;
* a pre-registration evaluated once, then refused the second time.

Usage:  python -m research.integrity.evidence --out <dir> [--n-nulls N] [--n-per-effect N]

Synthetic results describe the machinery's error rates, never market edges.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

from research.common.rng import stream
from research.common.synthetic import MarketSpec, PlantedEffect, SyntheticMarket, generate
from research.integrity import multiple_testing as MT
from research.integrity import stats as S
from research.integrity.controls import BatteryPlan, PipelineDecision, run_battery
from research.integrity.ledger import Hypothesis, Ledger, TrialRecord
from research.integrity.pbo import pbo
from research.integrity.authority import Authority
from research.integrity.prereg import (ATTESTATION_PURPOSE, PreRegistration, attest, attestation_payload,
                                       evaluate_once, register)
from research.integrity.reality_check import hansen_spa, white_reality_check

VARIANTS: Tuple[Tuple[float, int], ...] = tuple((k, h) for k in (1.5, 2.0, 2.5) for h in (3, 5, 10, 20))
COST = 0.05          # round-trip cost per trade, in normalised outcome units
VOL_WINDOW = 60


def variant_events(m: SyntheticMarket, k: float, h: int) -> Tuple[np.ndarray, np.ndarray]:
    """(session index of entry, normalised gross outcome) for one variant.

    Trigger at the close of t when r_t < -k * sigma_t, with sigma_t the
    rolling std of the last VOL_WINDOW log returns (known at t). Entry at the
    open of t+1, exit at the close of t+h; outcome in units of sigma_t*sqrt(h).
    Non-overlapping per symbol (event sampling).
    """
    sess, out = [], []
    for b in m.bars.values():
        o = np.log(b["open"].to_numpy())
        c = np.log(b["close"].to_numpy())
        n = len(c)
        if n < VOL_WINDOW + h + 2:
            continue
        r = np.diff(c, prepend=c[0])
        r[0] = 0.0
        cs, cq = np.cumsum(r), np.cumsum(r * r)
        sig = np.full(n, np.nan)
        t_idx = np.arange(VOL_WINDOW, n)
        s1 = cs[t_idx] - cs[t_idx - VOL_WINDOW]
        s2 = cq[t_idx] - cq[t_idx - VOL_WINDOW]
        var = (s2 - s1 * s1 / VOL_WINDOW) / (VOL_WINDOW - 1)
        sig[t_idx] = np.sqrt(np.where(var > 0, var, np.nan))
        cand = np.flatnonzero(np.isfinite(sig) & (r < -k * sig) & (np.arange(n) < n - h))
        last = -1
        for t in cand:                                   # non-overlapping per symbol
            if t <= last:
                continue
            sess.append(t + 1)
            out.append((c[t + h] - o[t + 1]) / (sig[t] * np.sqrt(h)))
            last = t + h - 1
    order = np.argsort(np.asarray(sess, dtype=np.int64), kind="mergesort")
    return np.asarray(sess, dtype=np.int64)[order], np.asarray(out, dtype=float)[order]


def family(m: SyntheticMarket):
    """Per-variant net outcomes and a (sessions x variants) family matrix."""
    n_sess = m.spec.n_sessions
    mats = np.zeros((n_sess, len(VARIANTS)))
    per = []
    for j, (k, h) in enumerate(VARIANTS):
        s, g = variant_events(m, k, h)
        net = g - COST
        per.append((k, h, s, net))
        np.add.at(mats[:, j], np.minimum(s, n_sess - 1), net)
    return per, mats


def naive_pipeline(m: SyntheticMarket) -> PipelineDecision:
    per, _ = family(m)
    best_t, best = -np.inf, None
    for k, h, _, net in per:
        if len(net) < 10:
            continue
        t = net.mean() / (net.std(ddof=1) / np.sqrt(len(net)))
        if t > best_t:
            best_t, best = t, (k, h)
    return PipelineDecision(passed=bool(best_t > 1.96), statistic=float(best_t), details={"best": best})


def honest_pipeline(m: SyntheticMarket) -> PipelineDecision:
    per, _ = family(m)
    ts, ps, nets = [], [], []
    for k, h, _, net in per:
        if len(net) < 10:
            ts.append(-np.inf); ps.append(1.0); nets.append(net)
            continue
        _, t, p = S.mean_test(net, lags=h)
        ts.append(t if np.isfinite(t) else -np.inf)
        ps.append(p if np.isfinite(p) else 1.0)
        nets.append(net)
    adj = MT.adjust(ps, "holm")
    j = int(np.argmax(ts))
    if len(nets[j]) < 10:
        return PipelineDecision(False, float("nan"), {"reason": "too few events"})
    d = S.deflated_sharpe_ratio(nets[j], n_trials=len(VARIANTS))
    passed = bool(adj[j] <= 0.05 and d.dsr >= 0.95)
    return PipelineDecision(passed, float(ts[j]), {"best": VARIANTS[j], "holm_p": float(adj[j]), "dsr": d.dsr})


BASE = dict(n_symbols=10, n_sessions=750)


def make_null(seed: int) -> SyntheticMarket:
    return generate(MarketSpec(seed=seed, **BASE))


def make_positive(effect: float, seed: int) -> SyntheticMarket:
    return generate(MarketSpec(seed=seed, effects=(PlantedEffect("rev", "drop_reversal", effect, horizon=5, k=2.0),),
                               **BASE))


def run(out_dir: Path, n_nulls: int = 100, n_per_effect: int = 30,
        effects: Tuple[float, ...] = (0.05, 0.1, 0.2, 0.3, 0.4), n_snoop: int = 20,
        snoop_effect: float = 0.4) -> Dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    ledger_path = out_dir / "lane_b_ledger.sqlite"
    if ledger_path.exists():
        ledger_path.unlink()
    led = Ledger(ledger_path)
    hid = led.register_hypothesis(Hypothesis(
        family_id="drop_reversal_v0", statement="Large one-bar drops partially revert over the next bars",
        mechanism="liquidity provision to forced sellers", falsifier="no positive net mean after costs out of sample",
        expected_effect="positive net mean outcome after drops", author="lane-B-evidence"))
    results = {}
    for name, pipe in (("naive", naive_pipeline), ("honest", honest_pipeline)):
        plan = BatteryPlan(name="%s_drop_reversal" % name, pipeline_id="research.integrity.evidence.%s_pipeline" % name,
                           n_nulls=n_nulls, effect_sizes=effects, n_per_effect=n_per_effect, base_seed=20261026)
        res = run_battery(plan, pipe, make_null, make_positive, ledger=led)
        results[name] = asdict(res)
    # PBO / SPA / RC across many null and planted markets: rejection rates.
    snoop = {}
    for label, mk_fn in (("null", lambda sd: make_null(sd)), ("planted_%.1f" % snoop_effect, lambda sd: make_positive(snoop_effect, sd))):
        rows = []
        for i in range(n_snoop):
            mk = mk_fn(910000 + i)
            _, fm = family(mk)
            rng = stream(7, "lane_b", "snoop", label, str(i))
            rows.append((pbo(fm, n_blocks=10).pbo, hansen_spa(fm, rng, n_boot=500).p_value,
                         white_reality_check(fm, rng, n_boot=500).p_value, S.effective_trials_eigen(fm),
                         S.effective_trials_cluster(fm)))
            for j, (k, h) in enumerate(VARIANTS):
                led.record_trial(TrialRecord(family_id="drop_reversal_v0", kind="research",
                                             spec_digest="k=%s,h=%s" % (k, h), data_digest=mk.spec_digest,
                                             code_digest="research.integrity.evidence", seed=mk.spec.seed,
                                             hypothesis_id=hid, metrics={"sr": S.sharpe(fm[:, j]) if fm[:, j].std() > 0 else 0.0,
                                                                         "dataset": label}))
        a_ = np.asarray(rows)
        spa_rej = int(np.sum(a_[:, 1] <= 0.05))
        rc_rej = int(np.sum(a_[:, 2] <= 0.05))
        snoop[label] = {"n": n_snoop, "mean_pbo": float(a_[:, 0].mean()),
                        "spa_rejections": spa_rej, "spa_rejection_ci": S.clopper_pearson(spa_rej, n_snoop),
                        "rc_rejections": rc_rej, "rc_rejection_ci": S.clopper_pearson(rc_rej, n_snoop),
                        "mean_eff_trials_eigen": float(a_[:, 3].mean()), "mean_eff_trials_cluster": float(a_[:, 4].mean())}
    # Pre-registration: one-shot.
    p = PreRegistration(family_id="drop_reversal_v0", hypothesis_id=hid, spec_digest="k=2.0,h=5", metric="dsr",
                        comparator=">=", threshold=0.95, min_effective_n=30,
                        forward_window_start_utc="2027-01-01T00:00:00+00:00",
                        analysis_plan="DSR of net outcomes with n_trials=12 on forward data only")
    d = register(led, p)
    # 1) A caller-declared timestamp is recorded as untrusted and cannot make an evaluation pass.
    p_unt = PreRegistration(**{**p.__dict__, "spec_digest": "k=2.0,h=5/untrusted-demo"})
    d_unt = register(led, p_unt)
    attest(led, d_unt, server_timestamp_utc="2026-10-26T23:59:00+00:00", source="declared by caller",
           evidence_url="(none)")
    untrusted = evaluate_once(led, d_unt, value=0.99, effective_n=40,
                              data_first_ts_utc="2027-01-02T21:00:00+00:00", data_digest="example")
    # 2) An authority-signed attestation (local demo authority; in operation a separately held key).
    demo = Authority("local-demo-authority", stream(7, "lane_b", "demo-key").bytes(32))
    payload = attestation_payload(d, "2026-10-26T23:59:00+00:00", "example: github comment created_at",
                                  "(illustrative; a real run uses the actual comment URL)")
    attest(led, d, **{k: payload[k] for k in ("server_timestamp_utc", "source", "evidence_url")},
           signature=demo.sign(ATTESTATION_PURPOSE, payload), verifier=demo.verifier())
    first = evaluate_once(led, d, value=0.5, effective_n=40, data_first_ts_utc="2027-01-02T21:00:00+00:00",
                          data_digest="example")
    try:
        evaluate_once(led, d, value=0.99, effective_n=40, data_first_ts_utc="2027-01-02T21:00:00+00:00",
                      data_digest="example")
        second = "ALLOWED (defect)"
    except Exception as exc:                     # expected: one-shot refusal
        second = "refused: %s" % exc
    ver = led.verify()
    jsonl = out_dir / "lane_b_ledger.jsonl"
    led.export_jsonl(jsonl)
    led.close()
    # Tamper demo on a copy: edit one payload bypassing the triggers.
    tam = out_dir / "lane_b_ledger_tampered.sqlite"
    tam.write_bytes(ledger_path.read_bytes())
    con = sqlite3.connect(tam)
    con.execute("DROP TRIGGER entries_no_update")
    cur = con.execute("UPDATE entries SET payload = replace(payload, '\"author\":\"lane-B-evidence\"', "
                      "'\"author\":\"someone-else\"') WHERE seq = 1")
    if cur.rowcount != 1:
        raise RuntimeError("tamper demo did not modify the target entry")
    con.commit()
    con.close()
    tver = Ledger(tam).verify()
    tam.unlink()
    summary = {"batteries": results, "snooping": snoop, "family_variants": len(VARIANTS),
               "prereg": {"digest": d, "first_evaluation": asdict(first), "second_evaluation": second,
                          "untrusted_attestation_evaluation": asdict(untrusted)},
               "ledger": {"entries": ver.n_entries, "verified": ver.ok, "jsonl": jsonl.name,
                          "jsonl_verified": Ledger.verify_jsonl(jsonl).ok,
                          "tampered_copy_verified": tver.ok, "tamper_detected_at_seq": tver.first_bad_seq,
                          "tamper_reason": tver.reason},
               "config": {"n_nulls": n_nulls, "n_per_effect": n_per_effect, "effects": list(effects),
                          "market": BASE, "cost": COST, "n_snoop": n_snoop, "snoop_effect": snoop_effect},
               "runtime_seconds": round(time.time() - t0, 1)}
    (out_dir / "lane_b_evidence.json").write_text(json.dumps(summary, indent=2, sort_keys=True, default=str))
    (out_dir / "lane_b_evidence.md").write_text(render_markdown(summary))
    return summary


def render_markdown(s: Dict) -> str:
    L: List[str] = ["# Lane B evidence: does the integrity stack tell noise from a planted effect?", "",
                    "Generated by `python -m research.integrity.evidence`. Synthetic markets (%d symbols x %d "
                    "sessions each); %d drop-reversal variants searched per dataset; cost %.2f per trade. "
                    "These numbers describe the **pipelines' error rates**, not any market." % (
                        s["config"]["market"]["n_symbols"], s["config"]["market"]["n_sessions"],
                        s["family_variants"], s["config"]["cost"]), ""]
    L += ["## 1. Null and planted-positive batteries", "",
          "| pipeline | nulls | null passes | false-pass rate | one-sided 95 % upper bound | verdict |",
          "|---|---|---|---|---|---|"]
    for name in ("naive", "honest"):
        b = s["batteries"][name]
        L.append("| %s | %d | %d | %.1f %% | %.1f %% | %s |" % (name, b["n_nulls"], b["null_passes"],
                 100 * b["false_pass_rate"], 100 * b["false_pass_upper"], b["verdict"]))
    L += ["", "Detection rate (power) on markets with a planted drop-reversal effect of the given strength "
          "(sigma per bar for 5 bars after a > 2 sigma drop):", "",
          "| strength | naive detection | honest detection (95 % CI) |", "|---|---|---|"]
    for pn, ph in zip(s["batteries"]["naive"]["power"], s["batteries"]["honest"]["power"]):
        L.append("| %.2f | %d/%d | %d/%d (%.0f–%.0f %%) |" % (ph["effect"], pn["detections"], pn["n"],
                 ph["detections"], ph["n"], 100 * ph["lo"], 100 * ph["hi"]))
    L += ["", "Verdict rule: CALIBRATED needs the one-sided 95 % upper bound on the null false-pass rate at or "
          "below 5 % **and** the exact lower bound of the detection rate at every required effect (by default "
          "the largest) at or above 80 %. Each battery plan is ledgered before it runs and can be used only once." ]
    L += ["", "## 2. Data-snooping tests across %d null and %d planted (strength %.1f) markets" % (
              s["config"]["n_snoop"], s["config"]["n_snoop"], s["config"]["snoop_effect"]), "",
          "| datasets | Hansen SPA rejections at 5 % (95 % CI) | White RC rejections at 5 % (95 % CI) | mean PBO | mean effective trials (eigen / cluster) |",
          "|---|---|---|---|---|"]
    for k, v in s["snooping"].items():
        L.append("| %s | %d/%d (%.0f–%.0f %%) | %d/%d (%.0f–%.0f %%) | %.2f | %.1f / %.1f |" % (
            k, v["spa_rejections"], v["n"], 100 * v["spa_rejection_ci"][0], 100 * v["spa_rejection_ci"][1],
            v["rc_rejections"], v["n"], 100 * v["rc_rejection_ci"][0], 100 * v["rc_rejection_ci"][1],
            v["mean_pbo"], v["mean_eff_trials_eigen"], v["mean_eff_trials_cluster"]))
    nul = s["snooping"]["null"]
    pl = s["snooping"]["planted_%.1f" % s["config"]["snoop_effect"]]
    L += ["", "Mean PBO is %.2f on null markets (about 0.5 means picking the in-sample best variant is pure "
          "noise) and %.2f on planted markets. PBO diagnoses the *selection among variants*; when every variant "
          "shares one effect their ranking is still partly noise, so PBO alone is not an edge test. The "
          "12 variants behave like about %.1f independent trials (eigenvalue estimate), which is what a "
          "deflated Sharpe should be charged for if trials are correlated." % (
              nul["mean_pbo"], pl["mean_pbo"], nul["mean_eff_trials_eigen"])]
    lg = s["ledger"]
    L += ["", "## 3. Ledger integrity", "",
          "- Entries: %d; chain verified: **%s**; JSONL mirror verified: **%s**." % (lg["entries"], lg["verified"],
                                                                                   lg["jsonl_verified"]),
          "- Tampered copy (one payload edited after dropping the UPDATE trigger): verified = **%s**, detected at "
          "entry %s (%s)." % (lg["tampered_copy_verified"], lg["tamper_detected_at_seq"], lg["tamper_reason"]),
          "", "## 4. Pre-registration needs a trusted timestamp and is one-shot", "",
          "- Caller-declared (untrusted) timestamp, metric value far above threshold: passed = %s (%s)." % (
              s["prereg"]["untrusted_attestation_evaluation"]["passed"],
              s["prereg"]["untrusted_attestation_evaluation"]["reason"]),
          "- Authority-signed timestamp, first evaluation: %s." % s["prereg"]["first_evaluation"]["reason"],
          "- Second evaluation: %s." % s["prereg"]["second_evaluation"], "",
          "_Runtime %.1f s. Reproduce with the command above; all randomness is seeded._" % s["runtime_seconds"]]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--n-nulls", type=int, default=100)
    ap.add_argument("--n-per-effect", type=int, default=30)
    a = ap.parse_args(argv)
    s = run(Path(a.out), a.n_nulls, a.n_per_effect)
    print(json.dumps({k: s["batteries"][k]["verdict"] for k in s["batteries"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
