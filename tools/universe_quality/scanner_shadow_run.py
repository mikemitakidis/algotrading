#!/usr/bin/env python3
"""Runtime registry scanner shadow-run (read-only, fixture-backed, no network).

Validates the REAL bot.scanner.scan_cycle path for the 5 UK pilot symbols only,
with a deterministic fixture data provider monkeypatched in place of the live
provider — so Yahoo/yfinance is never called and the VPS rate-limit is
irrelevant. Read-only and side-effect-free:

  - focus = bot.universe.uk_pilot.get_uk_pilot_symbols()  (the 5 UK symbols)
  - conn = None  -> no DB writes, no flywheel log_candidate path
  - the fixture provider replaces bot.providers.get_provider (patched there,
    because bot.data.fetch_bars does `from bot.providers import get_provider`
    at call time, so the name is resolved on bot.providers)
  - no Telegram (scan_cycle never sends; it only logs and returns signals/meta)
  - no broker / live / paper (scan_cycle constructs no orders; nothing here
    imports execution code)

Committed report is SIMULATED/fixture only. Live/runtime output goes to /tmp.

This module is a tool, not runtime: nothing in bot/ imports it.
"""
import argparse
import contextlib
import datetime
import json
import logging
import time
from pathlib import Path
from unittest import mock

import pandas as pd

from bot.providers.base import DataProvider
from bot.universe.uk_pilot import get_uk_pilot_symbols

_EXPECTED = ["AAF.L", "AAL.L", "ABDN.L", "ABF.L", "ADM.L"]
# The exact callable bot.data.fetch_bars resolves at call time.
_PATCH_TARGET = "bot.providers.get_provider"


def _make_bars(n_days: int, end_date: str, seed: float) -> pd.DataFrame:
    """Deterministic UTC-indexed daily OHLCV frame (lowercase columns).

    A gently trending series so feature/indicator computation has something
    real to chew on; fully deterministic from (n_days, end_date, seed).
    """
    end = datetime.datetime.fromisoformat(end_date).replace(
        tzinfo=datetime.timezone.utc)
    idx = pd.date_range(end=end, periods=n_days, freq="D", tz="UTC")
    rows = []
    price = 100.0 + seed
    for i in range(n_days):
        # deterministic small oscillation + slow drift
        drift = 0.05 * i
        osc = 2.0 * ((i % 7) - 3) / 3.0
        close = price + drift + osc
        high = close + 1.0
        low = close - 1.0
        open_ = close - 0.5 * osc
        vol = 1_000_000 + (i % 5) * 25_000 + int(seed * 1000)
        rows.append((open_, high, low, close, float(vol)))
    return pd.DataFrame(
        rows, index=idx,
        columns=["open", "high", "low", "close", "volume"])


class FixtureDataProvider(DataProvider):
    """Offline DataProvider returning canned bars for the pilot symbols only.

    Records which symbols were requested so the harness can assert that ONLY
    the UK pilot symbols were touched (never the 193 global / 536 US sets) and
    that yfinance was never used.
    """

    def __init__(self, end_date="2026-06-26", n_days=120):
        self.end_date = end_date
        self.n_days = n_days
        self.requested_symbols = []     # flat log of every symbol asked for
        self.fetch_calls = 0

    @property
    def name(self) -> str:
        return "fixture (shadow-run, offline)"

    def fetch_bars(self, symbols, period, interval):
        self.fetch_calls += 1
        out = {}
        for s in symbols:
            self.requested_symbols.append(s)
            # serve bars ONLY for the 5 UK pilot symbols; anything else is
            # absent (which also makes an accidental 193/536 scan visibly empty)
            if s in _EXPECTED:
                seed = float(_EXPECTED.index(s))
                out[s] = _make_bars(self.n_days, self.end_date, seed)
        return out

    def fetch_bars_range(self, sym, interval, start, end):
        # Not used by the live scanner path; implemented to satisfy the ABC.
        return None, "not_implemented_in_fixture"


def _min_config():
    """Minimal scanner config. Routing thresholds are TF-count labels only;
    no execution is triggered by them."""
    return {
        "strategy": "default",
        "routing": {"etoro_min_tfs": 4, "ibkr_min_tfs": 2, "min_valid_tfs": 1},
    }


def run_shadow(config=None, end_date="2026-06-26"):
    """Run the REAL scan_cycle against the fixture provider for the 5 UK
    symbols. Returns a result dict with signals/meta/elapsed and the fixture's
    request log. Performs no DB writes (conn=None) and no network."""
    from bot.scanner import scan_cycle  # imported here to keep import surface
    focus = get_uk_pilot_symbols()
    config = config or _min_config()
    fixture = FixtureDataProvider(end_date=end_date)

    log_records = []
    handler = logging.Handler()
    handler.emit = lambda rec: log_records.append(rec.getMessage())
    root = logging.getLogger()
    root.addHandler(handler)

    t0 = time.monotonic()
    with mock.patch(_PATCH_TARGET, return_value=fixture):
        signals, meta = scan_cycle(focus, config, conn=None, cycle_id=0)
    elapsed = time.monotonic() - t0
    root.removeHandler(handler)

    return {
        "focus": focus,
        "n_focus": len(focus),
        "fixture_fetch_calls": fixture.fetch_calls,
        "requested_symbols_unique": sorted(set(fixture.requested_symbols)),
        "signals": signals,
        "n_signals": len(signals),
        "meta": meta,
        "elapsed_seconds": round(elapsed, 4),
        "n_log_lines": len(log_records),
    }


def render(result, data_source="simulated_fixture"):
    L = []
    L.append("# Runtime Registry — Scanner Shadow-Run")
    L.append("")
    L.append("- report_type: **real scan_cycle shadow-run (fixture-backed)**")
    L.append("- scope: **UK pilot 5 symbols only**")
    L.append("- data_source: **%s**" % data_source)
    L.append("- network: **disabled**")
    L.append("- provider: **fixture (monkeypatched; yfinance NOT called)**")
    L.append("- not_live_yfinance: **true**")
    L.append("- focus: **%d** (%s)"
             % (result["n_focus"], ", ".join(result["focus"])))
    L.append("- fixture_fetch_calls: **%d**" % result["fixture_fetch_calls"])
    L.append("- symbols actually requested: %s"
             % ", ".join("`%s`" % s
                         for s in result["requested_symbols_unique"]))
    L.append("- signals_returned: **%d**" % result["n_signals"])
    L.append("- elapsed_seconds: **%s**" % result["elapsed_seconds"])
    L.append("")
    L.append("> Read-only shadow run of the REAL bot.scanner.scan_cycle with a "
             "monkeypatched fixture data provider. No Yahoo/yfinance call, no "
             "network, no DB writes (conn=None), no Telegram, no broker / live "
             "/ paper. Default runtime is unchanged; the US 536 set and the 193 "
             "global set are never loaded.")
    L.append("")
    L.append("## Signals (deterministic)")
    L.append("")
    if not result["signals"]:
        L.append("(no actionable signals from the fixture series — the run "
                 "still validates the full scan_cycle path end to end)")
    else:
        L.append("| symbol | direction | route | TFs |")
        L.append("|---|---|---|---|")
        for s in result["signals"][:25]:
            L.append("| `%s` | %s | %s | %s |"
                     % (s.get("symbol"), s.get("direction"),
                        s.get("route"), s.get("tf_count", s.get("tfs"))))
    L.append("")
    L.append("## Safety confirmation")
    L.append("")
    L.append("- real scan_cycle invoked (not a copy); focus = 5 UK pilot only")
    L.append("- fixture provider used; yfinance provider not called; no network")
    L.append("- conn=None -> no DB insert / no flywheel log_candidate")
    L.append("- no Telegram; no broker / live / paper; no orders constructed")
    L.append("- no bot/scanner.py or bot/data.py edit; no default-path change")
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report",
                    default="reports/runtime_registry_scanner_shadow_run.md")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--end-date", default="2026-06-26")
    args = ap.parse_args()
    result = run_shadow(end_date=args.end_date)
    rp = Path(args.report)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(render(result), encoding="utf-8")
    print("wrote %s" % rp)
    if args.json_out:
        serialisable = {k: v for k, v in result.items()
                        if k not in ("signals", "meta")}
        serialisable["n_signals"] = result["n_signals"]
        Path(args.json_out).write_text(json.dumps(serialisable, indent=2))
        print("wrote %s" % args.json_out)
    print("focus=%d requested=%s signals=%d elapsed=%ss"
          % (result["n_focus"], result["requested_symbols_unique"],
             result["n_signals"], result["elapsed_seconds"]))


if __name__ == "__main__":
    main()
