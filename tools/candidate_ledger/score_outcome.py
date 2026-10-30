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
                                 reconciliation MATCHED to the operator's IBKR statement (``matched_trade_ids`` of an
                                 ``m21-e5-reconciliation/3`` report, accepted only after ``verify_statement_evidence``
                                 has checked it against the same statement files and journal copy, see below);
* ``IBKR_PAPER_JOURNAL``       — IBKR paper trades known only from OUR journal: database rows are not
                                 proof, so no association verdict is ever stated for them;
* ``SIMULATED``                — the local simulated broker: describes the machinery, never markets;
* ``UNKNOWN_SOURCE``           — an order with no data source, or with conflicting data sources;
* ``NO_ORDER``                 — candidates that never produced an order.

Statement evidence (``--statement-matches``) is never taken on trust. It needs the SAME statement file(s)
(``--statement``) and journal copy (``--journal-db``) the reconciliation used, and is refused unless:
the report's own sha256 matches its content (edits); every input digest matches (other, stale or unrelated
inputs); it is at most ``--max-evidence-age-days`` old and not from the future; the account scope is one
verified paper account; every matched trade exists in the journal as a CLOSED ``ibkr_paper`` order on that
account; every identifier that links a leg to the statement appears in the statement files and belongs to that
journal order; no identifier or statement row supports two legs; and the ledger and journal agree on each
matched trade's candidate and symbol. Trades whose statement fees are unknown are reported with unconfirmed
fees. Digests detect edits and bind the evidence to exact inputs; they are not signatures. The authoritative
re-derivation is ``python -m research.economics.statement --verify-evidence`` on the same inputs, and that the
statement came from IBKR is established by the operator who exported it (runbook OP1.6/OP1.7).

Verdicts are deliberately weak. Only statement-matched IBKR paper trades can carry an association
verdict; below ``min_n`` of them it is INSUFFICIENT_EVIDENCE. Above it, the association is DESCRIPTIVE
and IN-SAMPLE: it is not predictive performance. Simulated outcomes are always NOT_MARKET_EVIDENCE and
journal-only outcomes NOT_STATEMENT_CONFIRMED.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
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
E5_SCHEMA = "m21-e5-reconciliation/3"     # the E5 report version (tools never import the research package)
ACCOUNT_DIGEST_PREFIX = "m21-ibkr-account:"   # = research.economics.statement.ACCOUNT_DIGEST_PREFIX
VERIFIED_SCOPE = "VERIFIED_SINGLE_PAPER_ACCOUNT"
MAX_EVIDENCE_AGE_DAYS = 7
FUTURE_SKEW = timedelta(minutes=5)
EVIDENCE_CAVEAT = ("digests bind the statement evidence to these exact inputs and detect edits; they are not "
                   "signatures: that the statement came from IBKR is established by the operator who exported it")
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


class EvidenceRefused(ValueError):
    """The statement evidence does not hold up against its inputs; no trade is treated as statement-matched."""


def _file_sha256(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def canonical_digest(doc: Mapping[str, Any]) -> str:
    """sha256 of the canonical JSON of a report without its ``report_sha256`` (as E5 computes it)."""
    body = {k: v for k, v in doc.items() if k != "report_sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                                     allow_nan=False).encode()).hexdigest()


def _statement_cells(paths: Sequence[str]) -> set:
    cells = set()
    for p in paths:
        with open(p, encoding="utf-8-sig", newline="") as fh:
            for row in csv.reader(fh):
                cells.update(c.strip() for c in row if c.strip())
    return cells


def _aware(s: Any) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo is not None else None


def verify_statement_evidence(doc: Mapping[str, Any], *, statements: Sequence[str], journal_db: str,
                              now: Optional[datetime] = None, max_age_days: float = MAX_EVIDENCE_AGE_DAYS
                              ) -> Dict[str, Any]:
    """Check an E5 report against the statement files and journal copy it claims to come from.
    Raises ``EvidenceRefused`` listing every problem; returns the matched ids and their fee status."""
    now = now or datetime.now(timezone.utc)
    bad: List[str] = []
    if doc.get("schema") != E5_SCHEMA:
        raise EvidenceRefused("not an %s report" % E5_SCHEMA)
    if doc.get("report_sha256") != canonical_digest(doc):
        bad.append("report_altered")
    ev = doc.get("evidence")
    ids = doc.get("matched_trade_ids")
    if not isinstance(ev, dict) or not isinstance(ids, list) or not all(isinstance(t, str) for t in ids):
        raise EvidenceRefused("; ".join(bad + ["evidence_missing_or_malformed"]))
    gen = _aware(ev.get("generated_at_utc"))
    if gen is None:
        bad.append("generation_time_unreadable")
    elif gen > now + FUTURE_SKEW:
        bad.append("generated_in_the_future")
    elif now - gen > timedelta(days=max_age_days):
        bad.append("stale:older_than_%g_days" % max_age_days)
    inputs = ev.get("inputs") or {}
    if sorted(x.get("sha256") for x in inputs.get("statements") or []) != sorted(_file_sha256(p) for p in statements):
        bad.append("statement_files_differ_from_the_reconciled_ones")
    if (inputs.get("journal_db") or {}).get("sha256") != _file_sha256(journal_db):
        bad.append("journal_snapshot_differs_from_the_reconciled_one")
    scope = ev.get("account_scope") or {}
    if ids and (scope.get("status") != VERIFIED_SCOPE or not scope.get("account_digest")):
        bad.append("account_scope_not_verified")
    trades = ev.get("matched_trades") or []
    if [t.get("trade_id") for t in trades] != ids or len(set(ids)) != len(ids):
        bad.append("matched_ids_inconsistent_with_manifest")
    cells = _statement_cells(statements)
    seen_links, seen_rows = set(), set()
    c = _open_ro(journal_db)
    try:
        c.row_factory = sqlite3.Row
        for t in trades:
            tid = t.get("trade_id")
            o = c.execute("SELECT * FROM loop_orders WHERE client_ref=?", (tid,)).fetchone()
            if o is None:
                bad.append("%s:not_in_journal" % tid)
                continue
            o = dict(o)
            acct = (o.get("account") or "").strip()
            if o.get("data_source") != "ibkr_paper" or o.get("state") != "CLOSED":
                bad.append("%s:not_a_closed_ibkr_paper_order" % tid)
            if not acct.upper().startswith("DU") or hashlib.sha256(
                    (ACCOUNT_DIGEST_PREFIX + acct).encode()).hexdigest() != scope.get("account_digest"):
                bad.append("%s:journal_account_not_the_verified_paper_account" % tid)
            claimed = {}
            for r in c.execute("SELECT exec_id, leg FROM loop_exec_claims WHERE client_ref=?", (tid,)):
                claimed[r["exec_id"]] = "entry" if r["leg"] == "entry" else "exit"
            oref = o.get("order_ref")
            allowed = {"entry": {"order_ref": {oref} - {None}, "perm_id_as_IBOrderID": {o.get("broker_perm_id")} - {None}},
                       "exit": {"order_ref": {oref, (oref or "") + ":FLAT"} - {None, ":FLAT"},
                                "perm_id_as_IBOrderID": {o.get(k) for k in ("tp_perm_id", "sl_perm_id",
                                                                             "flatten_perm_id")} - {None}}}
            legs = t.get("legs") or []
            if sorted(l.get("leg") for l in legs) != ["entry", "exit"]:
                bad.append("%s:legs_incomplete" % tid)
            for l in legs:
                kind, key, vals = l.get("leg"), l.get("key"), l.get("link_values") or []
                if not vals:
                    bad.append("%s:%s:no_identifier_link" % (tid, kind))
                for v in vals:
                    if v not in cells:
                        bad.append("%s:%s:identifier_not_in_statement" % (tid, kind))
                    if key == "exec_id":
                        ok = claimed.get(v) == kind
                    else:
                        ok = str(v) in {str(x) for x in (allowed.get(kind) or {}).get(key, set())}
                    if not ok:
                        bad.append("%s:%s:identifier_not_this_journal_order" % (tid, kind))
                    # one orderRef legitimately links both legs of ONE trade (bracket children share it); an
                    # identifier may never link the same leg kind twice (rows below are the execution-level check)
                    if (key, v, kind) in seen_links:
                        bad.append("%s:%s:identifier_supports_two_legs" % (tid, kind))
                    seen_links.add((key, v, kind))
                for row in l.get("statement_rows") or []:
                    if row in seen_rows:
                        bad.append("%s:%s:statement_row_supports_two_legs" % (tid, kind))
                    seen_rows.add(row)
    finally:
        c.close()
    if bad:
        raise EvidenceRefused("; ".join(dict.fromkeys(bad)))
    return {"matched": list(ids), "fees_known": {t["trade_id"]: bool(t.get("fees_known")) for t in trades},
            "summary": {"report_sha256": doc["report_sha256"], "generated_at_utc": ev["generated_at_utc"],
                        "statement_sha256": sorted(x["sha256"] for x in inputs["statements"]),
                        "journal_db_sha256": inputs["journal_db"]["sha256"],
                        "account_scope": {"status": scope.get("status"), "account_masked": scope.get("account_masked")},
                        "matched_trades": len(ids),
                        "fees_unknown_trades": sum(1 for t in trades if not t.get("fees_known")),
                        "max_age_days": max_age_days, "caveat": EVIDENCE_CAVEAT}}


def check_ledger_link(rows: Sequence[Mapping[str, Any]], journal_db: str, matched: Iterable[str]) -> None:
    """The ledger copy and the journal copy must describe the same matched trades."""
    want, bad = set(matched), []
    seen = Counter(str(r["trade_id"]) for r in rows if r.get("trade_id") is not None and str(r["trade_id"]) in want)
    bad += ["%s:on_%d_ledger_candidates" % (t, n) for t, n in seen.items() if n > 1]
    c = _open_ro(journal_db)
    try:
        for r in rows:
            tid = None if r.get("trade_id") is None else str(r["trade_id"])
            if tid not in want:
                continue
            o = c.execute("SELECT candidate_id, symbol FROM loop_orders WHERE client_ref=?", (tid,)).fetchone()
            if o is None or (o[0] is not None and o[0] != r["candidate_id"]) or o[1] != r["symbol"]:
                bad.append("%s:ledger_and_journal_disagree" % tid)
            if r.get("execution_class") != IBKR_MATCHED:
                bad.append("%s:ledger_source_is_not_ibkr_paper" % tid)
    finally:
        c.close()
    if bad:
        raise EvidenceRefused("; ".join(bad))


def load_candidates(conn: sqlite3.Connection, *, matched_trade_ids: Iterable[str] = (),
                    statement_fees_known: Optional[Mapping[str, bool]] = None) -> List[Dict[str, Any]]:
    """One row per candidate with the payload of the last attempt of each stage.
    ``matched_trade_ids``: journal trade ids of VERIFIED statement evidence (``verify_statement_evidence``);
    ``statement_fees_known``: per matched trade, whether the statement reported its fees (unknown stays unknown)."""
    matched = {str(t) for t in matched_trade_ids}
    fees_known = dict(statement_fees_known or {})
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
                        fees_confirmed=oc.get("fees_final") in (1, True) and (
                            klass != IBKR_MATCHED or statement_fees_known is None
                            or fees_known.get(str(oc.get("trade_id")), False))))
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


def report(rows: List[Dict[str, Any]], *, min_n: int = 30, statement_reconciliation: Optional[str] = None,
           statement_evidence: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
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
        "statement_evidence": dict(statement_evidence) if statement_evidence else None,
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
    ev = rep.get("statement_evidence")
    if ev:
        out += ["Statement evidence: account scope %s (%s); generated %s; statement fees unknown on %d matched "
                "trade(s), reported as unconfirmed. %s." % (
                    ev["account_scope"]["status"], ev["account_scope"]["account_masked"], ev["generated_at_utc"],
                    ev["fees_unknown_trades"], ev["caveat"].capitalize()), ""]
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
    ap.add_argument("--statement-matches", help="E5 reconciliation JSON (m21-e5-reconciliation/3)")
    ap.add_argument("--statement", action="append", default=[],
                    help="with --statement-matches: the SAME statement file(s) the reconciliation used")
    ap.add_argument("--journal-db", help="with --statement-matches: the SAME journal copy the reconciliation used")
    ap.add_argument("--max-evidence-age-days", type=float, default=MAX_EVIDENCE_AGE_DAYS)
    ap.add_argument("--out-json")
    ap.add_argument("--out-md")
    a = ap.parse_args(argv)
    matched, note, fees_known, evidence = (), None, None, None
    if a.statement_matches:
        if not a.statement or not a.journal_db:
            print("--statement-matches needs the same --statement file(s) and --journal-db the reconciliation used",
                  file=sys.stderr)
            return 2
        try:
            doc = json.loads(Path(a.statement_matches).read_text())
            v = verify_statement_evidence(doc, statements=a.statement, journal_db=a.journal_db,
                                          max_age_days=a.max_evidence_age_days)
        except EvidenceRefused as exc:
            print("statement evidence refused: %s" % exc, file=sys.stderr)
            return 2
        except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error) as exc:
            print("statement evidence unreadable: %s" % type(exc).__name__, file=sys.stderr)
            return 2
        matched, fees_known, evidence = v["matched"], v["fees_known"], v["summary"]
        note = "%s (%s, %d matched trades, verified against its inputs; report sha256 %s)" % (
            Path(a.statement_matches).name, E5_SCHEMA, len(matched), doc["report_sha256"][:12])
    try:
        conn = _open_ro(a.ledger_db)
        try:
            rows = load_candidates(conn, matched_trade_ids=matched, statement_fees_known=fees_known)
        finally:
            conn.close()
        if matched:
            check_ledger_link(rows, a.journal_db, matched)
        rep = report(rows, min_n=a.min_n, statement_reconciliation=note, statement_evidence=evidence)
    except EvidenceRefused as exc:
        print("statement evidence refused: %s" % exc, file=sys.stderr)
        return 2
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
           "POS_ASSOC", "NEG_ASSOC", "EvidenceRefused", "canonical_digest", "verify_statement_evidence",
           "check_ledger_link", "load_candidates", "report", "render_markdown", "wilson", "spearman", "main"]
