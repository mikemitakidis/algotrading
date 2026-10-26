#!/usr/bin/env python3
"""M21.1 — signals-only BUY / SHORT alerts from the scanner + M19 scoring.

This process cannot place orders: it imports no broker, order or paper-account
code and has no flag that would. It scans (or reads signals), scores them with
the read-only M19 bridge, and sends Telegram messages through a persistent
outbox with dedup, cooldown, retry and expiry (see signal_alerts.py).

Signal sources (choose one):
  --from-json PATH   a JSON list of scanner-format signal dicts (offline)
  --scan             run the existing scanner (bot.scanner.scan_cycle) once on
                     the focus list; read-only (conn=None: no DB writes)
  --drain-only       deliver pending outbox alerts; no new signals

Delivery:
  default            DRY RUN: print the messages that would be sent; writes
                     nothing, sends nothing
  --send             enqueue + deliver via Telegram. Needs TELEGRAM_ENABLED,
                     TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the process
                     environment or from --credentials-file (only TELEGRAM_*
                     keys are read from that file; nothing else is loaded)

Scheduling: one invocation = one cycle (run it from a timer, e.g. every 15
minutes during market hours). Nothing here installs a service.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.signal_scoring.signal_alerts import (  # noqa: E402
    BUCKET_ORDER, SIGNALS_ONLY, AlertOutbox, AlertPolicy, SignalAlertService,
    default_db_path, format_alert_message,
)

TELEGRAM_KEYS = ("TELEGRAM_ENABLED", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
_TRUE = ("1", "true", "yes", "on")


def read_telegram_credentials(path: Optional[str]) -> Dict[str, str]:
    """TELEGRAM_* values: process environment first, then (if given) a
    KEY=VALUE credentials file. Only the three TELEGRAM_* keys are ever read;
    nothing is exported into os.environ."""
    vals: Dict[str, str] = {}
    if path:
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k in TELEGRAM_KEYS:
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1]
                vals[k] = v
    for k in TELEGRAM_KEYS:
        if os.environ.get(k, "").strip():
            vals[k] = os.environ[k].strip()
    return vals


def _scan_signals(focus_size: int, use_registry: bool) -> List[Dict[str, Any]]:
    from bot.focus import FOCUS_SYMBOLS
    from bot.scanner import scan_cycle
    focus: List[str] = []
    if use_registry:
        try:
            from bot.universe.active_selection import get_scan_ready_symbols
            focus = list(get_scan_ready_symbols())[:focus_size]
        except Exception as exc:                       # mirror main.py: fall back, never abort
            print("registry universe unavailable (%s); using FOCUS_SYMBOLS"
                  % type(exc).__name__, file=sys.stderr)
    if not focus:
        focus = list(FOCUS_SYMBOLS)[:focus_size]
    signals, _meta = scan_cycle(focus, {}, conn=None, cycle_id=0)   # conn=None: read-only
    return list(signals)


def _load_json_signals(path: str) -> List[Dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("signals"), list):
        data = data["signals"]
    if not isinstance(data, list):
        raise SystemExit("--from-json must contain a JSON list of signal objects")
    return data


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-json", metavar="PATH")
    src.add_argument("--scan", action="store_true")
    src.add_argument("--drain-only", action="store_true")
    ap.add_argument("--send", action="store_true",
                    help="really send via Telegram (default: dry run)")
    ap.add_argument("--credentials-file", default=None)
    ap.add_argument("--db", default=None, help="outbox SQLite path (default: %s)" % default_db_path())
    ap.add_argument("--focus-size", type=int, default=150)
    ap.add_argument("--registry-universe", action="store_true")
    ap.add_argument("--min-bucket", choices=BUCKET_ORDER[1:], default="MANUAL_REVIEW")
    ap.add_argument("--min-score", type=float, default=None)
    ap.add_argument("--require-hard-gate", action="store_true")
    ap.add_argument("--no-buy", action="store_true")
    ap.add_argument("--no-short", action="store_true")
    ap.add_argument("--cooldown-minutes", type=float, default=240.0)
    ap.add_argument("--max-signal-age-minutes", type=float, default=30.0)
    ap.add_argument("--max-alerts", type=int, default=10)
    ap.add_argument("--json-out", default=None)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if SIGNALS_ONLY is not True:                        # there is no trading mode
        raise SystemExit("signals-only invariant violated")
    policy = AlertPolicy(min_decision_bucket=args.min_bucket, min_score_100=args.min_score,
                         require_hard_gate=args.require_hard_gate, allow_buy=not args.no_buy,
                         allow_short=not args.no_short, cooldown_minutes=args.cooldown_minutes,
                         max_signal_age_minutes=args.max_signal_age_minutes,
                         max_alerts_per_cycle=args.max_alerts)

    transport = None
    if args.send:
        creds = read_telegram_credentials(args.credentials_file)
        if creds.get("TELEGRAM_ENABLED", "").strip().lower() not in _TRUE:
            print("REFUSED: TELEGRAM_ENABLED is not true; nothing sent.", file=sys.stderr)
            return 2
        if not creds.get("TELEGRAM_BOT_TOKEN") or not creds.get("TELEGRAM_CHAT_ID"):
            print("REFUSED: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing; nothing sent.",
                  file=sys.stderr)
            return 2
        from tools.signal_scoring.telegram_transport import TelegramTransport
        transport = TelegramTransport(token=creds["TELEGRAM_BOT_TOKEN"],
                                      chat_id=creds["TELEGRAM_CHAT_ID"])

    if args.drain_only:
        signals: List[Dict[str, Any]] = []
    elif args.scan:
        signals = _scan_signals(args.focus_size, args.registry_universe)
    else:
        signals = _load_json_signals(args.from_json)

    if not args.send:                                   # dry run: no outbox, no network
        svc = SignalAlertService(outbox=None, transport=None, policy=policy)
        alerts, skipped = svc.evaluate(signals)
        report: Dict[str, Any] = {"mode": "dry-run", "signals_only": True,
                                  "signals": len(signals),
                                  "would_alert": [a.alert_key for a in alerts],
                                  "skipped": skipped}
        for a in alerts:
            print("----- would send -----\n%s" % format_alert_message(a))
    else:
        svc = SignalAlertService(outbox=AlertOutbox(args.db or default_db_path()),
                                 transport=transport, policy=policy)
        report = {"mode": "send", "signals": len(signals)}
        report.update(svc.run_cycle(signals))
    report["at_utc"] = datetime.now(timezone.utc).isoformat()
    text = json.dumps(report, indent=2, default=str)
    if args.json_out:
        Path(args.json_out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
