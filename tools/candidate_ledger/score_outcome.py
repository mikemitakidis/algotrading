"""M21 E4 — score versus outcome, from the candidate ledger (roadmap §5 "compare predicted vs actual"; C-4).

Reads the A02 candidate ledger READ-ONLY (SQLite ``mode=ro``) and reports, per execution class:

* the funnel: every candidate by terminal kind (not selected, scoring blocked, risk rejected, broker not
  accepted, outcome resolved, still open, ...);
* missing outcomes: candidates with an accepted order and no resolved outcome, and contradictions
  (a resolved terminal without an outcome record);
* score versus outcome for RESOLVED trades: fixed 10-point score bins with trade counts, win rate and its
  Wilson 95 % interval, mean R-multiple with a 95 % interval where R is known; and a Spearman rank
  association between score and R with a seeded permutation p-value;
* rejected, blocked and not-selected candidates: counts and score distribution only. Their outcomes were
  never observed; a counterfactual comparison belongs to the research counterfactual lane.

Execution classes are kept apart and never pooled:

* ``IBKR_PAPER_STATEMENT_MATCHED`` — IBKR paper trades whose journal trade id the E5 statement
                                 reconciliation MATCHED to the operator's IBKR statement (``matched_trade_ids``);
* ``IBKR_PAPER_JOURNAL``       — IBKR paper trades known only from OUR journal: database rows are not
                                 proof, so no association verdict is ever stated for them;
* ``SIMULATED``                — the local simulated broker: describes the machinery, never markets;
* ``UNKNOWN_SOURCE``           — an order with no data source, or with conflicting data sources;
* ``NO_ORDER``                 — candidates that never produced an order.

Verdicts are deliberately weak. Only statement-matched IBKR paper trades can carry an association
verdict; below ``min_n`` of them it is INSUFFICIENT_EVIDENCE. Above it, the association is DESCRIPTIVE
and IN-SAMPLE: it is not predictive performance. Simulated outcomes are always NOT_MARKET_EVIDENCE and
journal-only outcomes NOT_STATEMENT_CONFIRMED.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from bot.candidate_ledger.stages import ACCEPTED_STATUSES

IBKR_MATCHED, IBKR_PAPER = "IBKR_PAPER_STATEMENT_MATCHED", "IBKR_PAPER_JOURNAL"
SIMULATED, NO_ORDER, UNKNOWN_SOURCE = "SIMULATED", "NO_ORDER", "UNKNOWN_SOURCE"
CLASSES = (IBKR_MATCHED, IBKR_PAPER, SIMULATED, UNKNOWN_SOURCE, NO_ORDER)
INSUFFICIENT, NOT_MARKET, NOT_CONFIRMED = "INSUFFICIENT_EVIDENCE", "NOT_MARKET_EVIDENCE", "NOT_STATEMENT_CONFIRMED"
NO_ASSOC, POS_ASSOC, NEG_ASSOC = "NO_ASSOCIATION_DETECTED", "POSITIVE_ASSOCIATION_IN_SAMPLE", \
    "NEGATIVE_ASSOCIATION_IN_SAMPLE"
BINS = (0, 40, 50, 60, 70, 80, 90, 101)
RESOLVED = ("WIN", "LOSS", "TIMEOUT")
CAVEAT = ("Descriptive and in-sample: an association between score and paper outcomes is not predictive "
          "performance. Predictive claims need the pre-registered C-4 evaluation on enough out-of-sample trades.")


def _open_ro(path: str) -> sqlite3.Connection:
    p = str(path).replace("%", "%25").replace("?", "%3f").replace("#", "%23")
    return sqlite3.connect("file:%s?mode=ro" % p, uri=True)


def _num(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def load_candidates(conn: sqlite3.Connection, *, matched_trade_ids: Iterable[str] = ()
                    ) -> List[Dict[str, Any]]:
    """One row per candidate with the payload of the last attempt of each stage.
    ``matched_trade_ids``: journal trade ids the E5 statement reconciliation MATCHED."""
    matched = {str(t) for t in matched_trade_ids}
    cand = {r[0]: {"candidate_id": r[0], "cycle_key": r[1], "symbol": r[2], "direction": r[3],
                   "strategy_id": r[4], "decision_ts_utc": r[5], "terminal_kind": r[6], "stages": {}}
            for r in conn.execute("SELECT candidate_id, cycle_key, symbol, direction, strategy_id, "
                                  "decision_ts_utc, terminal_kind FROM candidate_ledger")}
    for cid, stage, payload in conn.execute(
            "SELECT candidate_id, stage, payload_json FROM candidate_ledger_events ORDER BY candidate_id, "
            "stage, attempt"):
        if cid in cand:
            try:
                cand[cid]["stages"][stage] = json.loads(payload or "{}")
            except ValueError:
                cand[cid]["stages"][stage] = {"_unreadable": True}
    out = []
    for c in cand.values():
        st = c["stages"]
        scored, scanned = st.get("scored") or {}, st.get("scanned") or {}
        score, src = _num(scored.get("research_rank")), "scored.research_rank"
        if score is None:
            score, src = _num(scanned.get("score")), "scanned.score"
        if score is None:
            src = None
        oc = st.get("outcome_resolved") or {}
        sources = {p.get("data_source") for p in (oc, st.get("broker_result") or {}, st.get("intent_logged") or {})
                   if p.get("data_source")}
        if "intent_logged" not in st:
            klass = NO_ORDER
        elif sources == {"simulated"}:
            klass = SIMULATED
        elif sources == {"ibkr_paper"}:
            klass = IBKR_MATCHED if oc.get("trade_id") is not None and str(oc["trade_id"]) in matched \
                else IBKR_PAPER
        else:                       # none recorded, an unknown one, or stages that disagree
            klass = UNKNOWN_SOURCE
        br = st.get("broker_result") or {}
        out.append(dict(c, score=score, score_source=src, execution_class=klass,
                        data_sources=sorted(sources),
                        risk_evidence=(st.get("risk_evaluated") or {}).get("evidence"),
                        selectable=scored.get("selectable"), has_intent="intent_logged" in st,
                        broker_status=br.get("status"), accepted=br.get("status") in ACCEPTED_STATUSES,
                        outcome=oc.get("outcome") if oc.get("outcome") in RESOLVED else None,
                        trade_id=oc.get("trade_id"),
                        r_multiple=_num(oc.get("r_multiple")), realized_pnl=_num(oc.get("realized_pnl")),
                        fees_confirmed=oc.get("fees_final") in (1, True)))
    return sorted(out, key=lambda r: (str(r["decision_ts_utc"]), r["candidate_id"]))


def wilson(k: int, n: int, z: float = 1.959963984540054) -> Tuple[Optional[float], Optional[float]]:
    if n == 0:
        return None, None
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, mid - half), min(1.0, mid + half)


def _mean_ci(x: Sequence[float]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    x = np.asarray(x, dtype=float)
    if len(x) == 0:
        return None, None, None
    m = float(x.mean())
    if len(x) < 2:
        return m, None, None
    half = 1.96 * float(x.std(ddof=1)) / math.sqrt(len(x))
    return m, m - half, m + half


def _ranks(x: np.ndarray) -> np.ndarray:
    order = np.argsort(x, kind="mergesort")
    r = np.empty(len(x))
    r[order] = np.arange(len(x), dtype=float)
    # average ties
    xs = x[order]
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[j + 1] == xs[i]:
            j += 1
        if j > i:
            r[order[i:j + 1]] = (i + j) / 2.0
        i = j + 1
    return r


def spearman(x: Sequence[float], y: Sequence[float], *, n_perm: int = 2000, seed: int = 20261029
             ) -> Tuple[Optional[float], Optional[float]]:
    """Spearman rho and a two-sided permutation p-value (seeded, reproducible)."""
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if len(x) < 3 or np.all(x == x[0]) or np.all(y == y[0]):
        return None, None
    rx, ry = _ranks(x), _ranks(y)
    rx, ry = rx - rx.mean(), ry - ry.mean()
    den = math.sqrt(float((rx * rx).sum() * (ry * ry).sum()))
    rho = float((rx * ry).sum() / den)
    rng = np.random.default_rng(seed)
    hits = sum(1 for _ in range(n_perm) if abs(float((rx * rng.permutation(ry)).sum() / den)) >= abs(rho) - 1e-12)
    return rho, (hits + 1) / (n_perm + 1)


def _bin(score: Optional[float]) -> str:
    if score is None:
        return "no_score"
    for lo, hi in zip(BINS[:-1], BINS[1:]):
        if lo <= score < hi:
            return "%d-%d" % (lo, min(hi, 100))
    return "out_of_range"


def _class_report(rows: List[Dict[str, Any]], klass: str, min_n: int) -> Dict[str, Any]:
    resolved = [r for r in rows if r["outcome"] is not None]
    bins = defaultdict(list)
    for r in resolved:
        bins[_bin(r["score"])].append(r)
    table = []
    for b in sorted(bins, key=lambda s: (s == "no_score", s)):
        g = bins[b]
        wins = sum(1 for r in g if r["outcome"] == "WIN")
        lo, hi = wilson(wins, len(g))
        rs = [r["r_multiple"] for r in g if r["r_multiple"] is not None]
        m, mlo, mhi = _mean_ci(rs)
        table.append({"score_bin": b, "trades": len(g), "wins": wins, "win_rate": wins / len(g),
                      "win_rate_ci95": [lo, hi], "n_with_r": len(rs), "mean_r": m, "mean_r_ci95": [mlo, mhi]})
    pairs = [(r["score"], r["r_multiple"]) for r in resolved if r["score"] is not None and r["r_multiple"] is not None]
    rho, p = spearman([a for a, _ in pairs], [b for _, b in pairs]) if len(pairs) >= 3 else (None, None)
    if klass == SIMULATED:
        verdict = NOT_MARKET
    elif klass != IBKR_MATCHED:
        verdict = NOT_CONFIRMED
    elif len(pairs) < min_n:
        verdict = INSUFFICIENT
    elif p is None or p >= 0.05:
        verdict = NO_ASSOC
    else:
        verdict = POS_ASSOC if rho > 0 else NEG_ASSOC
    accepted_open = [r["candidate_id"] for r in rows
                     if r["accepted"] and r["outcome"] is None and r["terminal_kind"] is None]
    no_broker = [r["candidate_id"] for r in rows
                 if r["has_intent"] and r["broker_status"] is None and r["terminal_kind"] is None]
    contradictions = [r["candidate_id"] for r in rows
                      if r["terminal_kind"] == "outcome_resolved" and r["outcome"] is None]
    return {"execution_class": klass, "candidates": len(rows), "resolved_trades": len(resolved),
            "trades_with_score_and_r": len(pairs),
            "outcomes": dict(Counter(r["outcome"] for r in resolved)),
            "missing_outcomes": {"accepted_without_resolved_outcome": len(accepted_open),
                                 "candidate_ids": accepted_open[:50],
                                 "intent_without_broker_result": len(no_broker),
                                 "intent_without_broker_result_ids": no_broker[:50],
                                 "resolved_terminal_without_outcome_record": len(contradictions),
                                 "resolved_terminal_without_outcome_record_ids": contradictions[:50]},
            "fees_unconfirmed_in_resolved": sum(1 for r in resolved if not r["fees_confirmed"]),
            "score_bins": table,
            "association": {"spearman_rho_score_vs_r": rho, "permutation_p": p, "min_n": min_n},
            "verdict": verdict}


def report(rows: List[Dict[str, Any]], *, min_n: int = 30, statement_reconciliation: Optional[str] = None
           ) -> Dict[str, Any]:
    by = defaultdict(list)
    for r in rows:
        by[r["execution_class"]].append(r)
    funnel = {k: dict(Counter(r["terminal_kind"] or "open" for r in v)) for k, v in sorted(by.items())}
    not_traded = [r for r in rows if r["terminal_kind"] in ("not_selected", "scoring_blocked", "risk_rejected",
                                                            "broker_unready", "broker_not_accepted")]
    rej_bins = Counter((r["terminal_kind"], _bin(r["score"])) for r in not_traded)
    return {
        "schema": "m21-e4-score-outcome/1", "caveat": CAVEAT, "min_n": min_n,
        "candidates": len(rows),
        "statement_reconciliation": statement_reconciliation or "none supplied: no IBKR paper trade is confirmed",
        "funnel_by_execution_class": funnel,
        "classes": {k: _class_report(by.get(k, []), k, min_n) for k in CLASSES if k != NO_ORDER},
        "not_traded": {"count": len(not_traded),
                       "by_terminal_and_score_bin": [{"terminal_kind": t, "score_bin": b, "count": n}
                                                     for (t, b), n in sorted(rej_bins.items())],
                       "note": "outcomes were not observed for these candidates; no counterfactual is implied"},
        "score_sources": dict(Counter(r["score_source"] or "none" for r in rows)),
        "risk_evidence_of_ordered": dict(Counter(r["risk_evidence"] or "none" for r in rows
                                                 if r["execution_class"] != NO_ORDER)),
    }


def render_markdown(rep: Mapping[str, Any]) -> str:
    out = ["# Score versus outcome (candidate ledger)", "", "> %s" % rep["caveat"], "",
           "Candidates: %d. Minimum statement-matched trades for any association statement: %d." % (
               rep["candidates"], rep["min_n"]),
           "Statement reconciliation: %s." % rep["statement_reconciliation"], ""]
    for k, c in rep["classes"].items():
        out += ["## %s — verdict **%s**" % (k, c["verdict"]), "",
                "Resolved trades %d (with score and R: %d); accepted without a resolved outcome: %d; "
                "resolved trades with unconfirmed fees: %d." % (
                    c["resolved_trades"], c["trades_with_score_and_r"],
                    c["missing_outcomes"]["accepted_without_resolved_outcome"], c["fees_unconfirmed_in_resolved"]),
                ""]
        if c["score_bins"]:
            out += ["| score | trades | wins | win rate (95% CI) | n with R | mean R (95% CI) |",
                    "|---|---|---|---|---|---|"]
            for b in c["score_bins"]:
                f = lambda v: "n/a" if v is None else "%.2f" % v
                out.append("| %s | %d | %d | %.2f (%s–%s) | %d | %s (%s–%s) |" % (
                    b["score_bin"], b["trades"], b["wins"], b["win_rate"], f(b["win_rate_ci95"][0]),
                    f(b["win_rate_ci95"][1]), b["n_with_r"], f(b["mean_r"]), f(b["mean_r_ci95"][0]),
                    f(b["mean_r_ci95"][1])))
            out.append("")
    out += ["## Not traded", "", "%d candidates (rejected, blocked, not selected): %s" % (
        rep["not_traded"]["count"], rep["not_traded"]["note"]), ""]
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.candidate_ledger.score_outcome",
                                 description="Read-only score-vs-outcome report from the candidate ledger.")
    ap.add_argument("--ledger-db", required=True, help="a COPY of the database holding the candidate ledger")
    ap.add_argument("--min-n", type=int, default=30)
    ap.add_argument("--statement-matches", help="E5 reconciliation JSON (its matched journal trade ids)")
    ap.add_argument("--out-json")
    ap.add_argument("--out-md")
    a = ap.parse_args(argv)
    matched, note = (), None
    if a.statement_matches:
        try:
            doc = json.loads(Path(a.statement_matches).read_text())
            matched = [str(t) for t in doc["matched_trade_ids"]]
            note = "%s (%s, %d matched trades)" % (Path(a.statement_matches).name, doc.get("schema"), len(matched))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print("statement matches unreadable: %s" % type(exc).__name__, file=sys.stderr)
            return 2
    try:
        conn = _open_ro(a.ledger_db)
        try:
            rep = report(load_candidates(conn, matched_trade_ids=matched), min_n=a.min_n,
                         statement_reconciliation=note)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print("candidate ledger unreadable: %s" % exc, file=sys.stderr)
        return 2
    if a.out_json:
        Path(a.out_json).write_text(json.dumps(rep, sort_keys=True, indent=1, default=str))
    md = render_markdown(rep)
    if a.out_md:
        Path(a.out_md).write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["IBKR_MATCHED", "IBKR_PAPER", "SIMULATED", "NO_ORDER", "UNKNOWN_SOURCE", "INSUFFICIENT", "NOT_MARKET",
           "NOT_CONFIRMED", "NO_ASSOC",
           "POS_ASSOC", "NEG_ASSOC", "load_candidates", "report", "render_markdown", "wilson", "spearman", "main"]
