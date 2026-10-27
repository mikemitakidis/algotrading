"""research.agents.mock — the deterministic, offline research-team workflow (canonical B22 "mock first").

No model, network, API key or scheduler is involved. Each role is a small
deterministic function acting through the permission gate. The run shows the
whole governed path and the guards firing:

1. Director assigns a question.
2. Scout records sources from an OFFLINE fixture library. A network fetch
   is attempted and denied.
3. Hypothesist proposes a falsifiable hypothesis. Two more proposals are
   refused: a reworded duplicate, and one citing a source published after
   the test window (look-ahead through the literature).
4. Engineer writes a genome SPECIFICATION (data). "Generated code" text is
   only linted; executing it is denied.
5. Skeptic reviews: validator, falsifiability, lint findings.
6. Auditor (deterministic code):
   - registers the hypothesis and a pre-registration in the lane B
     ledger. The attestation is untrusted, so the pre-registration can
     never pass;
   - runs the genome on paired synthetic markets with the lane D engine,
     ledgering every trial;
   - produces a lane B report card;
   - is denied when it tries to open the holdout.
7. Librarian records lineage and, unless the evidence passes, buries the
   hypothesis in the graveyard with an explicit re-test trigger.
8. Reporter writes the summary.

Every decision is in the hash-chained ledger; ``MockRun.digest`` is stable.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List

import numpy as np

from research.agents.knowledge import KnowledgeError, KnowledgeLibrary, Prediction, Proposal, Source
from research.agents.roles import Gate, PermissionDenied
from research.boundary import capability_findings, may_execute_untrusted, scan_source
from research.common.hashing import digest
from research.common.rng import stream
from research.common.synthetic import MarketSpec, PlantedEffect, generate
from research.genome import engine as E
from research.genome import schema as G
from research.integrity import prereg as PR
from research.integrity.ledger import Hypothesis, Ledger, TrialRecord
from research.integrity.provenance import code_identity, outcome_digest
from research.integrity.report_card import GauntletConfig, evaluate as gauntlet

CODE_PATHS = ("research/agents", "research/genome", "research/integrity", "research/common", "bot/backtesting")
CODE_ID = code_identity(CODE_PATHS)

FIXTURE_NOTE = "offline fixture; bibliographic details recorded from the published article, not re-verified online"
FIXTURE_SOURCES = (
    Source("Does the Stock Market Overreact?", "W. F. M. De Bondt; R. Thaler", "Journal of Finance 40(3), 793-805",
           "1985-07-01", "doi:10.1111/j.1540-6261.1985.tb05004.x",
           "Extreme past losers subsequently outperform extreme past winners (long-horizon overreaction).",
           "2026-10-27", "fixture", FIXTURE_NOTE),
    Source("When Are Contrarian Profits Due to Stock Market Overreaction?", "A. W. Lo; A. C. MacKinlay",
           "Review of Financial Studies 3(2), 175-205", "1990-04-01", "doi:10.1093/rfs/3.2.175",
           "Short-horizon contrarian profits are partly explained by cross-autocorrelation, not only overreaction.",
           "2026-10-27", "fixture", FIXTURE_NOTE),
    Source("Returns to Buying Winners and Selling Losers", "N. Jegadeesh; S. Titman",
           "Journal of Finance 48(1), 65-91", "1993-03-01", "doi:10.1111/j.1540-6261.1993.tb04702.x",
           "Intermediate-horizon (3-12 month) momentum earns abnormal returns.", "2026-10-27", "fixture", FIXTURE_NOTE),
    Source("The Deflated Sharpe Ratio", "D. H. Bailey; M. Lopez de Prado", "Journal of Portfolio Management 40(5)",
           "2014-09-01", "doi:10.3905/jpm.2014.40.5.094",
           "Selection among many trials inflates Sharpe ratios; deflate for the number of trials.", "2026-10-27",
           "fixture", FIXTURE_NOTE),
    Source("FICTIONAL TEST FIXTURE: a source from the future", "nobody", "not a real publication", "2030-01-01",
           "urn:fixture:future", "Exists only to prove that look-ahead citations are refused.", "2030-01-02",
           "fixture", "deliberately fictional; never cite"),
)
GENERATED_CODE = '''import os
def signal(df):
    os.system("curl http://example.invalid")          # what a careless agent might write
    return df["close"] < df["close"].rolling(20).mean()
'''


def _clock():
    n = [0]

    def f():
        n[0] += 1
        return "2026-10-27T00:%02d:%02d+00:00" % (n[0] // 60, n[0] % 60)
    return f


@dataclass
class MockRun:
    decisions: List[Dict]
    refusals: List[str]
    kb: Dict
    evaluation: Dict
    report_card: Dict
    prereg_outcome: Dict
    report: str
    ledger_entries: int
    ledger_ok: bool
    extra: Dict = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return digest({"decisions": self.decisions, "kb": self.kb, "evaluation": self.evaluation,
                       "report_card": self.report_card, "prereg": self.prereg_outcome, "report": self.report})


def run(seed: int = 3, n_symbols: int = 12, n_sessions: int = 600, n_markets: int = 3) -> MockRun:
    L = Ledger(clock=_clock())
    gate = Gate(L)
    kb = KnowledgeLibrary(L, gate)
    refusals: List[str] = []

    def attempt(fn, *a, **k):
        try:
            return fn(*a, **k)
        except (PermissionDenied, KnowledgeError) as exc:
            refusals.append("%s: %s" % (type(exc).__name__, exc))
            return None

    # 1. Director
    gate.require("director", "task.assign", "short-horizon oversold reversal after costs")
    # 2. Scout
    sids = [kb.add_source("scout", s) for s in FIXTURE_SOURCES]
    attempt(gate.require, "scout", "network.fetch", "https://example.invalid/papers")
    # 3. Hypothesist
    window = "2018-01-02"
    h1 = Proposal(statement="Short horizon oversold stocks below the lower Bollinger band with RSI under 30 "
                            "revert upward over the following five sessions enough to cover costs",
                  mechanism="liquidity-driven overreaction to one-sided selling (De Bondt-Thaler; Lo-MacKinlay)",
                  prediction=Prediction("paired_return_diff_vs_null", ">", 0.0),
                  kill_criteria="paired difference not positive in a majority of markets, or report card not PASS",
                  source_ids=(sids[0], sids[1]), template="mean_reversion", proposed_by="hypothesist",
                  test_window_start=window)
    hid = kb.propose(h1)
    dup = Proposal(**{**asdict(h1), "prediction": h1.prediction,
                      "statement": "Short-horizon oversold stocks below the lower Bollinger band, with RSI under 30, "
                                   "revert upward over the following five sessions enough to cover the costs"})
    attempt(kb.propose, dup)
    future = Proposal(**{**asdict(h1), "prediction": h1.prediction, "source_ids": (sids[4],),
                         "statement": "A completely different claim about intraday seasonality in small caps"})
    attempt(kb.propose, future)
    # 4. Engineer: a specification (data), linted text, denied execution
    spec = G.seed("mean_reversion").to_dict()
    gate.require("engineer", "genome.propose", spec["genome_id"])
    gate.require("engineer", "genome.validate", spec["genome_id"])
    genome = G.from_dict(spec)
    gate.require("engineer", "code.lint", "generated signal function")
    lint = {"import_violations": [str(v) for v in scan_source(GENERATED_CODE, "<generated>")],
            "capability_findings": [asdict(f) for f in capability_findings(GENERATED_CODE, "<generated>")],
            "may_execute": may_execute_untrusted(GENERATED_CODE)}
    attempt(gate.require, "engineer", "code.execute", "generated signal function")
    # 5. Skeptic
    notes = ["genome validates fail-closed: %s" % genome.genome_id,
             "prediction is falsifiable: %s %s %s" % (h1.prediction.metric, h1.prediction.comparator,
                                                     h1.prediction.threshold),
             "generated code text rejected: %d capability finding(s); it will never be executed"
             % len(lint["capability_findings"])]
    kb.review("skeptic", hid, "proceed_with_spec_only", notes)
    # 6. Auditor (deterministic)
    lh = L.register_hypothesis(Hypothesis(family_id="agents:mock", statement=h1.statement, mechanism=h1.mechanism,
                                          falsifier=h1.kill_criteria, expected_effect="paired return diff > 0",
                                          author="auditor", source_refs=h1.source_ids))
    gate.require("auditor", "prereg.register", lh)
    pre = PR.PreRegistration(family_id="agents:mock", hypothesis_id=lh, spec_digest=genome.genome_id,
                             metric="paired_return_diff_vs_null", comparator=">", threshold=0.0, min_effective_n=3,
                             forward_window_start_utc="2026-11-01T00:00:00+00:00",
                             analysis_plan="mean paired total-return difference across synthetic markets")
    pdig = PR.register(L, pre)
    PR.attest(L, pdig, server_timestamp_utc="2026-10-27T00:00:00+00:00", source="mock (no authority)",
              evidence_url="urn:mock")
    gate.require("auditor", "eval.synthetic", genome.genome_id)
    diffs, trial_ids, r_all = [], [], []
    rng = stream(seed, "agents", "markets")
    for i in range(n_markets):
        sd = int(rng.integers(1, 2 ** 31))
        eff = (PlantedEffect("rsi", "rsi_oversold", 0.5, horizon=5, level=30.0),)
        planted = generate(MarketSpec(n_symbols=n_symbols, n_sessions=n_sessions, seed=sd, effects=eff))
        null = generate(MarketSpec(n_symbols=n_symbols, n_sessions=n_sessions, seed=sd))
        rp, rn = E.run_genome(genome, planted.bars), E.run_genome(genome, null.bars)
        mp, mn = E.metrics(rp), E.metrics(rn)
        diffs.append(mp["total_return"] - mn["total_return"])
        r_all += [t.r_multiple for t in rp.trades if np.isfinite(t.r_multiple)]
        gate.require("auditor", "ledger.trial", genome.genome_id)
        for tag, m, mk, rr in (("planted", mp, planted, rp), ("null", mn, null, rn)):
            trial_ids.append(L.record_trial(TrialRecord(
                family_id="agents:mock", kind="research" if tag == "planted" else "control_null",
                spec_digest=genome.genome_id, data_digest=mk.spec_digest, code_digest=CODE_ID, seed=sd,
                metrics={k: v for k, v in m.items() if np.isfinite(v)}, hypothesis_id=lh,
                artifacts={"gross": outcome_digest([t.r_multiple for t in rr.trades]),
                           "family_series": outcome_digest(rr.equity.pct_change().dropna().to_numpy())},
                notes="mock auditor %s market %d" % (tag, i))))
    attempt(gate.require, "auditor", "holdout.open", "agents:mock")
    research_ids = [t for t in trial_ids if L.entries("trial", t)[0].payload["kind"] == "research"]
    card = gauntlet(candidate_id="agents:mock:%s" % genome.genome_id, family_id="agents:mock",
                    gross=np.array(r_all), cost_per_obs=0.0, n_trials=max(1, L.family_trial_count("agents:mock")),
                    rng=stream(seed, "agents", "card"), cfg=GauntletConfig(n_boot=200), ledger=L,
                    trial_ids=research_ids)
    pre_out = PR.evaluate_once(L, pdig, value=float(np.mean(diffs)), effective_n=len(diffs),
                               data_first_ts_utc="2026-11-02T00:00:00+00:00", data_digest="synthetic")
    evaluation = {"paired_return_diffs": [float(x) for x in diffs], "mean_diff": float(np.mean(diffs)),
                  "positive_markets": int(sum(d > 0 for d in diffs)), "n_markets": n_markets,
                  "trades_r_multiples": len(r_all), "trial_ids": trial_ids}
    # 7. Librarian
    kb.record_lineage("librarian", hypothesis_id=hid, genome_id=genome.genome_id, trial_ids=trial_ids)
    if card.verdict != "PASS":
        kb.reject("librarian", hid, reason="report card %s: %s" % (card.verdict, "; ".join(card.reasons[:3])),
                  evidence=trial_ids,
                  retest_trigger={"requires": ["approved_real_data_snapshot", "calibrated_canary",
                                               "authority_signed_prereg"]})
    # 8. Reporter
    gate.require("reporter", "report.write", hid)
    report = "\n".join([
        "### Mock research run (offline, deterministic, synthetic)", "",
        "- Hypothesis `%s` (%s); status **%s**." % (hid, h1.template, kb.status(hid)),
        "- Synthetic evaluation: mean paired return difference %+.2f %% over %d markets (%d positive)."
        % (100 * evaluation["mean_diff"], n_markets, evaluation["positive_markets"]),
        "- Report card: **%s** (%s)." % (card.verdict, "; ".join(card.reasons) or "no reasons"),
        "- Pre-registration: passed=%s (%s)." % (pre_out.passed, pre_out.reason),
        "- Guards fired: %d refusals: %s." % (len(refusals), " | ".join(refusals)),
        "- Generated code: %d capability finding(s), may_execute=%s; never executed." % (
            len(lint["capability_findings"]), lint["may_execute"]),
    ])
    decisions = [e.payload for e in L.entries("agent_decision")]
    out = MockRun(decisions, refusals, kb.summary(), evaluation, {"verdict": card.verdict, "reasons": list(card.reasons)},
                  asdict(pre_out), report, len(L.entries()), L.verify().ok,
                  {"lint": lint, "genome": spec,
                   "trial_code_digests": sorted({e.payload["code_digest"] for e in L.entries("trial")})})
    L.close()
    return out


__all__ = ["CODE_PATHS", "CODE_ID", "FIXTURE_SOURCES", "GENERATED_CODE", "MockRun", "run"]
