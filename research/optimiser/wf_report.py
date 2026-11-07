"""research.optimiser.wf_report — WP13 walk-forward results as research report cards (M21 E1).

The walk-forward driver (``walkforward.run``) already decides PASS / FAIL /
INCONCLUSIVE per template. This module turns each template result into the
existing ``research.integrity.report_card.ReportCard`` and adds the two things
a walk-forward verdict alone cannot show:

* that the decision procedure is CALIBRATED for this exact experiment: the
  canary battery is verified from THIS ledger by the report card's own check
  (``report_card._canary_status``), against the experiment digest computed
  from the SAME ``WFConfig`` the run used (``canary.experiment_digest``);
* what kind of evidence it is: synthetic data describes the machinery, never a
  market, so a synthetic run can never produce a PASS card.

Card verdict (fail-closed; FAIL takes precedence, then INCONCLUSIVE):

* FAIL         walk-forward FAIL, or the one-shot historical holdout FAILED;
* INCONCLUSIVE walk-forward INCONCLUSIVE; the result is not the one recorded
               in this ledger (``wf_run`` entry) or the ledger chain does not
               verify; the run's config is not the config the experiment was
               computed from; synthetic origin; holdout not run or
               inconclusive; canary missing, not CALIBRATED or not verified
               for this experiment; no ledgered trial of the chosen genome;
* PASS         otherwise: real (close-stamped) data, walk-forward PASS,
               holdout PASS, and a canary verified for this experiment.

The walk-forward's own verdict and reasons are kept unchanged in the card
(``inputs['wf_verdict']`` and the reasons list). Every card is appended to the
ledger as a ``report_card`` entry. Nothing here runs a battery, touches a
broker, the network or production data.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Dict, Mapping, Optional

from research.common.hashing import digest
from research.integrity.controls import BatteryResult
from research.integrity.ledger import Ledger
from research.integrity.report_card import DISCLAIMER, GauntletConfig, ReportCard, _canary_status
from research.optimiser import canary as C
from research.optimiser import walkforward as W

PASS, FAIL, INCONCLUSIVE = W.PASS, W.FAIL, W.INCONCLUSIVE
SYNTHETIC_REASON = "synthetic data: describes the machinery, not a market (never a PASS card)"
EVIDENCE_REAL, EVIDENCE_SYNTHETIC = "REAL_HISTORICAL_BARS", "SYNTHETIC_MACHINERY"


def _canonical(payload: Mapping[str, Any]) -> str:
    """The digest of a payload exactly as the ledger stored it (JSON round trip)."""
    return digest(json.loads(json.dumps(payload, sort_keys=True, default=str)))


def _recorded_run(ledger: Ledger, result: Mapping[str, Any]):
    """The ledger's ``wf_run`` entry equal to ``result`` (folds excluded, as the driver records it)."""
    want = dict({k: v for k, v in result.items() if k != "folds"}, n_folds=len(result.get("folds") or ()))
    target = _canonical(want)
    hits = [e for e in ledger.entries("wf_run", result.get("family_id", ""))
            if _canonical(e.payload) == target]
    return hits[-1] if hits else None


def _candidate_trial(ledger: Ledger, result: Mapping[str, Any], before_seq: int) -> Optional[str]:
    """The ledgered trial of the genome the last fold chose (the code that produced the result)."""
    folds = result.get("folds") or ()
    if not folds:
        return None
    gid = folds[-1].get("chosen_genome_id")
    rows = [e for e in ledger.trials(result["family_id"])
            if e.payload.get("spec_digest") == gid and e.seq < before_seq]
    return rows[-1].ref_id if rows else None


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def template_card(result: Mapping[str, Any], *, cfg: W.WFConfig, run_config_digest: str, ledger: Ledger,
                  canary: Optional[BatteryResult], protocol: C.CanaryProtocol = C.PROTOCOL_V3,
                  gauntlet: GauntletConfig = GauntletConfig()) -> ReportCard:
    """One template's walk-forward result as a report card (see the module docstring).
    ``run_config_digest`` is the ``config['digest']`` of the ``walkforward.run`` output the result came
    from; ``cfg`` must be that config (the experiment identity is computed from it)."""
    template, family = result["template"], result["family_id"]
    if family != "wf:%s" % template:
        raise ValueError("result family %r does not match its template %r" % (family, template))
    s = result.get("summary") or {}
    fail, inc = [], []
    wf_verdict = result.get("verdict")
    if wf_verdict not in (PASS, FAIL, INCONCLUSIVE):
        raise ValueError("walk-forward verdict must be PASS, FAIL or INCONCLUSIVE, got %r" % (wf_verdict,))
    hold = (result.get("holdout") or {}).get("status", W.HOLDOUT_NOT_RUN)

    if wf_verdict == FAIL:
        fail.append("walk-forward FAIL: %s" % ", ".join(result.get("reasons") or ()))
    elif wf_verdict == INCONCLUSIVE:
        inc.append("walk-forward INCONCLUSIVE: %s" % ", ".join(result.get("reasons") or ()))
    if wf_verdict == PASS and hold == FAIL:
        fail.append("one-shot historical holdout FAILED")
    elif hold != PASS:
        inc.append("historical holdout %s" % hold)

    entry = _recorded_run(ledger, result)
    chain = ledger.verify()
    if entry is None:
        inc.append("result is not the walk-forward run recorded in this ledger")
    if not chain.ok:
        inc.append("ledger chain does not verify (%s)" % chain.reason)

    cfg_digest = W._cfg_dict(cfg)["digest"]
    if run_config_digest != cfg_digest:
        inc.append("run config differs from the config the experiment identity is computed from")

    origin = result.get("origin")
    if origin != "real":
        inc.append(SYNTHETIC_REASON)

    try:
        required = C.experiment_digest(template, cfg, protocol)
    except ValueError as exc:                     # no canary design exists for this template
        required = None
        inc.append("no canary experiment is defined for this template (%s)" % exc)
    trial = _candidate_trial(ledger, result, entry.seq if entry is not None else 10 ** 12)
    if trial is None:
        inc.append("no ledgered trial of the chosen genome: the code that produced the result is unknown")
        can_ok, can_why = False, "no candidate trial"
    elif required is None:
        can_ok, can_why = False, "no required experiment"
    else:
        can_ok, can_why = _canary_status(canary, ledger, family, trial, gauntlet, required)
    if canary is None or canary.verdict != "CALIBRATED":
        inc.append("walk-forward decision procedure has no CALIBRATED canary battery")
    elif not can_ok:
        inc.append("canary battery not verified for this experiment (%s)" % can_why)

    if fail:
        verdict, reasons = FAIL, fail + inc
    elif inc:
        verdict, reasons = INCONCLUSIVE, inc
    else:
        verdict, reasons = PASS, ["walk-forward PASS, holdout PASS, canary verified for this experiment, real data"]

    dsr = s.get("dsr") or {}
    pbo = s.get("pbo") or {}
    hd = result.get("holdout") or {}
    numbers = {
        "oos_days": _num(s.get("oos_days")), "oos_trades": _num(s.get("oos_trades")),
        "oos_sharpe_ann": _num(s.get("oos_sharpe_ann")), "oos_mean_net_r": _num(s.get("oos_mean_net_r")),
        "default_params_oos_sharpe_ann": _num(s.get("default_params_oos_sharpe_ann")),
        "stress_oos_sharpe_ann": _num(s.get("stress_oos_sharpe_ann")),
        "stress_multiplier": _num(s.get("stress_multiplier")),
        "n_trials_family": _num(s.get("n_trials_family")),
        "dsr": _num(dsr.get("dsr")), "pbo": _num(pbo.get("pbo")),
        "oos_no_fill": _num(s.get("oos_no_fill")), "oos_inconclusive": _num(s.get("oos_inconclusive")),
        "oos_exit_capacity_breach": _num(s.get("oos_exit_capacity_breach")),
        "holdout_trades": _num(hd.get("trades")), "holdout_sharpe_ann": _num(hd.get("sharpe_ann")),
        "canary_verified": 1.0 if can_ok else 0.0,
        "canary_false_pass_upper": _num(canary.false_pass_upper) if canary is not None else float("nan"),
        "real_data": 1.0 if origin == "real" else 0.0,
        "ledger_recorded": 1.0 if entry is not None else 0.0,
    }
    inputs = {
        "wf_verdict": wf_verdict, "wf_reasons": list(result.get("reasons") or ()), "holdout_status": hold,
        "origin": origin, "evidence_class": EVIDENCE_REAL if origin == "real" else EVIDENCE_SYNTHETIC,
        "wf_run_entry_hash": entry.entry_hash if entry is not None else None,
        "candidate_trial_id": trial, "required_canary_experiment": required,
        "canary_plan_digest": canary.plan_digest if canary is not None else None,
        "canary_protocol": protocol.name, "wf_config_digest": cfg_digest,
        "ledger_tip": ledger.tip_hash,
    }
    card = ReportCard(template, family, verdict, tuple(reasons), numbers, inputs,
                      {"gauntlet": asdict(gauntlet), "source": "research.optimiser.wf_report"}, DISCLAIMER)
    ledger.append("report_card", card.to_dict(), ref_id=family)
    return card


def run_cards(run_output: Mapping[str, Any], *, cfg: W.WFConfig, ledger: Ledger,
              canaries: Mapping[str, BatteryResult], protocol: C.CanaryProtocol = C.PROTOCOL_V3,
              gauntlet: GauntletConfig = GauntletConfig()) -> Dict[str, ReportCard]:
    """A card per template of a ``walkforward.run`` output, all bound to the config the run used."""
    run_cfg = (run_output.get("config") or {}).get("digest")
    if run_cfg != W._cfg_dict(cfg)["digest"]:
        raise ValueError("the supplied WFConfig is not the config this run used")
    return {t: template_card(r, cfg=cfg, run_config_digest=run_cfg, ledger=ledger, canary=canaries.get(t),
                             protocol=protocol, gauntlet=gauntlet)
            for t, r in sorted((run_output.get("templates") or {}).items())}


def render_markdown(cards: Mapping[str, ReportCard]) -> str:
    synthetic = any(c.inputs.get("evidence_class") != EVIDENCE_REAL for c in cards.values())
    head = ["# Walk-forward report cards", ""]
    if synthetic:
        head += ["> **SYNTHETIC** — these cards describe the machinery, not a market.", ""]
    head += ["| template | card | walk-forward | holdout | canary verified | evidence |", "|---|---|---|---|---|---|"]
    for t, c in sorted(cards.items()):
        head.append("| %s | **%s** | %s | %s | %s | %s |" % (
            t, c.verdict, c.inputs["wf_verdict"], c.inputs["holdout_status"],
            "yes" if c.numbers["canary_verified"] == 1.0 else "no", c.inputs["evidence_class"]))
    return "\n".join(head + [""] + [c.to_markdown() + "\n" for _, c in sorted(cards.items())])


__all__ = ["PASS", "FAIL", "INCONCLUSIVE", "SYNTHETIC_REASON", "EVIDENCE_REAL", "EVIDENCE_SYNTHETIC",
           "template_card", "run_cards", "render_markdown"]
