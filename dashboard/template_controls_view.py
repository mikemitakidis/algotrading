"""M21 E6 — read-only dashboard view of the paper-lane template configuration (WP9 store).

Every change to the template configuration goes through the WP14 Promotion & Approval process
(``bot.promotion``): shadow comparison first, the change budget (one active proposal, at most one apply
in 7 days), an operator token bound to the exact content, RED cooling-off and auto-revert. That process
applies changes through ``bot.templates.controls``. This page SHOWS the result; it has no route that
proposes, approves, activates, rolls back or withdraws anything. (The first version of this blueprint had
such routes; the 30 Oct release-safety review found they bypassed WP14 and they were removed.)

Routes (GET only)
  GET /api/template-controls   JSON: active config (with its source and any fallback reasons), versions
                               (with their integrity check), the latest audit rows, Appendix A parameter bounds
  GET /template-controls       the same as a server-rendered page (every value HTML-escaped)

Safety
* Both routes require an authenticated dashboard session (``session['authed']``, as ``require_auth``).
* OFF by default: without ``TEMPLATE_CONTROLS_DB_PATH`` (Flask app config, else the environment) both
  answer 503. Reads use an SQLite ``mode=ro`` connection: a GET never creates or writes the store.
* No writer: this module never constructs ``TemplateControls`` and never calls a lifecycle method
  (pinned by test). Live routing stays ``bot.templates.routing.LIVE_TEMPLATES`` (Template 1); nothing
  here places orders or touches a broker.

Registration is a protected ``dashboard/app.py`` change and is NOT applied here; the reviewed patch is
``docs/m21/E6_dashboard_app_wiring.patch``:

    from dashboard.template_controls_view import bp as _e6_template_controls_bp
    app.register_blueprint(_e6_template_controls_bp)
"""
from __future__ import annotations

import json
import os
import sqlite3
from functools import wraps
from html import escape
from typing import Any, Dict, Mapping, Optional, Tuple

from flask import Blueprint, current_app, jsonify, session

from bot.templates import controls as TC
from bot.templates.core import APPENDIX_A, get as get_template
from bot.templates.routing import LIVE_TEMPLATES

bp = Blueprint("template_controls", __name__)
DB_KEY = "TEMPLATE_CONTROLS_DB_PATH"
AUDIT_TAIL = 30
PAGE = "/template-controls"
CHANGE_PATH = ("changes are made only through the Promotion & Approval process (WP14): shadow first, "
               "change budget, operator token, cooling-off, auto-revert")


def _path() -> Optional[str]:
    p = current_app.config.get(DB_KEY) or os.environ.get(DB_KEY, "").strip()
    return str(p) if p else None


def _guarded(f):
    """Auth first (401), then configuration (503)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authed"):
            return jsonify({"error": "Unauthorized"}), 401
        if _path() is None:
            return jsonify({"error": "not_configured", "detail": "%s is not set" % DB_KEY}), 503
        return f(*args, **kwargs)
    return wrapper


def _bounds() -> Dict[str, Dict[str, Any]]:
    out = {}
    for t in APPENDIX_A:
        d = get_template(t)
        out[t] = {"number": d.number, "description": d.description, "live": t in LIVE_TEMPLATES,
                  "params": {k: {"kind": p.kind, "default": p.default, "lo": p.lo, "hi": p.hi,
                                 "choices": list(p.choices)} for k, p in d.params.items()},
                  "constraints": [list(c) for c in d.constraints]}
    return out


def _read_rows(path: str) -> Tuple[list, list, Optional[str]]:
    """Versions and audit tail through a read-only connection: a GET never creates or writes the store."""
    try:
        c = sqlite3.connect("file:%s?mode=ro" % path.replace("%", "%25").replace("?", "%3f").replace("#", "%23"),
                            uri=True, timeout=10)
    except sqlite3.Error as exc:
        return [], [], "store_unreadable:%s" % type(exc).__name__
    try:
        c.row_factory = sqlite3.Row
        versions = [dict(r) for r in c.execute("SELECT * FROM template_config_versions ORDER BY version")]
        audit = [dict(r) for r in c.execute("SELECT * FROM (SELECT * FROM template_config_audit ORDER BY seq DESC "
                                            "LIMIT ?) ORDER BY seq", (AUDIT_TAIL,))]
        return versions, audit, None
    except sqlite3.Error as exc:
        return [], [], "store_unreadable:%s" % type(exc).__name__
    finally:
        c.close()


def state() -> Dict[str, Any]:
    path = _path()
    active = TC.read_active(path)
    versions, audit, problem = _read_rows(path)
    for v in versions:
        v["intact"] = TC.TemplateControls._intact(v)
    return {"lane": TC.LANE, "read_only": True, "change_path": CHANGE_PATH, "store_problem": problem,
            "live_templates": sorted(LIVE_TEMPLATES),
            "active": {"version": active.version, "source": active.source, "config": active.config,
                       "fallback_reasons": list(active.fallback_reasons)},
            "versions": versions, "audit": audit, "templates": _bounds(),
            "note": "selection controls the paper lane and the shadow runner; live routing stays Template 1 only"}


@bp.route("/api/template-controls", methods=["GET"])
@_guarded
def api_state():
    return jsonify(state())


def _e(x: Any) -> str:
    return escape("" if x is None else str(x))


def render_page(s: Mapping[str, Any]) -> str:
    a = s["active"]
    cur = a["version"]
    parts = ["<!DOCTYPE html><html><head><meta charset='utf-8'><title>Template controls</title></head><body>",
             "<h1>Paper-lane template configuration</h1>",
             "<p><strong>Read-only. Paper lane only.</strong> %s. %s. Live routing: %s.</p>" % (
                 _e(s["change_path"].capitalize()), _e(s["note"]), _e(", ".join(s["live_templates"])))]
    if s.get("store_problem"):
        parts.append("<p><strong>Store problem:</strong> %s (showing the built-in default)</p>" % _e(s["store_problem"]))
    parts.append("<h2>Active</h2><p>version %s (source %s)%s</p><pre>%s</pre>" % (
        _e(cur if cur is not None else "none: built-in default"), _e(a["source"]),
        "; fallback: " + _e(", ".join(a["fallback_reasons"])) if a["fallback_reasons"] else "",
        _e(json.dumps(a["config"], indent=1, sort_keys=True))))
    parts.append("<h2>Versions</h2><table><tr><th>version</th><th>status</th><th>author</th><th>reason</th>"
                 "<th>created</th><th>sha256</th><th>intact</th><th>config</th></tr>")
    for v in reversed(s["versions"]):
        parts.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                     "<td><pre>%s</pre></td></tr>" % tuple(_e(x) for x in (
                         v.get("version"), v.get("status"), v.get("author"), v.get("reason"), v.get("created_at_utc"),
                         (v.get("config_sha256") or "")[:12], v.get("intact"), v.get("config_json"))))
    parts.append("</table><h2>Parameter bounds</h2><table><tr><th>template</th><th>parameter</th><th>kind</th>"
                 "<th>default</th><th>min</th><th>max</th></tr>")
    for t, d in s["templates"].items():
        for k, p in d["params"].items():
            parts.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % tuple(
                _e(x) for x in (t, k, p["kind"], p["default"], p["lo"], p["hi"])))
    parts.append("</table><h2>Audit (latest %d)</h2><table><tr><th>at</th><th>actor</th><th>action</th>"
                 "<th>version</th><th>detail</th></tr>" % AUDIT_TAIL)
    for r in reversed(s["audit"]):
        parts.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % tuple(
            _e(r.get(k)) for k in ("at_utc", "actor", "action", "version", "detail")))
    parts.append("</table></body></html>")
    return "".join(parts)


@bp.route(PAGE, methods=["GET"])
@_guarded
def page():
    return render_page(state()), 200, {"Content-Type": "text/html; charset=utf-8"}


__all__ = ["bp", "DB_KEY", "CHANGE_PATH", "state", "render_page"]
