"""M21.1extra-D3 — authenticated, read-only dashboard view of paper outcomes.

A Flask blueprint with two GET-only routes:

  /api/paper-loop/metrics   JSON from bot.runtime.paper_outcome_metrics
  /paper-loop/metrics       a small server-rendered panel (HTML-escaped)

Both require an authenticated dashboard session (the same ``session['authed']``
marker the main dashboard's ``require_auth`` checks) and return 401 otherwise.
Nothing here writes, places orders or touches a broker: the metrics module opens
the paper-loop database with an SQLite ``mode=ro`` URI.

The database path comes from the app config key ``PAPER_LIFECYCLES_DB_PATH``
when set (tests), else the metrics module's default resolution.

Registration (one line in dashboard/app.py, shipped as a separate integration
change because app.py is a protected runtime file):

    from dashboard.paper_metrics_view import bp as paper_metrics_bp
    app.register_blueprint(paper_metrics_bp)
"""
from __future__ import annotations

from functools import wraps
from html import escape

from flask import Blueprint, current_app, jsonify, session

from bot.runtime.paper_outcome_metrics import compute_paper_metrics, display

bp = Blueprint("paper_metrics", __name__)


def _authed_only(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authed"):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


def _metrics():
    return compute_paper_metrics(current_app.config.get("PAPER_LIFECYCLES_DB_PATH"))


@bp.route("/api/paper-loop/metrics", methods=["GET"])
@_authed_only
def paper_loop_metrics_api():
    return jsonify(_metrics())


_ROWS = (("Trades", "trades", "int"), ("Win rate", "win_rate", "pct"),
         ("Avg win", "avg_win", "money"), ("Avg loss", "avg_loss", "money"),
         ("Total P&L", "total_pnl", "money"), ("Expectancy / trade", "expectancy", "money"),
         ("Profit factor", "profit_factor", "num"), ("Max drawdown", "max_drawdown", "num"))


def _metric_table(groups):
    head = "".join("<th>%s</th>" % escape(name) for name in groups)
    body = []
    for label, key, kind in _ROWS:
        cells = "".join("<td>%s</td>" % escape(display(m.get(key), kind))
                        for m in groups.values())
        body.append("<tr><th>%s</th>%s</tr>" % (escape(label), cells))
    return "<table><tr><th></th>%s</tr>%s</table>" % (head, "".join(body))


def render_panel(m) -> str:
    edge = m["edge_outcomes"]
    groups = {"All strategies": edge["overall"]}
    groups.update(edge["by_strategy"])
    eq = m["broker_equity"]
    mech = m["mechanical_lifecycles"]
    note = ("No genuine paper-trade outcomes yet: metrics show N/A until the loop "
            "records closed IBKR paper trades." if edge["rows_genuine"] == 0 else
            "Closed IBKR paper trades only (simulated data excluded).")
    latest = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            escape(str(r["symbol"])), escape(str(r["persisted_at_utc"])),
            "yes" if r["lifecycle_confirmed"] else "no",
            "yes" if r["flatten_confirmed"] else "no")
        for r in mech["latest"])
    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>Paper loop outcomes</title></head><body>",
        "<h1>IBKR paper loop: trading outcomes</h1>",
        "<p>%s</p>" % escape(note),
        _metric_table(groups),
        "<p>Genuine rows: %d; excluded (simulated/unknown source): %d.</p>" % (
            edge["rows_genuine"], edge["excluded_non_genuine"]),
        "<h2>Broker equity (IBKR paper)</h2>",
        "<p>Latest: %s %s; peak: %s; drawdown: %s (%s); snapshots: %d.</p>" % (
            escape(display(eq["latest_equity"])), escape(str(eq["currency"] or "")),
            escape(display(eq["peak_equity"])), escape(display(eq["equity_drawdown"])),
            escape(display(eq["equity_drawdown_pct"], "pct")), eq["snapshots"]),
        "<h2>Mechanical lifecycles (not trades)</h2>",
        "<p>%s. Total %d, confirmed %d, flatten confirmed %d, not confirmed %d.</p>" % (
            escape(mech["note"]), mech["total"], mech["lifecycle_confirmed"],
            mech["flatten_confirmed"], mech["not_confirmed"]),
        "<table><tr><th>Symbol</th><th>Persisted (UTC)</th><th>Confirmed</th>"
        "<th>Flat</th></tr>%s</table>" % latest,
    ]
    if m.get("error"):
        parts.append("<p>Data error: %s</p>" % escape(m["error"]))
    parts.append("</body></html>")
    return "".join(parts)


@bp.route("/paper-loop/metrics", methods=["GET"])
@_authed_only
def paper_loop_metrics_page():
    return render_panel(_metrics()), 200, {"Content-Type": "text/html; charset=utf-8"}
