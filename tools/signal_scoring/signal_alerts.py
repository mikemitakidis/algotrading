"""M21.1 — reliable BUY / SHORT signal alerts (signals-only; never trades).

Pipeline: scanner signal -> M19 score (research-grade, read-only) -> alert
policy -> persistent outbox (dedup + cooldown) -> transport (Telegram) with
retry / backoff / expiry.

SIGNALS-ONLY GUARANTEE. This module, the Telegram transport and the CLI import
no broker, order, paper-account or risk-execution code, and nothing here has a
broker parameter. An alert is a message, not an order intent. The test suite
proves it three ways: a static import ban, a subprocess ``sys.modules`` check
after a full cycle, and broker ``submit`` sentinels that would raise if called.

RELIABILITY.
  * Write-ahead outbox (SQLite): an alert is persisted before any send, so a
    crash never silently loses it; a restart delivers what is still pending.
  * Idempotency: ``alert_key = SYMBOL|ACTION|signal timestamp`` is the primary
    key, so the same scanner signal is never enqueued twice, across restarts.
  * Cooldown: a symbol+action pair alerted (pending or sent) within
    ``cooldown_minutes`` is suppressed even when the scanner re-emits it with a
    new timestamp every cycle.
  * Delivery claims are compare-and-swap (pending -> sending), so two
    concurrent runners never send the same alert.
  * Retry: 429 honours Telegram's ``retry_after``; 5xx / network errors back
    off exponentially; other 4xx fail permanently (retrying cannot help).
  * Expiry: an alert not delivered within ``max_delivery_delay_minutes`` is
    expired, not sent late — a stale BUY/SHORT is misleading.
  * Interrupted sends (a ``sending`` row older than the claim lease) are
    retried and labelled "possible duplicate": at-least-once, honestly marked.

HONEST WORDING. Scores are the M19 research-grade composite (uncalibrated);
the message says so and never presents the score as a win probability.
"""
from __future__ import annotations

import contextlib
import html
import json
import math
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

ALERT_SCHEMA_VERSION = "m21.1-signal-alert-v1"
SIGNALS_ONLY = True                      # there is no order-placing mode
SIGNALS_ONLY_BANNER = "SIGNAL ONLY — no order placed"
TELEGRAM_MAX_CHARS = 4096

ACTION_BUY = "BUY"
ACTION_SHORT = "SHORT"
_ACTION_BY_DIRECTION = {"long": ACTION_BUY, "short": ACTION_SHORT}

# M19 decision buckets, lowest to highest. BLOCKED never alerts.
BUCKET_ORDER = ("BLOCKED", "REJECT", "WATCH", "MANUAL_REVIEW", "ELIGIBLE", "HIGH_CONVICTION")

STATUS_PENDING = "pending"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_SENDING, STATUS_SENT)


class InvalidSignal(ValueError):
    pass


def _enum_value(v: Any) -> Any:
    return getattr(v, "value", v)


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return _utc(dt).isoformat()


def _parse_ts(v: Any) -> datetime:
    if isinstance(v, datetime):
        ts = v
    elif isinstance(v, str) and v.strip():
        try:
            ts = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            raise InvalidSignal("timestamp is not ISO-8601")
    else:
        raise InvalidSignal("timestamp missing")
    if ts.tzinfo is None:
        raise InvalidSignal("timestamp must be timezone-aware")
    return ts.astimezone(timezone.utc)


def _pos_float(v: Any, name: str) -> float:
    if isinstance(v, bool):
        raise InvalidSignal("%s is not a number" % name)
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise InvalidSignal("%s is not a number" % name)
    if not math.isfinite(x) or x <= 0:
        raise InvalidSignal("%s must be finite and > 0" % name)
    return x


def action_for_direction(direction: Any) -> str:
    try:
        return _ACTION_BY_DIRECTION[direction]
    except (KeyError, TypeError):
        raise InvalidSignal("direction must be 'long' or 'short'")


# ─────────────────────────── alert model ───────────────────────────

@dataclass(frozen=True)
class SignalAlert:
    alert_key: str
    symbol: str
    action: str                       # BUY | SHORT
    direction: str                    # long | short
    signal_ts_utc: str
    entry_price: float
    stop_loss: float
    target_price: float
    score_100: float
    decision_bucket: str
    confidence_bucket: str
    hard_gate_passed: bool
    valid_tfs: Optional[int] = None
    available_tfs: Optional[int] = None
    route: Optional[str] = None
    strategy_version: Optional[str] = None
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    schema_version: str = ALERT_SCHEMA_VERSION

    @property
    def reward_risk(self) -> Optional[float]:
        risk = abs(self.entry_price - self.stop_loss)
        if risk <= 0:
            return None
        return abs(self.target_price - self.entry_price) / risk

    def to_json(self) -> str:
        d = asdict(self)
        d["reason_codes"] = list(self.reason_codes)
        return json.dumps(d, sort_keys=True)

    @staticmethod
    def from_json(text: str) -> "SignalAlert":
        d = json.loads(text)
        d["reason_codes"] = tuple(d.get("reason_codes") or ())
        return SignalAlert(**d)


def make_alert_key(symbol: str, action: str, signal_ts: datetime) -> str:
    return "%s|%s|%s" % (symbol, action, _iso(signal_ts))


def build_alert(signal: Mapping[str, Any], scored: Any) -> SignalAlert:
    """Validate a scanner signal + its M19 score and build the alert.
    Fail-closed: anything inconsistent raises InvalidSignal (never repaired)."""
    sym = signal.get("symbol")
    if not isinstance(sym, str) or not sym.strip() or sym != sym.strip().upper():
        raise InvalidSignal("symbol must be a non-empty upper-case string")
    direction = signal.get("direction")
    action = action_for_direction(direction)
    ts = _parse_ts(signal.get("timestamp"))
    entry = _pos_float(signal.get("entry_price", signal.get("price")), "entry_price")
    stop = _pos_float(signal.get("stop_loss"), "stop_loss")
    target = _pos_float(signal.get("target_price"), "target_price")
    if direction == "long" and not (stop < entry < target):
        raise InvalidSignal("BUY requires stop_loss < entry < target")
    if direction == "short" and not (target < entry < stop):
        raise InvalidSignal("SHORT requires target < entry < stop_loss")

    if getattr(scored, "symbol", sym) != sym:
        raise InvalidSignal("score belongs to another symbol")
    side = str(_enum_value(getattr(scored, "side", ""))).upper()
    if side != direction.upper():
        raise InvalidSignal("scored side %r does not match direction %r" % (side, direction))
    if getattr(scored, "execution_eligible", False) is not False:
        # M19 contract: research-grade output is never execution-eligible.
        raise InvalidSignal("score claims execution eligibility (M19 contract breach)")
    score = getattr(scored, "final_score_100", None)
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
        raise InvalidSignal("final_score_100 missing")
    bucket = str(_enum_value(getattr(scored, "decision_bucket", "")))
    if bucket not in BUCKET_ORDER:
        raise InvalidSignal("unknown decision bucket %r" % bucket)

    def _int(v):
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    return SignalAlert(
        alert_key=make_alert_key(sym, action, ts), symbol=sym, action=action,
        direction=direction, signal_ts_utc=_iso(ts), entry_price=entry, stop_loss=stop,
        target_price=target, score_100=round(float(score), 1), decision_bucket=bucket,
        confidence_bucket=str(_enum_value(getattr(scored, "confidence_bucket", ""))),
        hard_gate_passed=bool(getattr(scored, "hard_gate_passed", False)),
        valid_tfs=_int(signal.get("valid_count")), available_tfs=_int(signal.get("available_tfs")),
        route=signal.get("route") if isinstance(signal.get("route"), str) else None,
        strategy_version=(str(signal["strategy_version"])
                          if signal.get("strategy_version") is not None else None),
        reason_codes=tuple(sorted(str(c) for c in (getattr(scored, "reason_codes", None) or ()))))


# ─────────────────────────── policy ───────────────────────────

@dataclass(frozen=True)
class AlertPolicy:
    min_decision_bucket: str = "MANUAL_REVIEW"   # M19 score >= manual_review_low
    min_score_100: Optional[float] = None         # optional extra floor
    require_hard_gate: bool = False
    allow_buy: bool = True
    allow_short: bool = True
    max_signal_age_minutes: float = 30.0
    cooldown_minutes: float = 240.0
    max_alerts_per_cycle: int = 10
    max_delivery_delay_minutes: float = 30.0
    max_attempts: int = 6
    backoff_base_seconds: float = 30.0
    backoff_max_seconds: float = 900.0
    claim_lease_seconds: float = 120.0

    def __post_init__(self):
        if self.min_decision_bucket not in BUCKET_ORDER or self.min_decision_bucket == "BLOCKED":
            raise ValueError("min_decision_bucket must be one of %s (not BLOCKED)"
                             % (BUCKET_ORDER[1:],))
        if self.max_alerts_per_cycle < 1 or self.max_attempts < 1:
            raise ValueError("max_alerts_per_cycle and max_attempts must be >= 1")


def policy_rejection(alert: SignalAlert, policy: AlertPolicy, now: datetime) -> Optional[str]:
    """None if the alert passes the policy, else a reason code."""
    if alert.decision_bucket == "BLOCKED":
        return "blocked_by_hard_gate"
    if BUCKET_ORDER.index(alert.decision_bucket) < BUCKET_ORDER.index(policy.min_decision_bucket):
        return "below_min_bucket"
    if policy.min_score_100 is not None and alert.score_100 < policy.min_score_100:
        return "below_min_score"
    if policy.require_hard_gate and not alert.hard_gate_passed:
        return "hard_gate_not_passed"
    if alert.action == ACTION_BUY and not policy.allow_buy:
        return "buy_alerts_disabled"
    if alert.action == ACTION_SHORT and not policy.allow_short:
        return "short_alerts_disabled"
    age_min = (_utc(now) - datetime.fromisoformat(alert.signal_ts_utc)).total_seconds() / 60.0
    if age_min < -1.0:
        return "signal_from_future"
    if age_min > policy.max_signal_age_minutes:
        return "stale_signal"
    return None


# ─────────────────────────── message ───────────────────────────

def _px(x: float) -> str:
    return ("%.4f" % x).rstrip("0").rstrip(".") if x < 1 else "%.2f" % x


def format_alert_message(alert: SignalAlert, *, possible_duplicate: bool = False) -> str:
    """Telegram HTML message. Every interpolated value is HTML-escaped."""
    e = html.escape
    icon = "🟢" if alert.action == ACTION_BUY else "🔴"
    ts = datetime.fromisoformat(alert.signal_ts_utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = []
    if possible_duplicate:
        lines.append("⚠️ <i>Possible duplicate — an earlier delivery was interrupted.</i>")
    lines += [
        "%s <b>%s signal — %s</b>" % (icon, e(alert.action), e(alert.symbol)),
        "<b>%s</b>" % e(SIGNALS_ONLY_BANNER),
        "",
        "Entry ≈ <code>%s</code>  Stop <code>%s</code>  Target <code>%s</code>" % (
            e(_px(alert.entry_price)), e(_px(alert.stop_loss)), e(_px(alert.target_price))),
    ]
    rr = alert.reward_risk
    if rr is not None:
        lines.append("Reward:risk ≈ %s" % e("%.2f" % rr))
    lines.append("Score %s/100 · bucket %s · confidence %s" % (
        e("%.1f" % alert.score_100), e(alert.decision_bucket), e(alert.confidence_bucket or "n/a")))
    lines.append("<i>Research-grade ranking (uncalibrated) — not a win probability.</i>")
    tf = []
    if alert.valid_tfs is not None:
        tf.append("timeframes %s%s" % (alert.valid_tfs, "/%s" % alert.available_tfs
                                       if alert.available_tfs is not None else ""))
    if alert.route:
        tf.append("route %s" % alert.route)
    if alert.strategy_version:
        tf.append("strategy v%s" % alert.strategy_version)
    if tf:
        lines.append(e(" · ".join(tf)))
    if alert.action == ACTION_SHORT:
        lines.append("<i>Short side: research signal only; never execution-eligible.</i>")
    lines.append("Signal time %s" % e(ts))
    text = "\n".join(lines)
    if len(text) > TELEGRAM_MAX_CHARS:                       # defensive; normal size ~500
        text = text[:TELEGRAM_MAX_CHARS - 1] + "…"
    return text


# ─────────────────────────── outbox ───────────────────────────

def default_db_path() -> str:
    env = os.environ.get("SIGNAL_ALERTS_DB_PATH")
    if env:
        return env
    return str(Path(__file__).resolve().parents[2] / "data" / "signal_alerts.db")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_alert_outbox (
    alert_key            TEXT PRIMARY KEY,
    symbol               TEXT NOT NULL,
    action               TEXT NOT NULL CHECK (action IN ('BUY','SHORT')),
    signal_ts_utc        TEXT NOT NULL,
    created_at_utc       TEXT NOT NULL,
    alert_json           TEXT NOT NULL,
    status               TEXT NOT NULL,
    attempts             INTEGER NOT NULL DEFAULT 0,
    next_attempt_at_utc  TEXT NOT NULL,
    claimed_at_utc       TEXT,
    last_error           TEXT,
    sent_at_utc          TEXT,
    telegram_message_id  TEXT,
    possible_duplicate   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_sao_symbol_action ON signal_alert_outbox(symbol, action, created_at_utc);
CREATE INDEX IF NOT EXISTS ix_sao_status ON signal_alert_outbox(status, next_attempt_at_utc);
"""


class AlertOutbox:
    def __init__(self, db_path: str):
        self.db_path = db_path
        d = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(d, exist_ok=True)
        with self._session() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)   # autocommit
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        return c

    @contextlib.contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        c = self._conn()
        try:
            yield c
        finally:
            c.close()

    def enqueue(self, alert: SignalAlert, *, now: datetime, cooldown_minutes: float
                ) -> Tuple[bool, str]:
        """Atomically: duplicate check, cooldown check, insert. Returns
        (enqueued, reason)."""
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM signal_alert_outbox WHERE alert_key=?",
                         (alert.alert_key,)).fetchone():
                c.execute("ROLLBACK")
                return False, "duplicate_signal"
            since = _iso(_utc(now) - timedelta(minutes=cooldown_minutes))
            q = ("SELECT 1 FROM signal_alert_outbox WHERE symbol=? AND action=? "
                 "AND created_at_utc>=? AND status IN (%s)" % ",".join("?" * len(ACTIVE_STATUSES)))
            if c.execute(q, (alert.symbol, alert.action, since, *ACTIVE_STATUSES)).fetchone():
                c.execute("ROLLBACK")
                return False, "cooldown"
            c.execute("INSERT INTO signal_alert_outbox (alert_key, symbol, action, signal_ts_utc,"
                      " created_at_utc, alert_json, status, attempts, next_attempt_at_utc)"
                      " VALUES (?,?,?,?,?,?,?,0,?)",
                      (alert.alert_key, alert.symbol, alert.action, alert.signal_ts_utc, _iso(now),
                       alert.to_json(), STATUS_PENDING, _iso(now)))
            c.execute("COMMIT")
            return True, "enqueued"
        except Exception:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise
        finally:
            c.close()

    def release_stale_claims(self, *, now: datetime, lease_seconds: float) -> int:
        """A 'sending' row older than the lease means the sender died mid-send.
        Re-queue it, flagged possible_duplicate (Telegram may have received it)."""
        cutoff = _iso(_utc(now) - timedelta(seconds=lease_seconds))
        with self._session() as c:
            cur = c.execute("UPDATE signal_alert_outbox SET status=?, possible_duplicate=1,"
                            " claimed_at_utc=NULL, last_error='interrupted_send'"
                            " WHERE status=? AND claimed_at_utc<?",
                            (STATUS_PENDING, STATUS_SENDING, cutoff))
            return cur.rowcount

    def expire(self, *, now: datetime, max_delay_minutes: float) -> List[str]:
        cutoff = _iso(_utc(now) - timedelta(minutes=max_delay_minutes))
        with self._session() as c:
            keys = [r["alert_key"] for r in c.execute(
                "SELECT alert_key FROM signal_alert_outbox WHERE status=? AND created_at_utc<?",
                (STATUS_PENDING, cutoff))]
            for k in keys:
                c.execute("UPDATE signal_alert_outbox SET status=? WHERE alert_key=? AND status=?",
                          (STATUS_EXPIRED, k, STATUS_PENDING))
            return keys

    def due(self, *, now: datetime) -> List[sqlite3.Row]:
        with self._session() as c:
            return list(c.execute("SELECT * FROM signal_alert_outbox WHERE status=? AND "
                                  "next_attempt_at_utc<=? ORDER BY created_at_utc, alert_key",
                                  (STATUS_PENDING, _iso(now))))

    def claim(self, alert_key: str, *, now: datetime) -> bool:
        with self._session() as c:
            cur = c.execute("UPDATE signal_alert_outbox SET status=?, claimed_at_utc=?,"
                            " attempts=attempts+1 WHERE alert_key=? AND status=?",
                            (STATUS_SENDING, _iso(now), alert_key, STATUS_PENDING))
            return cur.rowcount == 1

    def mark_sent(self, alert_key: str, *, now: datetime, message_id: Optional[str]) -> None:
        with self._session() as c:
            c.execute("UPDATE signal_alert_outbox SET status=?, sent_at_utc=?, telegram_message_id=?,"
                      " last_error=NULL, claimed_at_utc=NULL WHERE alert_key=? AND status=?",
                      (STATUS_SENT, _iso(now), message_id, alert_key, STATUS_SENDING))

    def mark_retry(self, alert_key: str, *, next_at: datetime, error: str) -> None:
        with self._session() as c:
            c.execute("UPDATE signal_alert_outbox SET status=?, next_attempt_at_utc=?, last_error=?,"
                      " claimed_at_utc=NULL WHERE alert_key=? AND status=?",
                      (STATUS_PENDING, _iso(next_at), error[:500], alert_key, STATUS_SENDING))

    def mark_failed(self, alert_key: str, *, error: str) -> None:
        with self._session() as c:
            c.execute("UPDATE signal_alert_outbox SET status=?, last_error=?, claimed_at_utc=NULL"
                      " WHERE alert_key=? AND status=?",
                      (STATUS_FAILED, error[:500], alert_key, STATUS_SENDING))

    def rows(self) -> List[Dict[str, Any]]:
        with self._session() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM signal_alert_outbox ORDER BY created_at_utc, alert_key")]


# ─────────────────────────── service ───────────────────────────

def _default_scorer(signal: Mapping[str, Any]) -> Any:
    from tools.signal_scoring.scanner_bridge import score_signal   # read-only M19 scoring
    return score_signal(dict(signal))


class SignalAlertService:
    """Signals in, messages out. Holds no broker and accepts none."""

    def __init__(self, *, outbox: Optional[AlertOutbox], transport: Any,
                 policy: AlertPolicy = AlertPolicy(),
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 scorer: Callable[[Mapping[str, Any]], Any] = _default_scorer):
        self.outbox = outbox
        self.transport = transport
        self.policy = policy
        self.clock = clock
        self.scorer = scorer

    def evaluate(self, signals: Iterable[Mapping[str, Any]]
                 ) -> Tuple[List[SignalAlert], List[Dict[str, str]]]:
        """Score + validate + policy. Returns (selected alerts best-first, skipped)."""
        now = self.clock()
        selected: List[SignalAlert] = []
        skipped: List[Dict[str, str]] = []
        for sig in signals:
            sym = str(sig.get("symbol")) if isinstance(sig, Mapping) else "?"
            try:
                if not isinstance(sig, Mapping):
                    raise InvalidSignal("signal is not a mapping")
                alert = build_alert(sig, self.scorer(sig))
            except InvalidSignal as exc:
                skipped.append({"symbol": sym, "reason": "invalid_signal:%s" % exc})
                continue
            except Exception as exc:                       # scoring failure: skip, never crash
                skipped.append({"symbol": sym, "reason": "scoring_error:%s" % type(exc).__name__})
                continue
            why = policy_rejection(alert, self.policy, now)
            if why:
                skipped.append({"symbol": alert.symbol, "reason": why})
                continue
            selected.append(alert)
        selected.sort(key=lambda a: (-a.score_100, a.symbol, a.action))
        for a in selected[self.policy.max_alerts_per_cycle:]:
            skipped.append({"symbol": a.symbol, "reason": "per_cycle_cap"})
        return selected[:self.policy.max_alerts_per_cycle], skipped

    def enqueue(self, alerts: Iterable[SignalAlert]) -> Tuple[List[str], List[Dict[str, str]]]:
        if self.outbox is None:
            raise RuntimeError("no outbox configured")
        now = self.clock()
        enq, skipped = [], []
        for a in alerts:
            ok, why = self.outbox.enqueue(a, now=now, cooldown_minutes=self.policy.cooldown_minutes)
            (enq.append(a.alert_key) if ok else skipped.append({"symbol": a.symbol, "reason": why}))
        return enq, skipped

    def _backoff(self, attempts: int) -> float:
        return min(self.policy.backoff_max_seconds,
                   self.policy.backoff_base_seconds * (2 ** max(0, attempts - 1)))

    def deliver_pending(self) -> Dict[str, Any]:
        if self.outbox is None or self.transport is None:
            raise RuntimeError("delivery needs an outbox and a transport")
        p = self.policy
        out: Dict[str, Any] = {"sent": [], "retry": [], "failed": [], "expired": []}
        now = self.clock()
        out["requeued_interrupted"] = self.outbox.release_stale_claims(
            now=now, lease_seconds=p.claim_lease_seconds)
        out["expired"] = self.outbox.expire(now=now, max_delay_minutes=p.max_delivery_delay_minutes)
        for row in self.outbox.due(now=now):
            key = row["alert_key"]
            if not self.outbox.claim(key, now=self.clock()):
                continue                                    # another runner owns it
            attempts = int(row["attempts"]) + 1
            alert = SignalAlert.from_json(row["alert_json"])
            text = format_alert_message(alert, possible_duplicate=bool(row["possible_duplicate"]))
            try:
                res = self.transport.send(text)
            except Exception as exc:                        # transport bug: retryable
                res = None
                err, retryable, retry_after = "transport_exception:%s" % type(exc).__name__, True, None
            if res is not None:
                if res.ok:
                    self.outbox.mark_sent(key, now=self.clock(), message_id=res.message_id)
                    out["sent"].append(key)
                    continue
                err, retryable, retry_after = res.error or "send_failed", res.retryable, res.retry_after
            if not retryable or attempts >= p.max_attempts:
                self.outbox.mark_failed(key, error=err if not retryable
                                        else "%s (max attempts reached)" % err)
                out["failed"].append(key)
                continue
            delay = retry_after if retry_after is not None else self._backoff(attempts)
            self.outbox.mark_retry(key, next_at=self.clock() + timedelta(seconds=float(delay)),
                                   error=err)
            out["retry"].append(key)
        return out

    def run_cycle(self, signals: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
        alerts, skipped = self.evaluate(signals)
        enq, skipped2 = self.enqueue(alerts)
        delivery = self.deliver_pending() if self.transport is not None else None
        return {"signals_only": SIGNALS_ONLY, "selected": [a.alert_key for a in alerts],
                "enqueued": enq, "skipped": skipped + skipped2, "delivery": delivery}
