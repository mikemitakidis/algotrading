"""M21 WP9 (canonical A16, Appendix A Phase 1) — template controls for the paper lane.

What an operator controls here: which templates are selected for the paper
lane, each template's parameters (inside the declared bounds), and the
champion. Every change is a new immutable VERSION:

    propose (any author) -> approve (a named operator) -> activate -> [rollback]

* Validation is fail-closed and happens on propose: unknown templates or
  parameters, out-of-bounds values, a champion that is not selected, an
  empty selection, a live lane, or a missing author/reason are refused, and
  the refusal is audited.
* Only an APPROVED version can be activated; approval needs a named approver
  and a reason, and a version is approved at most once.
* Rollback re-activates the version that was active before the current one.
* Compare-and-swap: ``activate`` and ``rollback`` accept ``expected_current``;
  the switch then happens only if that version is still the active one,
  checked inside the same write transaction (a concurrent change is refused
  and audited, never overwritten). The promotion book (WP14) always uses it.
* Every action (including refusals) is appended to the audit table.
* Integrity: each stored config carries its sha256. ``active()`` never
  raises: an unreadable store, a missing row, or a config whose digest or
  validation fails falls back to the last good activation, and finally to
  the built-in default (Template 1 only), with the reason stated.
* Paper/live separation: the lane is always "paper". Selection never makes a
  template tradable: routing stays ``bot.templates.routing.LIVE_TEMPLATES``
  (Template 1 only); templates 2-4 run in shadow until WP14 promotes them.

No dashboard element is wired to this module yet: the Phase 1 UI is a later,
approved change to the protected ``dashboard/app.py``.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from bot.templates.core import APPENDIX_A, TEMPLATE_1, TemplateError, validate_params

LANE = "paper"
PROPOSED, APPROVED, ACTIVE, SUPERSEDED, WITHDRAWN = "PROPOSED", "APPROVED", "ACTIVE", "SUPERSEDED", "WITHDRAWN"
SOURCE_STORE, SOURCE_LAST_GOOD, SOURCE_DEFAULT = "store", "last_good", "default"
# An approval must come from a named human operator, never from automation.
NON_HUMAN_APPROVERS = frozenset({"system", "claude", "auto", "bot", "scheduler", "agent"})

_DDL = (
    """CREATE TABLE IF NOT EXISTS template_config_versions (
        version INTEGER PRIMARY KEY,
        created_at_utc TEXT NOT NULL,
        author TEXT NOT NULL,
        reason TEXT NOT NULL,
        lane TEXT NOT NULL,
        config_json TEXT NOT NULL,
        config_sha256 TEXT NOT NULL,
        parent_version INTEGER,
        status TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS template_config_approvals (
        version INTEGER PRIMARY KEY,
        approver TEXT NOT NULL,
        reason TEXT NOT NULL,
        approved_at_utc TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS template_config_activations (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        version INTEGER NOT NULL,
        kind TEXT NOT NULL,
        actor TEXT NOT NULL,
        at_utc TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS template_config_audit (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        at_utc TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        version INTEGER,
        detail TEXT NOT NULL)""",
)


class ControlError(ValueError):
    pass


def default_config() -> Dict[str, Any]:
    """Built-in fallback: Template 1 only (the current production behaviour)."""
    return {"lane": LANE, "selected": [TEMPLATE_1], "champion": TEMPLATE_1, "params": {}}


def canonical(cfg: Mapping[str, Any]) -> str:
    return json.dumps(cfg, sort_keys=True, separators=(",", ":"))


def sha256(cfg: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(cfg).encode()).hexdigest()


def validate_config(cfg: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalised config, or ControlError listing every problem."""
    errs: List[str] = []
    if not isinstance(cfg, Mapping):
        raise ControlError("config must be a mapping")
    extra = sorted(set(cfg) - {"lane", "selected", "champion", "params"})
    errs += ["unknown key %r" % k for k in extra]
    lane = cfg.get("lane", LANE)
    if lane != LANE:
        errs.append("lane %r refused: only the paper lane is controllable" % lane)
    sel = cfg.get("selected")
    if not isinstance(sel, (list, tuple)) or not sel:
        errs.append("selected must be a non-empty list of templates")
        sel = []
    sel = list(dict.fromkeys(sel))
    errs += ["template %r is not an Appendix A template" % t for t in sel if t not in APPENDIX_A]
    champ = cfg.get("champion")
    if champ not in sel:
        errs.append("champion %r must be one of the selected templates" % champ)
    params = cfg.get("params", {}) or {}
    norm: Dict[str, Dict[str, Any]] = {}
    if not isinstance(params, Mapping):
        errs.append("params must be a mapping")
        params = {}
    for t, p in params.items():
        if t not in sel:
            errs.append("params given for unselected template %r" % t)
            continue
        if t == TEMPLATE_1 and p:
            errs.append("Template 1 parameters live in the scanner strategy config, not here")
            continue
        try:
            norm[t] = validate_params(t, p)
        except TemplateError as exc:
            errs.append("%s: %s" % (t, exc))
    if errs:
        raise ControlError("; ".join(errs))
    for t in sel:
        if t != TEMPLATE_1 and t not in norm:
            norm[t] = validate_params(t, {})
    return {"lane": LANE, "selected": sel, "champion": champ, "params": {k: norm[k] for k in sorted(norm)}}


@dataclass(frozen=True)
class ActiveConfig:
    config: Dict[str, Any]
    version: Optional[int]
    source: str                          # store | last_good | default
    fallback_reasons: Tuple[str, ...] = field(default_factory=tuple)


class TemplateControls:
    def __init__(self, path: str, *, clock):
        if not path:
            raise ValueError("a controls store path is required")
        self.path = path
        self.clock = clock
        c = self._conn()
        try:
            for stmt in _DDL:
                c.execute(stmt)
        finally:
            c.close()

    def _conn(self):
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)

    def _now(self) -> str:
        return self.clock().isoformat()

    def _audit(self, c, actor, action, version, detail):
        c.execute("INSERT INTO template_config_audit (at_utc, actor, action, version, detail) VALUES (?,?,?,?,?)",
                  (self._now(), str(actor or ""), action, version, json.dumps(detail, sort_keys=True, default=str)))

    def _tx(self, fn):
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            out = fn(c)
            c.execute("COMMIT")
            return out
        except BaseException:
            c.execute("ROLLBACK")
            raise
        finally:
            c.close()

    def _refuse(self, actor, action, version, why):
        def run(c):
            self._audit(c, actor, action + "_REFUSED", version, {"why": why})
        self._tx(run)
        raise ControlError(why)

    # ── lifecycle ──
    def propose(self, cfg: Mapping[str, Any], *, author: str, reason: str) -> int:
        if not str(author or "").strip() or not str(reason or "").strip():
            self._refuse(author, "PROPOSE", None, "author and reason are required")
        try:
            norm = validate_config(cfg)
        except ControlError as exc:
            self._refuse(author, "PROPOSE", None, str(exc))

        def run(c):
            cur = c.execute("SELECT version FROM template_config_activations ORDER BY seq DESC LIMIT 1").fetchone()
            v = (c.execute("SELECT COALESCE(MAX(version), 0) FROM template_config_versions").fetchone()[0]) + 1
            c.execute("INSERT INTO template_config_versions VALUES (?,?,?,?,?,?,?,?,?)",
                      (v, self._now(), author, reason, LANE, canonical(norm), sha256(norm),
                       cur[0] if cur else None, PROPOSED))
            self._audit(c, author, "PROPOSED", v, {"reason": reason, "sha256": sha256(norm)})
            return v
        return self._tx(run)

    def approve(self, version: int, *, approver: str, reason: str) -> None:
        if not str(approver or "").strip() or not str(reason or "").strip():
            self._refuse(approver, "APPROVE", version, "approver and reason are required")
        if str(approver).strip().lower() in NON_HUMAN_APPROVERS:
            self._refuse(approver, "APPROVE", version, "approval must come from a named human operator")
        row = self._row(version)
        if row is None or row["status"] != PROPOSED:
            self._refuse(approver, "APPROVE", version,
                         "version %s is %s, not PROPOSED" % (version, row["status"] if row else "missing"))

        def run(c):
            c.execute("INSERT INTO template_config_approvals VALUES (?,?,?,?)", (version, approver, reason,
                                                                                 self._now()))
            c.execute("UPDATE template_config_versions SET status=? WHERE version=?", (APPROVED, version))
            self._audit(c, approver, "APPROVED", version, {"reason": reason})
        self._tx(run)

    def activate(self, version: int, *, actor: str, expected_current: Optional[int] = None) -> None:
        row = self._row(version)
        if row is None or row["status"] != APPROVED:
            self._refuse(actor, "ACTIVATE", version,
                         "only an APPROVED version can be activated (version %s is %s)"
                         % (version, row["status"] if row else "missing"))
        if not self._intact(row):
            self._refuse(actor, "ACTIVATE", version, "stored config fails its integrity check")
        self._switch(version, actor, "ACTIVATE", expected_current=expected_current)

    def rollback(self, *, actor: str, reason: str, expected_current: Optional[int] = None) -> int:
        """Re-activate the most recent earlier activation that differs from the
        current one (a second rollback therefore undoes the first)."""
        if not str(actor or "").strip() or not str(reason or "").strip():
            self._refuse(actor, "ROLLBACK", None, "actor and reason are required")
        hist = self._activations()
        if len(hist) < 2:
            self._refuse(actor, "ROLLBACK", None, "no earlier activation to roll back to")
        current = hist[-1]
        target = next((v for v in reversed(hist[:-1]) if v != current), None)
        row = self._row(target) if target is not None else None
        if row is None or not self._intact(row):
            self._refuse(actor, "ROLLBACK", target, "rollback target missing or fails its integrity check")
        self._switch(target, actor, "ROLLBACK", detail={"reason": reason, "from": current},
                     expected_current=expected_current if expected_current is not None else current)
        return target

    def withdraw(self, version: int, *, actor: str, reason: str) -> None:
        row = self._row(version)
        if row is None or row["status"] not in (PROPOSED, APPROVED):
            self._refuse(actor, "WITHDRAW", version, "only a PROPOSED or APPROVED version can be withdrawn")

        def run(c):
            c.execute("UPDATE template_config_versions SET status=? WHERE version=?", (WITHDRAWN, version))
            self._audit(c, actor, "WITHDRAWN", version, {"reason": reason})
        self._tx(run)

    def _switch(self, version, actor, kind, detail=None, expected_current=None):
        def run(c):
            prev = c.execute("SELECT version FROM template_config_activations ORDER BY seq DESC LIMIT 1").fetchone()
            if expected_current is not None and (prev[0] if prev else None) != expected_current:
                return ("conflict", prev[0] if prev else None)
            if prev and prev[0] != version:
                c.execute("UPDATE template_config_versions SET status=? WHERE version=?", (SUPERSEDED, prev[0]))
            c.execute("UPDATE template_config_versions SET status=? WHERE version=?", (ACTIVE, version))
            c.execute("INSERT INTO template_config_activations (version, kind, actor, at_utc) VALUES (?,?,?,?)",
                      (version, kind, actor, self._now()))
            self._audit(c, actor, "ACTIVATED" if kind == "ACTIVATE" else "ROLLED_BACK", version,
                        dict(detail or {}, previous=prev[0] if prev else None))
            return None
        out = self._tx(run)
        if out is not None:
            self._refuse(actor, kind, version, "concurrent change: active version is %s, expected %s"
                         % (out[1], expected_current))

    # ── reads ──
    def _row(self, version) -> Optional[Dict[str, Any]]:
        c = self._conn()
        try:
            c.row_factory = sqlite3.Row
            r = c.execute("SELECT * FROM template_config_versions WHERE version=?", (version,)).fetchone()
            return dict(r) if r else None
        finally:
            c.close()

    def _activations(self) -> List[int]:
        c = self._conn()
        try:
            return [r[0] for r in c.execute("SELECT version FROM template_config_activations ORDER BY seq")]
        finally:
            c.close()

    @staticmethod
    def _intact(row: Mapping[str, Any]) -> bool:
        try:
            cfg = json.loads(row["config_json"])
            return sha256(cfg) == row["config_sha256"] and validate_config(cfg) == cfg
        except Exception:
            return False

    def audit(self) -> List[Dict[str, Any]]:
        c = self._conn()
        try:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute("SELECT * FROM template_config_audit ORDER BY seq")]
        finally:
            c.close()

    def versions(self) -> List[Dict[str, Any]]:
        c = self._conn()
        try:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute("SELECT * FROM template_config_versions ORDER BY version")]
        finally:
            c.close()


def read_active(path: str) -> ActiveConfig:
    """The active paper-lane config. Never raises and never writes: an
    unreadable store, a missing row or a corrupt config falls back to the last
    intact activation, then to ``default_config()``; reasons are listed."""
    reasons: List[str] = []
    try:
        c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=10)
        try:
            c.row_factory = sqlite3.Row
            hist = [r[0] for r in c.execute("SELECT version FROM template_config_activations ORDER BY seq DESC")]
            rows = {r["version"]: dict(r) for r in c.execute("SELECT * FROM template_config_versions")}
        finally:
            c.close()
    except Exception as exc:
        return ActiveConfig(default_config(), None, SOURCE_DEFAULT, ("store_unreadable:%s" % type(exc).__name__,))
    if not hist:
        return ActiveConfig(default_config(), None, SOURCE_DEFAULT, ("no_activation",))
    for i, v in enumerate(hist):
        row = rows.get(v)
        if row is not None and TemplateControls._intact(row):
            return ActiveConfig(json.loads(row["config_json"]), v, SOURCE_STORE if i == 0 else SOURCE_LAST_GOOD,
                                tuple(reasons))
        reasons.append("version_%s_%s" % (v, "missing" if row is None else "failed_integrity"))
    return ActiveConfig(default_config(), None, SOURCE_DEFAULT, tuple(reasons))


def shadow_params(active: ActiveConfig) -> Dict[str, Dict[str, Any]]:
    """Parameters for the shadow runner: the controlled params of every
    selected template other than Template 1. Templates 2-4 always run in
    shadow (evidence collection); unselected ones use their defaults."""
    return {t: dict(p) for t, p in active.config.get("params", {}).items() if t != TEMPLATE_1}


__all__ = ["LANE", "PROPOSED", "APPROVED", "ACTIVE", "SUPERSEDED", "WITHDRAWN", "SOURCE_STORE", "SOURCE_LAST_GOOD",
           "SOURCE_DEFAULT", "ControlError", "default_config", "canonical", "sha256", "validate_config",
           "ActiveConfig", "TemplateControls", "read_active", "shadow_params", "NON_HUMAN_APPROVERS"]
