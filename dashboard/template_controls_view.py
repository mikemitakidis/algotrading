"""M21 E6 — dashboard view and controls for the paper-lane template configuration (WP9 Phase 1 UI).

A Flask blueprint over ``bot.templates.controls`` (the WP9 store). It adds no control logic of its own:
validation, the propose -> approve (named human) -> activate -> rollback lifecycle, compare-and-swap,
integrity checks and the audit trail all stay in ``bot.templates.controls``.

Routes
  GET  /api/template-controls                 JSON: active config (with its source and any fallback reasons),
                                              versions, audit tail, Appendix A parameter bounds
  GET  /template-controls                     the same as a server-rendered page (every value HTML-escaped)
  POST /api/template-controls/propose         {config, author, reason}
  POST /api/template-controls/approve         {version, approver, reason}
  POST /api/template-controls/activate        {version, actor, expected_current}   (compare-and-swap)
  POST /api/template-controls/rollback        {actor, reason, expected_current}    (compare-and-swap)
  POST /api/template-controls/withdraw        {version, actor, reason}

Safety
* Every route needs an authenticated dashboard session (``session['authed']``, as ``require_auth``);
  every POST also needs the session CSRF token (``dashboard.auth.csrf.csrf_required``).
* OFF by default twice over: without ``TEMPLATE_CONTROLS_DB_PATH`` every route answers 503; without
  ``TEMPLATE_CONTROLS_WRITES_ENABLED`` every POST answers 403 and the page is read-only. Each is read from
  the Flask app config, else from the environment (``TEMPLATE_CONTROLS_WRITES_ENABLED=1``); both unset
  by default.
* ``activate`` and ``rollback`` require ``expected_current``: the switch happens only if that version is
  still the active one (checked inside the store's write transaction).
* Approval needs a named human; ``bot.templates.controls`` refuses automation names and audits the refusal.
* Paper lane only. Selecting a template never makes it tradable: live routing stays
  ``bot.templates.routing.LIVE_TEMPLATES`` (Template 1). Nothing here places orders or touches a broker.

Registration is a protected ``dashboard/app.py`` change and is NOT applied here; the reviewed patch is
``docs/m21/E6_dashboard_app_wiring.patch``:

    from dashboard.template_controls_view import bp as _e6_template_controls_bp
    app.register_blueprint(_e6_template_controls_bp)
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from html import escape
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import quote

from flask import Blueprint, current_app, jsonify, redirect, request, session

from bot.templates import controls as TC
from bot.templates.core import APPENDIX_A, get as get_template
from bot.templates.routing import LIVE_TEMPLATES
from dashboard.auth.csrf import CSRF_FORM_FIELD, csrf_required, get_csrf_token

bp = Blueprint("template_controls", __name__)
DB_KEY = "TEMPLATE_CONTROLS_DB_PATH"
WRITES_KEY = "TEMPLATE_CONTROLS_WRITES_ENABLED"
AUDIT_TAIL = 30
PAGE = "/template-controls"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _path() -> Optional[str]:
    p = current_app.config.get(DB_KEY) or os.environ.get(DB_KEY, "").strip()
    return str(p) if p else None


def _writes_enabled() -> bool:
    if WRITES_KEY in current_app.config:
        return current_app.config[WRITES_KEY] is True
    return os.environ.get(WRITES_KEY, "").strip() == "1"


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


def _writable(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _writes_enabled():
            return _done(403, {"error": "read_only", "detail": "%s is not enabled" % WRITES_KEY})
        return f(*args, **kwargs)
    return wrapper


def _from_form() -> bool:
    return not request.is_json


def _body() -> Dict[str, Any]:
    if request.is_json:
        b = request.get_json(silent=True)
        if not isinstance(b, dict):
            raise ValueError("a JSON object body is required")
        return b
    return {k: v for k, v in request.form.items() if k != CSRF_FORM_FIELD}


def _done(code: int, payload: Dict[str, Any]):
    """JSON for API clients; a 303 back to the page (result in the query string) for the page's forms."""
    if request.method == "POST" and _from_form():
        msg = payload.get("result") or payload.get("detail") or payload.get("error") or ""
        return redirect("%s?status=%d&result=%s" % (PAGE, code, quote(str(msg)[:300])), code=303)
    return jsonify(payload), code


def _text(b: Mapping[str, Any], key: str) -> str:
    v = b.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError("%s is required" % key)
    return v.strip()


def _version(b: Mapping[str, Any], key: str, *, nullable: bool = False) -> Optional[int]:
    if key not in b:
        raise ValueError("%s is required" % key)
    v = b[key]
    if v in (None, "", "none", "null") and nullable:
        return None
    try:
        iv = int(v)
    except (TypeError, ValueError):
        raise ValueError("%s must be a version number" % key)
    if isinstance(v, bool) or iv < 1 or str(iv) != str(v).strip():
        raise ValueError("%s must be a version number" % key)
    return iv


def _store() -> TC.TemplateControls:
    return TC.TemplateControls(_path(), clock=_utc_now)


def _act(fn) -> Tuple[int, Dict[str, Any]]:
    try:
        b = _body()
        result = fn(b)
    except TC.ControlError as exc:
        return 409, {"error": "refused", "detail": str(exc)}
    except ValueError as exc:
        return 400, {"error": "bad_request", "detail": str(exc)}
    return 200, {"ok": True, "result": result}


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
    return {"lane": TC.LANE, "writes_enabled": _writes_enabled(), "store_problem": problem,
            "live_templates": sorted(LIVE_TEMPLATES),
            "active": {"version": active.version, "source": active.source, "config": active.config,
                       "fallback_reasons": list(active.fallback_reasons)},
            "versions": versions, "audit": audit, "templates": _bounds(),
            "note": "selection controls the paper lane and the shadow runner; live routing stays Template 1 only"}


@bp.route("/api/template-controls", methods=["GET"])
@_guarded
def api_state():
    return jsonify(state())


@bp.route("/api/template-controls/propose", methods=["POST"])
@_guarded
@csrf_required
@_writable
def api_propose():
    def run(b):
        cfg = b.get("config")
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except ValueError:
                raise ValueError("config must be a JSON object")
        if not isinstance(cfg, dict):
            raise ValueError("config must be a JSON object")
        v = _store().propose(cfg, author=_text(b, "author"), reason="[dashboard] " + _text(b, "reason"))
        return "proposed version %d" % v
    return _done(*_act(run))


@bp.route("/api/template-controls/approve", methods=["POST"])
@_guarded
@csrf_required
@_writable
def api_approve():
    def run(b):
        v = _version(b, "version")
        _store().approve(v, approver=_text(b, "approver"), reason="[dashboard] " + _text(b, "reason"))
        return "approved version %d" % v
    return _done(*_act(run))


@bp.route("/api/template-controls/activate", methods=["POST"])
@_guarded
@csrf_required
@_writable
def api_activate():
    def run(b):
        v = _version(b, "version")
        expected = _version(b, "expected_current", nullable=True)
        st = _store()
        if expected is None and TC.read_active(_path()).version is not None:
            raise TC.ControlError("expected_current is empty but a version is active: reload and retry")
        st.activate(v, actor=_text(b, "actor"), expected_current=expected)
        return "activated version %d" % v
    return _done(*_act(run))


@bp.route("/api/template-controls/rollback", methods=["POST"])
@_guarded
@csrf_required
@_writable
def api_rollback():
    def run(b):
        expected = _version(b, "expected_current")
        target = _store().rollback(actor=_text(b, "actor"), reason="[dashboard] " + _text(b, "reason"),
                                   expected_current=expected)
        return "rolled back to version %d" % target
    return _done(*_act(run))


@bp.route("/api/template-controls/withdraw", methods=["POST"])
@_guarded
@csrf_required
@_writable
def api_withdraw():
    def run(b):
        v = _version(b, "version")
        _store().withdraw(v, actor=_text(b, "actor"), reason="[dashboard] " + _text(b, "reason"))
        return "withdrew version %d" % v
    return _done(*_act(run))


# ── page ─────────────────────────────────────────────────────────────────────
def _e(x: Any) -> str:
    return escape("" if x is None else str(x))


def _form(action: str, fields, label: str, csrf: str) -> str:
    rows = "".join('<label>%s <input name="%s" value="%s"%s></label> ' % (
        _e(lab), _e(name), _e(val), " required" if req else "") for name, lab, val, req in fields)
    return ('<form method="post" action="/api/template-controls/%s"><input type="hidden" name="%s" value="%s">'
            '%s<button type="submit">%s</button></form>' % (_e(action), CSRF_FORM_FIELD, _e(csrf), rows, _e(label)))


def render_page(s: Mapping[str, Any], *, csrf: str, status: Optional[str] = None, result: Optional[str] = None
                ) -> str:
    a = s["active"]
    cur = a["version"]
    parts = ["<!DOCTYPE html><html><head><meta charset='utf-8'><title>Template controls</title></head><body>",
             "<h1>Paper-lane template controls</h1>",
             "<p><strong>Paper lane only.</strong> %s. Live routing: %s.</p>" % (
                 _e(s["note"]), _e(", ".join(s["live_templates"])))]
    if s.get("store_problem"):
        parts.append("<p><strong>Store problem:</strong> %s (showing the built-in default)</p>" % _e(s["store_problem"]))
    if result:
        parts.append("<p class='result' data-status='%s'>%s</p>" % (_e(status), _e(result)))
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
    parts.append("</table>")
    if s["writes_enabled"]:
        parts += ["<h2>Change</h2>",
                  _form("propose", [("config", "config (JSON)", "", True), ("author", "author", "", True),
                                    ("reason", "reason", "", True)], "Propose", csrf),
                  _form("approve", [("version", "version", "", True), ("approver", "approver (your name)", "", True),
                                    ("reason", "reason", "", True)], "Approve", csrf),
                  _form("activate", [("version", "version", "", True), ("actor", "actor", "", True),
                                     ("expected_current", "expected current", "" if cur is None else cur, False)],
                        "Activate", csrf),
                  _form("rollback", [("actor", "actor", "", True), ("reason", "reason", "", True),
                                     ("expected_current", "expected current", "" if cur is None else cur, True)],
                        "Roll back", csrf),
                  _form("withdraw", [("version", "version", "", True), ("actor", "actor", "", True),
                                     ("reason", "reason", "", True)], "Withdraw", csrf)]
    else:
        parts.append("<p>Read-only: changes are disabled (%s).</p>" % _e(WRITES_KEY))
    parts.append("<h2>Parameter bounds</h2><table><tr><th>template</th><th>parameter</th><th>kind</th>"
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
    html = render_page(state(), csrf=get_csrf_token(session), status=request.args.get("status"),
                       result=request.args.get("result"))
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


__all__ = ["bp", "DB_KEY", "WRITES_KEY", "state", "render_page"]
